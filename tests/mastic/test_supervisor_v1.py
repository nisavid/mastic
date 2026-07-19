from __future__ import annotations

import threading
import unittest
from dataclasses import dataclass

from mastic.domain.admission import PressureLevel
from mastic.domain.resources import (
    ActivationPolicy,
    InferenceService,
    ResourceName,
    ServiceRunState,
)
from mastic.infrastructure.supervisor_v1 import (
    CapabilityValidationError,
    PreparedLaunch,
    ProcessIdentity,
    Supervisor,
    _launch_identity,
)


def _service(
    name: str,
    *,
    pinned: bool = False,
    route: str | None = None,
    activation: ActivationPolicy = ActivationPolicy.MANUAL,
) -> InferenceService:
    return InferenceService(
        name=ResourceName(name),
        model_alias=ResourceName(f"{name}-model"),
        runtime_installation="optiq@0.2.18",
        route=ResourceName(route or name),
        activation=activation,
        pinned=pinned,
        options={"context_length": 32768},
    )


class FakeDesiredState:
    def __init__(self, *services: InferenceService) -> None:
        self.items = {str(service.name): service for service in services}

    def service(self, name: str) -> InferenceService | None:
        return self.items.get(name)

    def services(self) -> tuple[InferenceService, ...]:
        return tuple(self.items.values())


class FakeRuntimeSupply:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.error: Exception | None = None
        self.revision = "v1"

    def prepare_launch(
        self, service: InferenceService, host: str, port: int
    ) -> PreparedLaunch:
        self.calls.append((str(service.name), host, port))
        if self.error:
            raise self.error
        return PreparedLaunch(
            argv=("/runtime/bin/server", "--port", str(port)),
            environment={
                "MODEL_ALIAS": str(service.model_alias),
                "REVISION": self.revision,
            },
            required_capabilities=frozenset({"model", "host", "port"}),
            observed_capabilities=frozenset(
                {"model", "host", "port", "context_length"}
            ),
        )


class FakeStateStore:
    def __init__(self) -> None:
        self.operation_items: dict[str, dict[str, object]] = {}
        self.event_items: list[dict[str, object]] = []
        self.snapshot_items: list[dict[str, object]] = []
        self.lifecycle: list[str] = []

    def put_operation(self, item):
        self.operation_items[str(item["id"])] = dict(item)
        return item

    def append_event(self, item):
        self.event_items.append(dict(item))
        return {**item, "sequence": len(self.event_items)}

    def put_snapshot(self, item):
        self.snapshot_items.append(dict(item))
        if item.get("kind") == "service_run":
            self.lifecycle.append(f"persist:{item['state']}")
        return item

    def snapshots(self, kind=None):
        return tuple(
            item for item in self.snapshot_items if kind is None or item["kind"] == kind
        )


@dataclass
class FakeProcess:
    pid: int
    running: bool = True
    ignores_terminate: bool = False
    ignores_kill: bool = False
    terminate_calls: int = 0
    kill_calls: int = 0
    commit_calls: int = 0
    abort_calls: int = 0
    commit_error: Exception | None = None
    lifecycle: list[str] | None = None

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.terminate_calls += 1
        if not self.ignores_terminate:
            self.running = False

    def kill(self):
        self.kill_calls += 1
        if not self.ignores_kill:
            self.running = False

    def wait(self, timeout):
        if self.running:
            raise TimeoutError
        return 0

    def commit(self):
        self.commit_calls += 1
        if self.lifecycle is not None:
            self.lifecycle.append("commit")
        if self.commit_error is not None:
            raise self.commit_error

    def abort(self):
        self.abort_calls += 1


class FakeProcesses:
    def __init__(self) -> None:
        self.next_port = 49152
        self.launched: list[tuple[tuple[str, ...], dict[str, str]]] = []
        self.processes: dict[int, FakeProcess] = {}
        self.attached: list[int] = []
        self.commit_error: Exception | None = None
        self.lifecycle: list[str] | None = None
        self.ignores_terminate = False
        self.ignores_kill = False

    def allocate_loopback_port(self, host: str) -> int:
        port = self.next_port
        self.next_port += 1
        return port

    def launch(self, argv, environment):
        process = FakeProcess(
            1000 + len(self.processes),
            ignores_terminate=self.ignores_terminate,
            ignores_kill=self.ignores_kill,
            commit_error=self.commit_error,
            lifecycle=self.lifecycle,
        )
        self.processes[process.pid] = process
        self.launched.append((tuple(argv), dict(environment)))
        return process

    def attach(self, pid: int):
        self.attached.append(pid)
        return self.processes.get(pid)


