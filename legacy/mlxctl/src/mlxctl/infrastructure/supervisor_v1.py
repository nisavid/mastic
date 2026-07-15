"""Supported-v1 Supervisor for named local Inference Services.

The implementation is deliberately expressed in injected process, probe,
Gateway, desired-state, runtime-supply, persistence, pressure, and clock ports.
It owns orchestration and policy; adapters own operating-system details.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence

from mlxctl.domain.admission import PressureLevel
from mlxctl.domain.resources import ActivationPolicy, InferenceService, ServiceRunState
from mlxctl.infrastructure.gateway import GatewayRoute


class CapabilityValidationError(ValueError):
    """An exact Runtime Installation cannot satisfy a requested launch."""


class ServiceNotFoundError(KeyError):
    """Desired state has no Inference Service with the requested name."""


@dataclass(frozen=True, slots=True)
class PreparedLaunch:
    """An exact, capability-validated runtime launch."""

    argv: tuple[str, ...]
    environment: Mapping[str, str] = field(default_factory=dict)
    required_capabilities: frozenset[str] = frozenset()
    observed_capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.argv or not self.argv[0]:
            raise ValueError("runtime launch requires a non-empty argv")
        missing = self.required_capabilities - self.observed_capabilities
        if missing:
            raise CapabilityValidationError(
                "runtime installation lacks exact capabilities: "
                + ", ".join(sorted(missing))
            )
        object.__setattr__(
            self, "environment", MappingProxyType(dict(self.environment))
        )


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """PID plus an operating-system birth token used to detect PID reuse."""

    pid: int
    birth_token: str

    def __post_init__(self) -> None:
        if self.pid <= 0 or not self.birth_token:
            raise ValueError("process identity requires a PID and birth token")


@dataclass(frozen=True, slots=True)
class ServiceRunStatus:
    """Observed state for one concrete activation of an Inference Service."""

    service: str
    run_id: str
    state: ServiceRunState
    upstream_port: int | None = None
    pid: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ServiceTransition:
    """A correlated lifecycle result suitable for CLI, TUI, and protocol use."""

    operation_id: str
    run: ServiceRunStatus
    supervisor_started: bool = False


@dataclass(frozen=True, slots=True)
class ServiceDrainStatus:
    """Result of preventing new work and waiting for one route to become idle."""

    service: str
    route: str
    state: str = "drained"


@dataclass(frozen=True, slots=True)
class SupervisorStatus:
    """Read-only Supervisor observation."""

    state: str
    runs: tuple[ServiceRunStatus, ...] = ()
    shedding_new_work: bool = False
    operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class PressureOutcome:
    """Visible result of one memory-pressure reconciliation."""

    level: PressureLevel
    shedding_new_work: bool
    stopped_services: tuple[str, ...] = ()
    operator_stop_plan: tuple[str, ...] = ()


class DesiredState(Protocol):
    def service(self, name: str) -> InferenceService | None: ...

    def services(self) -> Sequence[InferenceService]: ...


class RuntimeSupply(Protocol):
    def prepare_launch(
        self, service: InferenceService, host: str, port: int
    ) -> PreparedLaunch: ...


class OperationalState(Protocol):
    def put_operation(self, operation: Mapping[str, object]) -> object: ...

    def append_event(self, event: Mapping[str, object]) -> object: ...

    def put_snapshot(self, snapshot: Mapping[str, object]) -> object: ...

    def snapshots(self, kind: str | None = None) -> Sequence[Mapping[str, object]]: ...


class ManagedProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float) -> int: ...


class ProcessLauncher(Protocol):
    def allocate_loopback_port(self, host: str) -> int: ...

    def launch(
        self, argv: Sequence[str], environment: Mapping[str, str]
    ) -> ManagedProcess: ...

    def attach(self, pid: int) -> ManagedProcess | None: ...


class ProcessProbe(Protocol):
    def identity(self, process: ManagedProcess) -> ProcessIdentity: ...

    def identity_matches(self, identity: ProcessIdentity) -> bool: ...

    def is_ready(self, endpoint: str, timeout: float) -> bool: ...


class GatewayRunner(Protocol):
    def start(self) -> None: ...

    def set_route(self, service: str, state: str, endpoint: str | None) -> None: ...

    def describe_route(self, route: GatewayRoute) -> None: ...

    def remove_route(self, service: str) -> None: ...

    def shed_new_work(self, enabled: bool) -> None: ...

    def is_busy(self, service: str) -> bool: ...

    def last_used_ns(self, service: str) -> int: ...

    def drain(self, timeout: float) -> None: ...

    def stop(self, timeout: float) -> None: ...


class MemoryPressure(Protocol):
    def current(self) -> PressureLevel: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def time_ns(self) -> int: ...

    def sleep(self, seconds: float) -> None: ...


@dataclass(slots=True)
class _Run:
    status: ServiceRunStatus
    service: InferenceService
    process: ManagedProcess | None = None
    identity: ProcessIdentity | None = None


class Supervisor:
    """Own one Gateway and concurrent named one-model Service Runs."""

    def __init__(
        self,
        *,
        desired_state: DesiredState,
        runtime_supply: RuntimeSupply,
        state_store: OperationalState,
        gateway: GatewayRunner,
        processes: ProcessLauncher,
        probe: ProcessProbe,
        memory_pressure: MemoryPressure,
        clock: Clock,
        readiness_timeout: float = 30.0,
        readiness_poll_interval: float = 0.05,
        drain_timeout: float = 10.0,
        terminate_timeout: float = 5.0,
        kill_timeout: float = 2.0,
    ) -> None:
        if (
            min(
                readiness_timeout,
                readiness_poll_interval,
                drain_timeout,
                terminate_timeout,
                kill_timeout,
            )
            <= 0
        ):
            raise ValueError("Supervisor timeouts must be positive")
        self._desired_state = desired_state
        self._runtime_supply = runtime_supply
        self._state_store = state_store
        self._gateway = gateway
        self._processes = processes
        self._probe = probe
        self._memory_pressure = memory_pressure
        self._clock = clock
        self._readiness_timeout = readiness_timeout
        self._readiness_poll_interval = readiness_poll_interval
        self._drain_timeout = drain_timeout
        self._terminate_timeout = terminate_timeout
        self._kill_timeout = kill_timeout
        self._lock = threading.RLock()
        self._state = "stopped"
        self._runs: dict[str, _Run] = {}
        self._sequence = 0
        self._shedding_new_work = False
        self._operation_metadata: dict[str, tuple[str, str]] = {}

    def status(self) -> SupervisorStatus:
        """Observe persisted/live state without activating the Supervisor."""

        with self._lock:
            self._refresh_exits_locked()
            return self._status_locked()

    def start(self) -> SupervisorStatus:
        """Explicitly start the one Gateway and recover verified child identities."""

        activate: tuple[str, ...] = ()
        with self._lock:
            if self._state == "running":
                return self._status_locked()
            self._state = "starting"
            operation_id = self._begin_operation_locked(
                "supervisor.start", "supervisor"
            )
            try:
                self._gateway.start()
                for service in self._desired_state.services():
                    route = self._gateway_route(service)
                    self._gateway.describe_route(
                        GatewayRoute(
                            service=route,
                            state="stopped",
                            model=str(service.model_alias),
                            runtime=service.runtime_installation,
                        )
                    )
                    self._gateway.set_route(route, "stopped", None)
                self._recover_runs_locked()
                activate = tuple(
                    str(service.name)
                    for service in self._desired_state.services()
                    if service.activation is ActivationPolicy.SUPERVISOR
                    and str(service.name) not in self._runs
                )
            except Exception as error:
                self._state = "failed"
                self._finish_operation_locked(operation_id, "failed", error=str(error))
                raise
            self._state = "running"
            self._finish_operation_locked(operation_id, "running")
        for name in activate:
            self.start_service(name)
        with self._lock:
            return self._status_locked(operation_id=operation_id)

    def restart(self) -> SupervisorStatus:
        """Explicitly drain and reconstruct Supervisor-owned live state."""

        self.stop()
        return self.start()

    def stop(self) -> SupervisorStatus:
        """Drain the Gateway, stop every run, stop the Gateway, and fully exit."""

        with self._lock:
            if self._state == "stopped":
                return self._status_locked()
            self._state = "stopping"
            operation_id = self._begin_operation_locked("supervisor.stop", "supervisor")
            self._set_shedding_locked(True)
            self._gateway.drain(self._drain_timeout)
            names = tuple(self._runs)
        for name in names:
            self.stop_service(name)
        with self._lock:
            self._gateway.stop(self._drain_timeout)
            self._state = "stopped"
            self._shedding_new_work = False
            self._finish_operation_locked(operation_id, "stopped")
            return self._status_locked(operation_id=operation_id)

    def start_service(self, name: str) -> ServiceTransition:
        """Start one named service; this mutation may visibly start Supervisor."""

        service = self._desired_state.service(name)
        if service is None:
            raise ServiceNotFoundError(name)
        with self._lock:
            self._refresh_exits_locked()
            current = self._runs.get(name)
            if current is not None and current.status.state in {
                ServiceRunState.STARTING,
                ServiceRunState.READY,
                ServiceRunState.UNHEALTHY,
            }:
                return ServiceTransition("none", current.status)
            if self._memory_pressure.current() is PressureLevel.CRITICAL:
                operation_id = self._begin_operation_locked("service.start", name)
                rejected = ServiceRunStatus(
                    name,
                    self._new_identity_locked("run"),
                    ServiceRunState.REJECTED,
                    error="critical memory pressure blocks new Service Runs",
                )
                self._finish_operation_locked(operation_id, rejected.state.value)
                return ServiceTransition(operation_id, rejected)
            needs_start = self._state != "running"
        if needs_start:
            self.start()
        with self._lock:
            operation_id = self._begin_operation_locked("service.start", name)
            run_id = self._new_identity_locked("run")
            try:
                port = self._processes.allocate_loopback_port("127.0.0.1")
                launch = self._runtime_supply.prepare_launch(service, "127.0.0.1", port)
            except (CapabilityValidationError, ValueError) as error:
                rejected = ServiceRunStatus(
                    name,
                    run_id,
                    ServiceRunState.REJECTED,
                    error=str(error),
                )
                self._runs[name] = _Run(rejected, service)
                self._gateway.set_route(
                    self._gateway_route(service), "unavailable", None
                )
                self._persist_run_locked(self._runs[name])
                self._finish_operation_locked(operation_id, rejected.state.value)
                return ServiceTransition(operation_id, rejected, needs_start)

            starting = ServiceRunStatus(
                name, run_id, ServiceRunState.STARTING, upstream_port=port
            )
            run = _Run(starting, service)
            self._runs[name] = run
            self._gateway.set_route(self._gateway_route(service), "unavailable", None)
            self._event_locked(operation_id, "launch_prepared", run_id=run_id)
            try:
                process = self._processes.launch(launch.argv, launch.environment)
                run.process = process
                identity = self._probe.identity(process)
            except Exception as error:
                if run.process is not None and run.identity is None:
                    self._terminate_direct_locked(run.process)
                return self._failed_start_locked(
                    run, operation_id, f"process launch failed: {error}", needs_start
                )
            run.identity = identity
            run.status = ServiceRunStatus(
                name,
                run_id,
                ServiceRunState.STARTING,
                upstream_port=port,
                pid=process.pid,
            )
            self._persist_run_locked(run)

        deadline = self._clock.monotonic() + self._readiness_timeout
        endpoint = f"http://127.0.0.1:{port}"
        while self._clock.monotonic() < deadline:
            with self._lock:
                if process.poll() is not None:
                    return self._failed_start_locked(
                        run,
                        operation_id,
                        "runtime exited before readiness",
                        needs_start,
                    )
            try:
                ready = self._probe.is_ready(endpoint, self._readiness_poll_interval)
            except Exception:
                ready = False
            if ready:
                with self._lock:
                    run.status = ServiceRunStatus(
                        name,
                        run_id,
                        ServiceRunState.READY,
                        upstream_port=port,
                        pid=process.pid,
                    )
                    self._gateway.set_route(
                        self._gateway_route(service), "ready", endpoint
                    )
                    self._persist_run_locked(run)
                    self._finish_operation_locked(operation_id, "ready")
                    return ServiceTransition(operation_id, run.status, needs_start)
            self._clock.sleep(self._readiness_poll_interval)
        with self._lock:
            self._terminate_locked(run)
            return self._failed_start_locked(
                run, operation_id, "readiness timed out", needs_start
            )

    def stop_service(self, name: str) -> ServiceTransition:
        """Stop one Service Run without disturbing sibling runs."""

        with self._lock:
            service = self._desired_state.service(name)
            if service is None:
                raise ServiceNotFoundError(name)
            run = self._runs.get(name)
            operation_id = self._begin_operation_locked("service.stop", name)
            if run is None or run.status.state in {
                ServiceRunState.STOPPED,
                ServiceRunState.REJECTED,
                ServiceRunState.FAILED,
            }:
                status = (
                    run.status
                    if run is not None
                    else ServiceRunStatus(
                        name, self._new_identity_locked("run"), ServiceRunState.STOPPED
                    )
                )
                self._gateway.set_route(self._gateway_route(service), "stopped", None)
                self._finish_operation_locked(operation_id, "stopped")
                return ServiceTransition(operation_id, status)
            run.status = ServiceRunStatus(
                name,
                run.status.run_id,
                ServiceRunState.STOPPING,
                upstream_port=run.status.upstream_port,
                pid=run.status.pid,
            )
            self._gateway.set_route(self._gateway_route(service), "unavailable", None)
            self._persist_run_locked(run)
            self._terminate_locked(run)
            run.status = ServiceRunStatus(
                name, run.status.run_id, ServiceRunState.STOPPED
            )
            self._gateway.set_route(self._gateway_route(service), "stopped", None)
            self._persist_run_locked(run)
            self._finish_operation_locked(operation_id, "stopped")
            return ServiceTransition(operation_id, run.status)

    def drain_service(self, name: str) -> ServiceDrainStatus:
        """Reject new work for one route and wait boundedly for active work."""

        service = self._desired_state.service(name)
        if service is None:
            raise ServiceNotFoundError(name)
        route = self._gateway_route(service)
        with self._lock:
            run = self._runs.get(name)
            endpoint = (
                f"http://127.0.0.1:{run.status.upstream_port}"
                if run is not None and run.status.upstream_port is not None
                else None
            )
            self._gateway.set_route(route, "unavailable", endpoint)
        deadline = self._clock.monotonic() + self._drain_timeout
        while self._gateway.is_busy(route):
            if self._clock.monotonic() >= deadline:
                raise RuntimeError(
                    f"Inference Service {name!r} is still serving an active request"
                )
            self._clock.sleep(self._readiness_poll_interval)
        return ServiceDrainStatus(name, route)

    def restart_service(self, name: str) -> ServiceTransition:
        """Create a new Service Run after a bounded stop of the previous run."""

        self.stop_service(name)
        return self.start_service(name)

    def remove_service(self, name: str) -> ServiceTransition:
        """Drain and stop one service, then remove its live Gateway route."""

        service = self._desired_state.service(name)
        if service is None:
            raise ServiceNotFoundError(name)
        self.drain_service(name)
        stopped = self.stop_service(name)
        with self._lock:
            self._gateway.remove_route(name)
            self._runs.pop(name, None)
        return stopped

    def service_status(self, name: str) -> ServiceRunStatus:
        """Observe one service without activating it."""

        service = self._desired_state.service(name)
        if service is None:
            raise ServiceNotFoundError(name)
        with self._lock:
            self._refresh_exits_locked()
            run = self._runs.get(name)
            if run is None:
                return ServiceRunStatus(name, "none", ServiceRunState.STOPPED)
            return run.status

    def reconcile_pressure(self) -> PressureOutcome:
        """Shed work and evict only LRU idle unpinned runs under critical pressure."""

        level = self._memory_pressure.current()
        with self._lock:
            self._refresh_exits_locked()
            if level is not PressureLevel.CRITICAL:
                self._set_shedding_locked(False)
                return PressureOutcome(level, False)
            self._set_shedding_locked(True)
            active = [
                run
                for run in self._runs.values()
                if run.status.state
                in {
                    ServiceRunState.STARTING,
                    ServiceRunState.READY,
                    ServiceRunState.UNHEALTHY,
                }
            ]
            candidates = sorted(
                (
                    run
                    for run in active
                    if not run.service.pinned
                    and not self._gateway.is_busy(self._gateway_route(run.service))
                ),
                key=lambda run: self._gateway.last_used_ns(
                    self._gateway_route(run.service)
                ),
            )
        stopped: list[str] = []
        for run in candidates:
            self.stop_service(run.status.service)
            stopped.append(run.status.service)
            if self._memory_pressure.current() is not PressureLevel.CRITICAL:
                with self._lock:
                    self._set_shedding_locked(False)
                return PressureOutcome(
                    self._memory_pressure.current(), False, tuple(stopped)
                )
        with self._lock:
            remaining = [
                run
                for run in self._runs.values()
                if run.status.state
                in {
                    ServiceRunState.STARTING,
                    ServiceRunState.READY,
                    ServiceRunState.UNHEALTHY,
                }
            ]
            plan = tuple(
                run.status.service
                for run in sorted(
                    remaining,
                    key=lambda run: (
                        run.service.pinned,
                        self._gateway.last_used_ns(self._gateway_route(run.service)),
                    ),
                )
            )
            operation_id = self._begin_operation_locked("pressure.reconcile", "memory")
            self._event_locked(
                operation_id,
                "pressure_decision",
                stopped_services=stopped,
                operator_stop_plan=plan,
            )
            self._finish_operation_locked(operation_id, "complete")
            return PressureOutcome(level, True, tuple(stopped), plan)

    def list_routes(self) -> tuple[GatewayRoute, ...]:
        """Return Gateway routes without starting stopped services."""

        return tuple(
            self.resolve(str(service.route))
            for service in self._desired_state.services()
        )

    def resolve(self, service: str) -> GatewayRoute | None:
        """Resolve current route state; resolution never activates a service."""

        desired = next(
            (
                item
                for item in self._desired_state.services()
                if str(item.route) == service
            ),
            None,
        )
        if desired is None:
            return None
        status = self.service_status(str(desired.name))
        endpoint = None
        route_state = "stopped"
        if status.state is ServiceRunState.READY and status.upstream_port is not None:
            route_state = "ready"
            endpoint = f"http://127.0.0.1:{status.upstream_port}"
        elif status.state not in {ServiceRunState.STOPPED, ServiceRunState.REJECTED}:
            route_state = "unavailable"
        return GatewayRoute(
            service=service,
            state=route_state,
            endpoint=endpoint,
            model=str(desired.model_alias),
            runtime=desired.runtime_installation,
        )

    def _status_locked(self, *, operation_id: str | None = None) -> SupervisorStatus:
        return SupervisorStatus(
            self._state,
            tuple(
                sorted(
                    (run.status for run in self._runs.values()),
                    key=lambda item: item.service,
                )
            ),
            self._shedding_new_work,
            operation_id,
        )

    def _recover_runs_locked(self) -> None:
        snapshots = self._state_store.snapshots("service_run")
        latest: dict[str, Mapping[str, object]] = {}
        for snapshot in snapshots:
            identifier = snapshot.get("id")
            if isinstance(identifier, str):
                latest[identifier] = snapshot
        for snapshot in latest.values():
            if snapshot.get("state") not in {"starting", "ready", "unhealthy"}:
                continue
            service_name = snapshot.get("service")
            pid = snapshot.get("pid")
            birth_token = snapshot.get("process_identity")
            port = snapshot.get("upstream_port")
            run_id = snapshot.get("run_id")
            if not (
                isinstance(service_name, str)
                and isinstance(pid, int)
                and isinstance(birth_token, str)
                and isinstance(port, int)
                and isinstance(run_id, str)
            ):
                continue
            service = self._desired_state.service(service_name)
            identity = ProcessIdentity(pid, birth_token)
            if service is None or not self._probe.identity_matches(identity):
                continue
            process = self._processes.attach(pid)
            if (
                process is None
                or process.poll() is not None
                or not self._probe.identity_matches(identity)
            ):
                continue
            status = ServiceRunStatus(
                service_name, run_id, ServiceRunState.READY, port, pid
            )
            self._runs[service_name] = _Run(status, service, process, identity)
            self._gateway.set_route(
                self._gateway_route(service),
                "ready",
                f"http://127.0.0.1:{port}",
            )

    def _refresh_exits_locked(self) -> None:
        for run in self._runs.values():
            if (
                run.process is not None
                and run.status.state
                in {
                    ServiceRunState.STARTING,
                    ServiceRunState.READY,
                    ServiceRunState.UNHEALTHY,
                }
                and run.process.poll() is not None
            ):
                run.process = None
                run.identity = None
                run.status = ServiceRunStatus(
                    run.status.service,
                    run.status.run_id,
                    ServiceRunState.FAILED,
                    error="runtime exited unexpectedly",
                )
                self._gateway.set_route(
                    self._gateway_route(run.service), "unavailable", None
                )
                operation_id = self._begin_operation_locked(
                    "service.failure", run.status.service
                )
                self._persist_run_locked(run)
                self._finish_operation_locked(
                    operation_id, "failed", error=run.status.error
                )

    def _terminate_locked(self, run: _Run) -> None:
        process = run.process
        identity = run.identity
        if process is None:
            return
        if identity is None or not self._probe.identity_matches(identity):
            run.process = None
            run.identity = None
            return
        process.terminate()
        try:
            process.wait(self._terminate_timeout)
        except (TimeoutError, OSError):
            if self._probe.identity_matches(identity):
                process.kill()
                try:
                    process.wait(self._kill_timeout)
                except (TimeoutError, OSError):
                    pass
        run.process = None
        run.identity = None

    def _terminate_direct_locked(self, process: ManagedProcess) -> None:
        """Bound cleanup of a handle returned directly by the launcher."""

        process.terminate()
        try:
            process.wait(self._terminate_timeout)
        except (TimeoutError, OSError):
            process.kill()
            try:
                process.wait(self._kill_timeout)
            except (TimeoutError, OSError):
                pass

    def _failed_start_locked(
        self,
        run: _Run,
        operation_id: str,
        error: str,
        supervisor_started: bool,
    ) -> ServiceTransition:
        self._terminate_locked(run)
        run.status = ServiceRunStatus(
            run.status.service,
            run.status.run_id,
            ServiceRunState.FAILED,
            error=error,
        )
        self._gateway.set_route(self._gateway_route(run.service), "unavailable", None)
        self._persist_run_locked(run)
        self._finish_operation_locked(operation_id, "failed", error=error)
        return ServiceTransition(operation_id, run.status, supervisor_started)

    def _set_shedding_locked(self, enabled: bool) -> None:
        if self._shedding_new_work != enabled:
            self._gateway.shed_new_work(enabled)
            self._shedding_new_work = enabled

    @staticmethod
    def _gateway_route(service: InferenceService) -> str:
        return str(service.route)

    def _persist_run_locked(self, run: _Run) -> None:
        snapshot: dict[str, object] = {
            "kind": "service_run",
            "id": f"{run.status.service}/{run.status.run_id}",
            "version": self._clock.time_ns(),
            "service": run.status.service,
            "run_id": run.status.run_id,
            "state": run.status.state.value,
        }
        if run.status.pid is not None:
            snapshot["pid"] = run.status.pid
        if run.status.upstream_port is not None:
            snapshot["upstream_port"] = run.status.upstream_port
        if run.identity is not None:
            snapshot["process_identity"] = run.identity.birth_token
        if run.status.error is not None:
            snapshot["error"] = run.status.error
        self._state_store.put_snapshot(snapshot)

    def _begin_operation_locked(self, kind: str, resource: str) -> str:
        operation_id = self._new_identity_locked("op")
        self._state_store.put_operation(
            {
                "id": operation_id,
                "kind": kind,
                "resource": resource,
                "status": "running",
            }
        )
        self._operation_metadata[operation_id] = (kind, resource)
        self._event_locked(operation_id, "started", resource=resource)
        return operation_id

    def _finish_operation_locked(
        self, operation_id: str, outcome: str, *, error: str | None = None
    ) -> None:
        kind, resource = self._operation_metadata.get(
            operation_id, ("lifecycle", "unknown")
        )
        current: dict[str, object] = {
            "id": operation_id,
            "kind": kind,
            "resource": resource,
            "status": "failed" if outcome == "failed" else "complete",
            "outcome": outcome,
        }
        if error is not None:
            current["error"] = error
        self._state_store.put_operation(current)
        self._event_locked(
            operation_id,
            "finished",
            status=current["status"],
            outcome=outcome,
        )

    def _event_locked(self, operation_id: str, kind: str, **details: object) -> None:
        self._state_store.append_event(
            {"operation_id": operation_id, "kind": kind, **details}
        )

    def _new_identity_locked(self, prefix: str) -> str:
        self._sequence += 1
        return f"{prefix}-{self._clock.time_ns()}-{self._sequence}"