class FakeProbe:
    def __init__(self) -> None:
        self.ready = True
        self.identities: dict[int, ProcessIdentity] = {}
        self.identity_error: Exception | None = None

    def identity(self, process: FakeProcess) -> ProcessIdentity:
        if self.identity_error is not None:
            raise self.identity_error
        identity = ProcessIdentity(process.pid, f"birth-{process.pid}")
        self.identities[process.pid] = identity
        return identity

    def identity_matches(self, identity: ProcessIdentity) -> bool:
        return self.identities.get(identity.pid) == identity

    def is_ready(self, endpoint: str, timeout: float) -> bool:
        return self.ready


class FakeGateway:
    def __init__(self) -> None:
        self.running = False
        self.shedding = False
        self.routes: dict[str, tuple[str, str | None]] = {}
        self.calls: list[object] = []
        self.busy_services: set[str] = set()
        self.last_used: dict[str, int] = {}
        self.descriptions = {}

    def start(self):
        self.running = True
        self.calls.append("start")

    def set_route(self, service: str, state: str, endpoint: str | None):
        self.routes[service] = (state, endpoint)
        self.calls.append(("route", service, state, endpoint))

    def describe_route(self, route):
        self.descriptions[route.service] = route

    def remove_route(self, service: str):
        self.routes.pop(service, None)
        self.descriptions.pop(service, None)
        self.calls.append(("remove_route", service))

    def shed_new_work(self, enabled: bool):
        self.shedding = enabled
        self.calls.append(("shed", enabled))

    def effective_route(self, service: str) -> tuple[str, str | None]:
        state, endpoint = self.routes[service]
        if self.shedding and state == "ready":
            return ("unavailable", None)
        return (state, endpoint)

    def is_busy(self, service: str) -> bool:
        return service in self.busy_services

    def last_used_ns(self, service: str) -> int:
        return self.last_used.get(service, 0)

    def drain(self, timeout: float):
        self.calls.append(("drain", timeout))

    def stop(self, timeout: float):
        self.running = False
        self.calls.append(("stop", timeout))


class FakePressure:
    def __init__(self) -> None:
        self.level = PressureLevel.NORMAL

    def current(self) -> PressureLevel:
        return self.level


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000

    def monotonic(self) -> float:
        self.now += 1
        return float(self.now)

    def time_ns(self) -> int:
        self.now += 1
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(1, int(seconds))


class SupervisorTests(unittest.TestCase):
    def test_supervisor_activation_policy_starts_only_selected_services(self) -> None:
        self.desired = FakeDesiredState(
            _service("manual"),
            _service("automatic", activation=ActivationPolicy.SUPERVISOR),
        )
        supervisor = Supervisor(
            desired_state=self.desired,
            runtime_supply=self.runtime,
            state_store=self.store,
            gateway=self.gateway,
            processes=self.processes,
            probe=self.probe,
            memory_pressure=self.pressure,
            clock=self.clock,
            readiness_timeout=3,
            drain_timeout=2,
            terminate_timeout=1,
        )

        status = supervisor.start()

        self.assertEqual(
            {run.service for run in status.runs if run.state is ServiceRunState.READY},
            {"automatic"},
        )
        self.assertEqual(self.gateway.routes["manual"][0], "stopped")

    def test_public_gateway_route_is_distinct_from_service_resource_name(self) -> None:
        service = _service("worker", route="coding")
        self.desired = FakeDesiredState(service)
        supervisor = Supervisor(
            desired_state=self.desired,
            runtime_supply=self.runtime,
            state_store=self.store,
            gateway=self.gateway,
            processes=self.processes,
            probe=self.probe,
            memory_pressure=self.pressure,
            clock=self.clock,
            readiness_timeout=3,
            drain_timeout=2,
            terminate_timeout=1,
        )

        supervisor.start()
        transition = supervisor.start_service("worker")

        self.assertEqual(transition.run.state, ServiceRunState.READY)
        self.assertIn("coding", self.gateway.routes)
        self.assertNotIn("worker", self.gateway.routes)
        route = supervisor.resolve("coding")
        self.assertEqual(route.service, "coding")
        self.assertEqual(route.model, "worker-model")
        self.assertEqual(route.runtime, "optiq@0.2.18")

    def setUp(self) -> None:
        self.desired = FakeDesiredState(_service("coding"), _service("memory"))
        self.runtime = FakeRuntimeSupply()
        self.store = FakeStateStore()
        self.gateway = FakeGateway()
        self.processes = FakeProcesses()
        self.probe = FakeProbe()
        self.pressure = FakePressure()
        self.clock = FakeClock()
        self.supervisor = Supervisor(
            desired_state=self.desired,
            runtime_supply=self.runtime,
            state_store=self.store,
            gateway=self.gateway,
            processes=self.processes,
            probe=self.probe,
            memory_pressure=self.pressure,
            clock=self.clock,
            readiness_timeout=3,
            drain_timeout=2,
            terminate_timeout=1,
        )

    def test_status_is_read_only_and_explicit_start_owns_one_gateway(self) -> None:
        self.assertEqual(self.supervisor.status().state, "stopped")
        self.assertEqual(self.gateway.calls, [])

        started = self.supervisor.start()
        again = self.supervisor.start()

        self.assertEqual(started.state, "running")
        self.assertEqual(again.state, "running")
        self.assertEqual(self.gateway.calls.count("start"), 1)
        self.assertEqual(
            self.gateway.routes,
            {"coding": ("stopped", None), "memory": ("stopped", None)},
        )
        operation = next(iter(self.store.operation_items.values()))
        self.assertEqual(operation["status"], "complete")
        self.assertEqual(operation["outcome"], "running")
        self.assertEqual(self.supervisor._operation_metadata, {})

    def test_service_start_visibly_activates_and_runs_multiple_named_services(self):
        coding = self.supervisor.start_service("coding")
        memory = self.supervisor.start_service("memory")

        self.assertTrue(coding.supervisor_started)
        self.assertFalse(memory.supervisor_started)
        self.assertEqual(coding.run.state, ServiceRunState.READY)
        self.assertNotEqual(coding.run.run_id, memory.run.run_id)
        self.assertNotEqual(coding.run.upstream_port, memory.run.upstream_port)
        self.assertEqual(len(self.processes.launched), 2)
        self.assertEqual(self.gateway.routes["coding"][0], "ready")
        self.assertEqual(self.gateway.routes["memory"][0], "ready")

    def test_service_start_persists_identity_before_committing_runtime_exec(self):
        lifecycle: list[str] = []
        self.store.lifecycle = lifecycle
        self.processes.lifecycle = lifecycle

        result = self.supervisor.start_service("coding")

        self.assertEqual(result.run.state, ServiceRunState.READY)
        self.assertEqual(lifecycle[:2], ["persist:starting", "commit"])
        process = self.processes.processes[result.run.pid]  # type: ignore[index]
        self.assertEqual(process.commit_calls, 1)

    def test_commit_failure_stops_gate_and_records_failed_start(self) -> None:
        self.processes.commit_error = OSError("commit pipe closed")

        result = self.supervisor.start_service("coding")

        process = next(iter(self.processes.processes.values()))
        self.assertEqual(result.run.state, ServiceRunState.FAILED)
        self.assertFalse(process.running)
        self.assertEqual(process.abort_calls, 1)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(self.store.snapshot_items[-1]["state"], "failed")

    def test_commit_and_cleanup_failures_return_a_recoverable_stopping_run(
        self,
    ) -> None:
        self.processes.commit_error = OSError("commit pipe closed")
        self.processes.ignores_terminate = True
        self.processes.ignores_kill = True

        result = self.supervisor.start_service("coding")

        process = next(iter(self.processes.processes.values()))
        self.assertEqual(result.run.state, ServiceRunState.STOPPING)
        self.assertEqual(result.run.pid, process.pid)
        self.assertIn(
            "process launch failed: commit pipe closed", result.run.error or ""
        )
        self.assertIn("cleanup failed:", result.run.error or "")
        operation = self.store.operation_items[result.operation_id]
        self.assertEqual(operation["status"], "failed")
        self.assertEqual(operation["outcome"], "failed")
        self.assertEqual(operation["error"], result.run.error)
        latest = self.store.snapshot_items[-1]
        self.assertEqual(latest["state"], "stopping")
        self.assertEqual(latest["pid"], process.pid)
        self.assertEqual(latest["process_identity"], f"birth-{process.pid}")
        self.assertTrue(process.running)

        process.ignores_kill = False
        recovered = self._new_supervisor().start()

        self.assertEqual(recovered.runs, ())
        self.assertFalse(process.running)
        self.assertEqual(self.store.snapshot_items[-1]["state"], "stopped")

    def test_identity_probe_and_direct_cleanup_failure_preserve_the_live_handle(
        self,
    ) -> None:
        self.probe.identity_error = OSError("identity unavailable")
        self.processes.ignores_terminate = True
        self.processes.ignores_kill = True

        result = self.supervisor.start_service("coding")

        process = next(iter(self.processes.processes.values()))
        self.assertEqual(result.run.state, ServiceRunState.STOPPING)
        self.assertEqual(result.run.pid, process.pid)
        self.assertIn("identity unavailable", result.run.error or "")
        self.assertIn("direct termination", result.run.error or "")
        self.assertTrue(process.running)
        self.assertIs(self.supervisor._runs["coding"].process, process)  # noqa: SLF001

        process.ignores_kill = False
        stopped = self.supervisor.stop_service("coding")

        self.assertEqual(stopped.run.state, ServiceRunState.STOPPED)
        self.assertFalse(process.running)

    def test_starting_snapshot_failure_aborts_without_committing_runtime(self) -> None:
        class FailingStateStore(FakeStateStore):
            def put_snapshot(self, item):
                if (
                    item.get("kind") == "service_run"
                    and item.get("state") == "starting"
                ):
                    raise OSError("journal unavailable")
                return super().put_snapshot(item)

        store = FailingStateStore()
        supervisor = Supervisor(
            desired_state=self.desired,
            runtime_supply=self.runtime,
            state_store=store,
            gateway=self.gateway,
            processes=self.processes,
            probe=self.probe,
            memory_pressure=self.pressure,
            clock=self.clock,
            readiness_timeout=3,
            drain_timeout=2,
            terminate_timeout=1,
        )

        result = supervisor.start_service("coding")

        process = next(iter(self.processes.processes.values()))
        self.assertEqual(result.run.state, ServiceRunState.FAILED)
        self.assertEqual(process.commit_calls, 0)
        self.assertEqual(process.abort_calls, 1)
        self.assertFalse(process.running)

    def test_service_start_claims_one_run_before_launch_preparation(self) -> None:
        supervisor: Supervisor

        class ReentrantRuntime(FakeRuntimeSupply):
            entered = False

            def prepare_launch(self, service, host, port):
                if not self.entered:
                    self.entered = True
                    supervisor.start_service(str(service.name))
                return super().prepare_launch(service, host, port)

        runtime = ReentrantRuntime()
        supervisor = Supervisor(
            desired_state=self.desired,
            runtime_supply=runtime,
            state_store=self.store,
            gateway=self.gateway,
            processes=self.processes,
            probe=self.probe,
            memory_pressure=self.pressure,
            clock=self.clock,
            readiness_timeout=3,
            drain_timeout=2,
            terminate_timeout=1,
        )

        transition = supervisor.start_service("coding")

        self.assertEqual(transition.run.state, ServiceRunState.READY)
        self.assertEqual(len(self.processes.launched), 1)
        self.assertEqual(
            len(
                [
                    item
                    for item in self.store.operation_items.values()
                    if item["kind"] == "service.start"
                ]
            ),
            1,
        )

    def test_capabilities_are_validated_before_process_launch(self) -> None:
        self.runtime.error = CapabilityValidationError("mtp is unavailable")

        result = self.supervisor.start_service("coding")

        self.assertEqual(result.run.state, ServiceRunState.REJECTED)
        self.assertIn("mtp is unavailable", result.run.error or "")
        self.assertEqual(self.processes.launched, [])

        with self.assertRaisesRegex(
            CapabilityValidationError, "exact capabilities: mtp"
        ):
            PreparedLaunch(
                argv=("/runtime/bin/server",),
                required_capabilities=frozenset({"mtp"}),
                observed_capabilities=frozenset({"model"}),
            )

    def test_unexpected_launch_preparation_failure_is_journaled(self) -> None:
        self.runtime.error = OSError("runtime catalogue disappeared")

        with self.assertRaisesRegex(OSError, "catalogue disappeared"):
            self.supervisor.start_service("coding")

        operation = next(
            item
            for item in self.store.operation_items.values()
            if item["kind"] == "service.start"
        )
        self.assertEqual(operation["status"], "failed")
        self.assertEqual(operation["outcome"], "failed")
        self.assertEqual(
            self.supervisor.service_status("coding").state,
            ServiceRunState.FAILED,
        )

    def test_readiness_timeout_forces_bounded_cleanup(self) -> None:
        self.probe.ready = False

        result = self.supervisor.start_service("coding")

        process = next(iter(self.processes.processes.values()))
        self.assertEqual(result.run.state, ServiceRunState.FAILED)
        self.assertEqual(process.terminate_calls, 1)
        self.assertFalse(process.running)

    def test_stale_readiness_cannot_resurrect_a_stopped_run(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingProbe(FakeProbe):
            def is_ready(self, endpoint: str, timeout: float) -> bool:
                del endpoint, timeout
                entered.set()
                release.wait(1)
                return True

        self.probe = BlockingProbe()
        supervisor = self._new_supervisor()
        transitions: list[object] = []
        starter = threading.Thread(
            target=lambda: transitions.append(supervisor.start_service("coding"))
        )
        starter.start()
        self.assertTrue(entered.wait(1))

        stopped = supervisor.stop_service("coding")
        release.set()
        starter.join(1)

        self.assertFalse(starter.is_alive())
        self.assertEqual(len(transitions), 1)
        self.assertEqual(stopped.run.state, ServiceRunState.STOPPED)
        self.assertEqual(
            supervisor.service_status("coding").state,
            ServiceRunState.STOPPED,
        )
        self.assertEqual(self.gateway.routes["coding"], ("stopped", None))

    def test_stop_restart_and_supervisor_shutdown_are_bounded_and_journaled(self):
        first = self.supervisor.start_service("coding")
        self.processes.processes[first.run.pid].ignores_terminate = True

        restarted = self.supervisor.restart_service("coding")
        stopped = self.supervisor.stop()

        self.assertNotEqual(first.run.run_id, restarted.run.run_id)
        self.assertEqual(
            self.processes.processes[first.run.pid].kill_calls,
            1,
        )
        self.assertEqual(stopped.state, "stopped")
        self.assertFalse(self.gateway.running)
        self.assertIn(("drain", 2), self.gateway.calls)

    def test_unconfirmed_termination_preserves_owned_process_identity(self) -> None:
        transition = self.supervisor.start_service("coding")
        process = self.processes.processes[transition.run.pid]  # type: ignore[index]
        process.ignores_terminate = True
        process.ignores_kill = True

        with self.assertRaisesRegex(RuntimeError, "could not be confirmed stopped"):
            self.supervisor.stop_service("coding")

        current = self.supervisor.service_status("coding")
        self.assertEqual(current.state, ServiceRunState.STOPPING)
        latest = self.store.snapshot_items[-1]
        self.assertEqual(latest["process_identity"], f"birth-{process.pid}")
        self.assertTrue(process.running)

    def test_remove_uses_live_run_after_desired_state_was_committed_absent(
        self,
    ) -> None:
        transition = self.supervisor.start_service("coding")
        process = self.processes.processes[transition.run.pid]  # type: ignore[index]
        self.desired.items.pop("coding")

        removed = self.supervisor.remove_service("coding", previous_route="coding")

        self.assertEqual(removed.run.state, ServiceRunState.STOPPED)
        self.assertFalse(process.running)
        self.assertNotIn("coding", self.gateway.routes)

    def test_remove_cleans_stopped_route_after_desired_state_was_committed_absent(
        self,
    ) -> None:
        self.supervisor.start()
        self.desired.items.pop("coding")

        removed = self.supervisor.remove_service("coding", previous_route="coding")

        self.assertEqual(removed.run.state, ServiceRunState.STOPPED)
        self.assertNotIn("coding", self.gateway.routes)

    def test_supervisor_stop_cleans_up_every_component_after_one_failure(self) -> None:
        coding = self.supervisor.start_service("coding")
        memory = self.supervisor.start_service("memory")
        stuck = self.processes.processes[coding.run.pid]  # type: ignore[index]
        sibling = self.processes.processes[memory.run.pid]  # type: ignore[index]
        stuck.ignores_terminate = True
        stuck.ignores_kill = True

        with self.assertRaisesRegex(RuntimeError, "Supervisor stop failed"):
            self.supervisor.stop()

        self.assertFalse(sibling.running)
        self.assertIn(("stop", 2), self.gateway.calls)
        self.assertEqual(self.supervisor.status().state, "failed")
        operation = [
            item
            for item in self.store.operation_items.values()
            if item["kind"] == "supervisor.stop"
        ][-1]
        self.assertEqual(operation["status"], "failed")
        self.assertEqual(operation["outcome"], "failed")

    def test_supervisor_restart_restores_gateway_admission_for_ready_service(self):
        self.supervisor.start_service("coding")

        self.supervisor.restart()
        restarted = self.supervisor.start_service("coding")

        self.assertEqual(restarted.run.state, ServiceRunState.READY)
        self.assertEqual(
            self.gateway.effective_route("coding"),
            ("ready", "http://127.0.0.1:49153"),
        )

    def test_service_drain_rejects_new_route_work_and_waits_for_idle(self) -> None:
        supervisor = self.supervisor
        supervisor.start_service("coding")
        route = "coding"
        self.gateway.busy_services.add(route)
        original_sleep = self.clock.sleep

        def become_idle(seconds: float) -> None:
            self.gateway.busy_services.discard(route)
            original_sleep(seconds)

        self.clock.sleep = become_idle  # type: ignore[method-assign]
        drained = supervisor.drain_service("coding")

        self.assertEqual(drained.state, "drained")
        self.assertEqual(drained.route, route)
        self.assertEqual(self.gateway.routes[route][0], "unavailable")

    def test_service_drain_times_out_without_stopping_a_busy_process(self) -> None:
        supervisor = self.supervisor
        transition = supervisor.start_service("coding")
        self.gateway.busy_services.add("coding")

        with self.assertRaisesRegex(RuntimeError, "active request"):
            supervisor.drain_service("coding")

        self.assertEqual(
            supervisor.service_status("coding").state, ServiceRunState.READY
        )
        self.assertEqual(
            self.processes.processes[transition.run.pid].terminate_calls,
            0,  # type: ignore[index]
        )
        self.assertTrue(self.store.operation_items)
        operation_ids = set(self.store.operation_items)
        self.assertTrue(
            all(
                event["operation_id"] in operation_ids
                for event in self.store.event_items
            )
        )

    def test_service_removal_drops_the_stopped_gateway_route(self) -> None:
        self.supervisor.start_service("coding")

        removed = self.supervisor.remove_service("coding")

        self.assertEqual(removed.run.state, ServiceRunState.STOPPED)
        self.assertNotIn("coding", self.gateway.routes)

    def test_service_removal_drops_the_public_route_not_the_resource_name(self) -> None:
        service = _service("worker", route="coding-route")
        supervisor = Supervisor(
            desired_state=FakeDesiredState(service),
            runtime_supply=self.runtime,
            state_store=self.store,
            gateway=self.gateway,
            processes=self.processes,
            probe=self.probe,
            memory_pressure=self.pressure,
            clock=self.clock,
            readiness_timeout=3,
            drain_timeout=2,
            terminate_timeout=1,
        )
        supervisor.start_service("worker")

        supervisor.remove_service("worker")

        self.assertNotIn("coding-route", self.gateway.routes)
        self.assertIn(("remove_route", "coding-route"), self.gateway.calls)

    def test_one_service_failure_does_not_stop_another(self) -> None:
        coding = self.supervisor.start_service("coding")
        memory = self.supervisor.start_service("memory")
        self.processes.processes[coding.run.pid].running = False

        failed = self.supervisor.service_status("coding")
        healthy = self.supervisor.service_status("memory")

        self.assertEqual(failed.state, ServiceRunState.FAILED)
        self.assertEqual(healthy.state, ServiceRunState.READY)
        self.assertTrue(self.processes.processes[memory.run.pid].running)

    def test_recovery_attaches_only_when_persisted_process_identity_matches(self):
        identity = ProcessIdentity(1234, "birth-1234")
        process = FakeProcess(1234)
        self.processes.processes[1234] = process
        self.probe.identities[1234] = identity
        process.commit()
        launch = self.runtime.prepare_launch(
            self.desired.service("coding"),
            "127.0.0.1",
            49199,  # type: ignore[arg-type]
        )
        self.store.snapshot_items.append(
            {
                "kind": "service_run",
                "id": "coding/run-old",
                "version": 1,
                "service": "coding",
                "run_id": "run-old",
                "state": "starting",
                "pid": 1234,
                "process_identity": "birth-1234",
                "launch_identity": _launch_identity(launch),
                "upstream_port": 49199,
            }
        )

        recovered = self.supervisor.start().runs[0]

        self.assertEqual(recovered.run_id, "run-old")
        self.assertEqual(recovered.state, ServiceRunState.READY)
        self.assertEqual(self.processes.attached, [1234])

        self.processes.attached.clear()
        self.probe.identities[1234] = ProcessIdentity(1234, "reused-pid")
        other = self._new_supervisor()
        other.start()
        self.assertEqual(self.processes.attached, [])
        self.assertTrue(process.running)
        self.assertEqual(self.store.snapshot_items[-1]["state"], "stopped")
        self.assertIn("no longer live", self.store.snapshot_items[-1]["error"])

    def test_recovery_requires_current_launch_identity_and_live_readiness(self) -> None:
        identity = ProcessIdentity(1234, "birth-1234")
        process = FakeProcess(1234)
        self.processes.processes[1234] = process
        self.probe.identities[1234] = identity
        service = self.desired.service("coding")
        launch = self.runtime.prepare_launch(service, "127.0.0.1", 49199)  # type: ignore[arg-type]
        snapshot = {
            "kind": "service_run",
            "id": "coding/run-old",
            "version": 1,
            "service": "coding",
            "run_id": "run-old",
            "state": "ready",
            "pid": 1234,
            "process_identity": "birth-1234",
            "launch_identity": _launch_identity(launch),
            "upstream_port": 49199,
        }
        self.store.snapshot_items.append(snapshot)
        self.probe.ready = False

        not_ready = self._new_supervisor().start()

        self.assertEqual(not_ready.runs, ())
        self.assertFalse(process.running)

        replacement = FakeProcess(2345)
        self.processes.processes[2345] = replacement
        replacement_identity = ProcessIdentity(2345, "birth-2345")
        self.probe.identities[2345] = replacement_identity
        self.probe.ready = True
        self.runtime.revision = "v2"
        changed = {**snapshot, "pid": 2345, "process_identity": "birth-2345"}
        self.store.snapshot_items.append(changed)

        mismatched = self._new_supervisor().start()

        self.assertEqual(mismatched.runs, ())
        self.assertFalse(replacement.running)

    def test_recovery_retries_cleanup_when_a_live_child_cannot_initially_stop(
        self,
    ) -> None:
        identity = ProcessIdentity(1234, "birth-1234")
        process = FakeProcess(1234, ignores_terminate=True, ignores_kill=True)
        self.processes.processes[1234] = process
        self.probe.identities[1234] = identity
        service = self.desired.service("coding")
        launch = self.runtime.prepare_launch(service, "127.0.0.1", 49199)  # type: ignore[arg-type]
        self.store.snapshot_items.append(
            {
                "kind": "service_run",
                "id": "coding/run-old",
                "version": 1,
                "service": "coding",
                "run_id": "run-old",
                "state": "ready",
                "pid": 1234,
                "process_identity": "birth-1234",
                "launch_identity": _launch_identity(launch),
                "upstream_port": 49199,
            }
        )
        self.probe.ready = False

        with self.assertRaisesRegex(RuntimeError, "could not be confirmed stopped"):
            self._new_supervisor().start()

        latest = self.store.snapshot_items[-1]
        self.assertEqual(latest["state"], "stopping")
        self.assertEqual(latest["pid"], 1234)
        process.ignores_kill = False

        recovered = self._new_supervisor().start()

        self.assertEqual(recovered.runs, ())
        self.assertFalse(process.running)
        self.assertEqual(self.store.snapshot_items[-1]["state"], "stopped")

    def test_recovery_stops_and_records_a_verified_undesired_child(self) -> None:
        service = self.desired.service("coding")
        launch = self.runtime.prepare_launch(service, "127.0.0.1", 49199)  # type: ignore[arg-type]
        process = FakeProcess(1234)
        self.processes.processes[1234] = process
        self.probe.identities[1234] = ProcessIdentity(1234, "birth-1234")
        self.store.snapshot_items.append(
            {
                "kind": "service_run",
                "id": "coding/run-old",
                "version": 1,
                "service": "coding",
                "run_id": "run-old",
                "state": "ready",
                "pid": 1234,
                "process_identity": "birth-1234",
                "launch_identity": _launch_identity(launch),
                "upstream_port": 49199,
            }
        )
        self.desired.items.pop("coding")

        status = self.supervisor.start()

        self.assertNotIn("coding", {run.service for run in status.runs})
        self.assertEqual(self.processes.attached, [1234])
        self.assertFalse(process.running)
        final = [
            item for item in self.store.snapshot_items if item["id"] == "coding/run-old"
        ][-1]
        self.assertEqual(final["state"], "stopped")
        self.assertNotIn("pid", final)

    def test_critical_pressure_stops_lru_idle_unpinned_but_never_pinned_or_busy(self):
        self.desired.items["pinned"] = _service("pinned", pinned=True)
        self.supervisor.start_service("coding")
        self.supervisor.start_service("memory")
        self.supervisor.start_service("pinned")
        self.gateway.last_used = {"coding": 10, "memory": 20, "pinned": 1}
        self.gateway.busy_services = {"memory"}
        self.pressure.level = PressureLevel.CRITICAL

        result = self.supervisor.reconcile_pressure()

        self.assertEqual(result.stopped_services, ("coding",))
        self.assertEqual(result.operator_stop_sequence, ("memory", "pinned"))
        self.assertEqual(
            self.supervisor.service_status("pinned").state, ServiceRunState.READY
        )
        self.assertEqual(
            self.supervisor.service_status("memory").state, ServiceRunState.READY
        )
        self.assertIn(("shed", True), self.gateway.calls)
        blocked = self.supervisor.start_service("coding")
        self.assertEqual(blocked.run.state, ServiceRunState.REJECTED)

    def test_gateway_route_lookup_never_starts_stopped_service(self) -> None:
        self.supervisor.start()
        before = len(self.processes.launched)

        route = self.supervisor.resolve("coding")

        self.assertEqual(route.state, "stopped")
        self.assertEqual(len(self.processes.launched), before)

    def test_maintenance_detects_exit_and_registers_new_desired_route(self) -> None:
        transition = self.supervisor.start_service("coding")
        self.processes.processes[transition.run.pid].running = False  # type: ignore[index]
        self.desired.items["new"] = _service("new")

        outcome = self.supervisor.maintain()

        self.assertEqual(
            self.supervisor.service_status("coding").state, ServiceRunState.FAILED
        )
        self.assertEqual(self.gateway.routes["new"], ("stopped", None))
        self.assertEqual(outcome.pressure, PressureLevel.NORMAL)

    def test_maintenance_activates_a_new_supervisor_owned_service(self) -> None:
        self.supervisor.start()
        self.desired.items["automatic"] = _service(
            "automatic", activation=ActivationPolicy.SUPERVISOR
        )

        outcome = self.supervisor.maintain()

        self.assertEqual(outcome.state, "running")
        self.assertEqual(
            self.supervisor.service_status("automatic").state,
            ServiceRunState.READY,
        )
        self.assertEqual(self.gateway.routes["automatic"][0], "ready")

    def test_maintenance_restarts_a_running_service_after_desired_edit(self) -> None:
        first = self.supervisor.start_service("coding")
        self.desired.items["coding"] = _service("coding", route="coding-v2")

        outcome = self.supervisor.maintain()

        current = self.supervisor.service_status("coding")
        self.assertEqual(current.state, ServiceRunState.READY)
        self.assertNotEqual(current.run_id, first.run.run_id)
        self.assertNotIn("coding", self.gateway.routes)
        self.assertEqual(self.gateway.routes["coding-v2"][0], "ready")
        self.assertEqual(outcome.restarted_services, ("coding",))

    def test_maintenance_closes_the_old_route_before_waiting_to_replace(self) -> None:
        class DrainAwareGateway(FakeGateway):
            def is_busy(self, service: str) -> bool:
                state, _endpoint = self.routes[service]
                if state != "unavailable":
                    raise AssertionError("replacement inspected an admitting route")
                return super().is_busy(service)

        self.gateway = DrainAwareGateway()
        supervisor = self._new_supervisor()
        supervisor.start_service("coding")
        self.desired.items["coding"] = _service("coding", route="coding-v2")

        outcome = supervisor.maintain()

        self.assertEqual(outcome.restarted_services, ("coding",))
        self.assertEqual(self.gateway.routes["coding-v2"][0], "ready")

    def test_maintenance_restarts_idle_run_after_exact_launch_target_update(
        self,
    ) -> None:
        first = self.supervisor.start_service("coding")
        self.runtime.revision = "v2"

        outcome = self.supervisor.maintain()

        current = self.supervisor.service_status("coding")
        self.assertEqual(current.state, ServiceRunState.READY)
        self.assertNotEqual(current.run_id, first.run.run_id)
        self.assertEqual(outcome.restarted_services, ("coding",))

    def _new_supervisor(self):
        candidate = Supervisor(
            desired_state=self.desired,
            runtime_supply=self.runtime,
            state_store=self.store,
            gateway=self.gateway,
            processes=self.processes,
            probe=self.probe,
            memory_pressure=self.pressure,
            clock=self.clock,
        )
        return candidate


if __name__ == "__main__":
    unittest.main()
