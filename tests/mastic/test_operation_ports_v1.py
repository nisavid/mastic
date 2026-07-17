import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from mastic.application.application_targets import SamplingProfile
from mastic.application.dispatch import ApplicationError
from mastic.application.config_schema import (
    ApplicationTargetSettings,
    ApplicationTargetSamplingSettings,
)
from mastic.infrastructure.control_client import SupervisorUnavailableError
from mastic.infrastructure.operation_ports import (
    ApplicationTargetOperationPort,
    RemoteOperationPort,
    SupervisorOperationPort,
)
from mastic.infrastructure.application_target_integrations import (
    ApplicationTargetConfiguration,
    ApplicationTargetOwnershipRecoveryRequired,
    ApplicationTargetRemovalResult,
)


class FakeControlClient:
    def __init__(self) -> None:
        self.calls = []
        self.error = None

    def execute(self, operation, parameters=None):
        self.calls.append(("execute", operation, dict(parameters or {})))
        if self.error:
            raise self.error
        return SimpleNamespace(
            result={"state": "ready"},
            operation_id="op-1",
            progress=({"phase": "start"},),
        )

    def cancel(self, operation_id):
        self.calls.append(("cancel", operation_id))
        return SimpleNamespace(
            result={"cancelled": True}, operation_id=operation_id, progress=()
        )


class FakeSupervisor:
    def __init__(self) -> None:
        self.calls = []

    def start(self):
        self.calls.append(("start",))
        return {"state": "running"}

    def stop(self):
        self.calls.append(("stop",))
        return {"state": "stopped"}

    def restart(self):
        self.calls.append(("restart",))
        return {"state": "running"}

    def start_service(self, resource):
        self.calls.append(("start_service", resource))
        return {"service": resource, "state": "ready"}

    def drain_service(self, resource):
        self.calls.append(("drain_service", resource))
        return {"service": resource, "state": "drained"}


class FakeApplicationTargetAdapter:
    def __init__(self) -> None:
        self.calls = []
        self.rollback_error = None

    def preview(self, configuration):
        self.calls.append(("preview", configuration.service_name))
        return ("model",)

    def apply(self, configuration, *, takeover=False):
        self.calls.append(("apply", configuration.service_name, takeover))
        return {"changed": True}

    def remove(self):
        self.calls.append(("remove",))
        return {"changed": True}

    def rollback_point(self):
        self.calls.append(("rollback_point",))

        def rollback():
            self.calls.append(("rollback",))
            if self.rollback_error is not None:
                raise self.rollback_error

        return rollback

    def test(self, configuration, request, *, profile):
        self.calls.append(("test", profile))
        return request(
            configuration.gateway_endpoint,
            configuration.service_name,
            configuration.sampling_profiles[profile].values(),
        )

    def stop_service(self, resource):
        self.calls.append(("stop_service", resource))
        return {"service": resource, "state": "stopped"}

    def restart_service(self, resource):
        self.calls.append(("restart_service", resource))
        return {"service": resource, "state": "ready"}


def _target_settings(service: str) -> ApplicationTargetSettings:
    return ApplicationTargetSettings(
        name="codex",
        kind="codex",
        service=service,
        profile=None,
        context_window=32768,
        provider="mlx-local",
        max_concurrent=None,
        sampling={},
    )


class OperationPortTests(unittest.TestCase):
    def test_remote_port_preserves_progress_and_cancel_identity(self) -> None:
        client = FakeControlClient()
        port = RemoteOperationPort(client)

        result = port.execute("service.start", {"resource": "coding"})
        cancelled = port.execute("operation.cancel", {"resource": "op-7"})

        self.assertEqual(result["operation_id"], "op-1")
        self.assertEqual(result["control_operation_id"], "op-1")
        self.assertEqual(result["progress"], [{"phase": "start"}])
        self.assertEqual(cancelled["operation_id"], "op-7")
        self.assertIn(("cancel", "op-7"), client.calls)

    def test_remote_port_preserves_owner_durable_operation_identity(self) -> None:
        client = FakeControlClient()
        original = client.execute

        def execute(operation, parameters=None):
            response = original(operation, parameters)
            response.result = {"operation_id": "durable-op-9", "state": "ready"}
            return response

        client.execute = execute
        result = RemoteOperationPort(client).execute("service.start", {})

        self.assertEqual(result["operation_id"], "durable-op-9")
        self.assertEqual(result["control_operation_id"], "op-1")

    def test_remote_errors_are_stable_application_errors(self) -> None:
        client = FakeControlClient()
        client.error = SupervisorUnavailableError(
            "supervisor_unavailable", "not running"
        )

        with self.assertRaises(ApplicationError) as raised:
            RemoteOperationPort(client).execute("service.start", {"resource": "coding"})

        self.assertEqual(raised.exception.code, "supervisor_unavailable")

    def test_direct_port_maps_named_lifecycle_without_ambiguity(self) -> None:
        supervisor = FakeSupervisor()
        port = SupervisorOperationPort(supervisor)  # type: ignore[arg-type]

        started = port.execute("service.start", {"resource": "coding"})
        drained = port.execute("service.drain", {"resource": "coding"})
        stopped = port.execute("supervisor.stop", {})

        self.assertEqual(started, {"service": "coding", "state": "ready"})
        self.assertEqual(drained, {"service": "coding", "state": "drained"})
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(
            supervisor.calls,
            [("start_service", "coding"), ("drain_service", "coding"), ("stop",)],
        )

    def test_application_target_port_uses_one_preview_apply_test_remove_contract(
        self,
    ) -> None:
        adapter = FakeApplicationTargetAdapter()
        records = []
        persisted = {}
        coding_profile = SamplingProfile(temperature=0.0)

        def configuration(name, parameters, settings):
            return ApplicationTargetConfiguration(
                "http://127.0.0.1:8766/v1",
                "coding",
                sampling_profiles={"coding": coding_profile},
                service_identity="coding-internal",
            )

        port = ApplicationTargetOperationPort(
            lambda operation, name, parameters, settings: adapter,
            configuration,
            request=lambda target, endpoint, model, sampling: {
                "target": target,
                "model": model,
                **sampling,
            },
            settings=lambda name: persisted.get(name),
            record=lambda name, value: (
                records.append((name, value)),
                persisted.pop(name, None)
                if value is None
                else persisted.__setitem__(name, value),
            ),
        )

        configured = port.execute(
            "application-target.configure",
            {"application_target": "codex", "service": "coding"},
        )
        tested = port.execute(
            "application-target.test", {"application_target": "codex"}
        )
        removed = port.execute(
            "application-target.remove", {"application_target": "codex"}
        )

        self.assertTrue(configured["result"]["changed"])
        self.assertEqual(records[0][1].service, "coding-internal")
        self.assertIs(records[0][1].sampling["coding"], coding_profile)
        self.assertEqual(tested["response"]["target"], "codex")
        self.assertEqual(tested["response"]["model"], "coding")
        self.assertTrue(removed["changed"])
        self.assertEqual(
            [call[0] for call in adapter.calls],
            [
                "preview",
                "rollback_point",
                "apply",
                "test",
                "rollback_point",
                "remove",
            ],
        )
        self.assertEqual(records[-1], ("codex", None))

    def test_hindsight_profile_is_required_then_persisted_for_test_and_remove(
        self,
    ) -> None:
        adapter = FakeApplicationTargetAdapter()
        records = {}
        factory_calls = []

        def adapter_factory(operation, name, parameters, settings):
            factory_calls.append((operation, name, dict(parameters), settings))
            return adapter

        def configuration(name, parameters, settings):
            service = settings.service if settings else str(parameters["service"])
            return ApplicationTargetConfiguration(
                "http://127.0.0.1:8766/v1",
                service,
                context_window=32768,
                sampling_profiles={
                    "verification": SamplingProfile(temperature=0.0),
                    "retain": SamplingProfile(temperature=0.1),
                    "reflect": SamplingProfile(temperature=0.9),
                    "consolidation": SamplingProfile(temperature=0.0),
                },
            )

        port = ApplicationTargetOperationPort(
            adapter_factory,
            configuration,
            request=lambda target, endpoint, model, sampling: {
                "target": target,
                "model": model,
                **sampling,
            },
            settings=lambda name: records.get(name),
            record=lambda name, value: (
                records.pop(name, None)
                if value is None
                else records.__setitem__(name, value)
            ),
        )

        with self.assertRaisesRegex(ApplicationError, "profile"):
            port.execute(
                "application-target.configure",
                {"application_target": "hindsight", "service": "memory"},
            )

        port.execute(
            "application-target.configure",
            {
                "application_target": "hindsight",
                "service": "memory",
                "profile": "agent-memory",
            },
        )
        stored = records["hindsight"]
        self.assertIsInstance(stored, ApplicationTargetSettings)
        self.assertEqual(stored.profile, "agent-memory")
        self.assertEqual(stored.context_window, 32768)
        self.assertEqual(
            stored.sampling["reflect"],
            ApplicationTargetSamplingSettings(temperature=0.9),
        )

        tested = port.execute(
            "application-target.test",
            {"application_target": "hindsight", "profile": "retain"},
        )
        port.execute("application-target.remove", {"application_target": "hindsight"})

        self.assertEqual(tested["response"]["target"], "hindsight")
        self.assertEqual(factory_calls[1][3].profile, "agent-memory")
        self.assertEqual(factory_calls[2][3].profile, "agent-memory")
        self.assertNotIn("hindsight", records)

    def test_configure_translates_unknown_sampling_fields_to_invalid_parameter(
        self,
    ) -> None:
        adapter = FakeApplicationTargetAdapter()

        def invalid_configuration(name, parameters, settings):
            raise ValueError("unknown sampling profile fields: temprature")

        port = ApplicationTargetOperationPort(
            lambda operation, name, parameters, settings: adapter,
            invalid_configuration,
            request=lambda target, endpoint, model, sampling: {},
        )

        with self.assertRaises(ApplicationError) as raised:
            port.execute(
                "application-target.configure",
                {
                    "application_target": "codex",
                    "service": "coding",
                    "sampling_profiles": {"coding": {"temprature": 0.6}},
                },
            )

        self.assertEqual(raised.exception.code, "invalid_parameter")
        self.assertIn("temprature", str(raised.exception))
        self.assertEqual(adapter.calls, [])

    def test_extra_application_target_profiles_fail_before_external_apply(self) -> None:
        adapter = FakeApplicationTargetAdapter()
        port = ApplicationTargetOperationPort(
            lambda operation, name, parameters, settings: adapter,
            lambda name, parameters, settings: ApplicationTargetConfiguration(
                "http://127.0.0.1:8766/v1",
                "coding",
                sampling_profiles={
                    "coding": SamplingProfile(temperature=0.6),
                    "surprise": SamplingProfile(temperature=0.6),
                },
            ),
            request=lambda target, endpoint, model, sampling: {},
        )

        with self.assertRaisesRegex(ApplicationError, "requires sampling profiles"):
            port.execute(
                "application-target.configure",
                {"application_target": "codex", "service": "coding"},
            )

        self.assertEqual(adapter.calls, [])

    def test_hindsight_profile_cannot_change_without_precise_removal(self) -> None:
        stored = ApplicationTargetSettings(
            name="hindsight",
            kind="hindsight",
            service="memory",
            profile="first",
            context_window=None,
            provider="openai",
            max_concurrent=1,
            sampling={},
        )
        port = ApplicationTargetOperationPort(
            lambda operation, name, parameters, settings: (
                FakeApplicationTargetAdapter()
            ),
            lambda name, parameters, settings: ApplicationTargetConfiguration(
                "http://127.0.0.1:8766/v1", "memory"
            ),
            request=lambda target, endpoint, model, sampling: {},
            settings=lambda name: stored,
        )

        with self.assertRaisesRegex(ApplicationError, "[Rr]emove"):
            port.execute(
                "application-target.configure",
                {
                    "application_target": "hindsight",
                    "service": "memory",
                    "profile": "second",
                },
            )

    def test_partial_precise_removal_retains_desired_state_identity(self) -> None:
        stored = ApplicationTargetSettings(
            name="codex",
            kind="codex",
            service="coding",
            profile=None,
            context_window=32768,
            provider="mlx-local",
            max_concurrent=None,
            sampling={},
        )
        adapter = FakeApplicationTargetAdapter()
        adapter.remove = lambda: ApplicationTargetRemovalResult(
            changed=True,
            changes=(),
            skipped_paths=(("model",),),
        )
        recorded = []
        port = ApplicationTargetOperationPort(
            lambda operation, name, parameters, settings: adapter,
            lambda name, parameters, settings: ApplicationTargetConfiguration(
                "http://127.0.0.1:8766/v1", "coding"
            ),
            request=lambda target, endpoint, model, sampling: {},
            settings=lambda name: stored,
            record=lambda name, value: recorded.append((name, value)),
        )

        result = port.execute(
            "application-target.remove", {"application_target": "codex"}
        )

        self.assertTrue(result["desired_state_retained"])
        self.assertEqual(result["skipped_paths"], [["model"]])
        self.assertEqual(recorded, [])

    def test_configure_rolls_back_exactly_when_desired_state_is_unchanged(
        self,
    ) -> None:
        adapter = FakeApplicationTargetAdapter()
        events = adapter.calls

        def settings(_name):
            events.append(("settings", None))
            return None

        def record(_name, _value):
            events.append(("record", "coding"))
            raise RuntimeError("desired state write failed")

        @contextmanager
        def transition(name):
            events.append(("transition_enter", name))
            try:
                yield
            finally:
                events.append(("transition_exit", name))

        port = ApplicationTargetOperationPort(
            lambda operation, name, parameters, settings: adapter,
            lambda name, parameters, settings: ApplicationTargetConfiguration(
                "http://127.0.0.1:8766/v1",
                "coding",
                sampling_profiles={"coding": SamplingProfile(temperature=0.0)},
                service_identity="coding",
            ),
            request=lambda target, endpoint, model, sampling: {},
            settings=settings,
            record=record,
            transition=transition,
        )

        with self.assertRaisesRegex(RuntimeError, "desired state write failed"):
            port.execute(
                "application-target.configure",
                {"application_target": "codex", "service": "coding"},
            )

        self.assertEqual(
            events,
            [
                ("transition_enter", "codex"),
                ("settings", None),
                ("preview", "coding"),
                ("rollback_point",),
                ("apply", "coding", False),
                ("record", "coding"),
                ("settings", None),
                ("rollback",),
                ("transition_exit", "codex"),
            ],
        )

    def test_same_target_transition_serializes_settings_mutation_and_record(
        self,
    ) -> None:
        adapter = FakeApplicationTargetAdapter()
        transition_lock = threading.Lock()
        first_apply_entered = threading.Event()
        release_first_apply = threading.Event()
        second_transition_attempted = threading.Event()
        settings_reads = []
        state = None
        original_apply = adapter.apply

        def apply(configuration, *, takeover=False):
            result = original_apply(configuration, takeover=takeover)
            if configuration.service_name == "first":
                first_apply_entered.set()
                release_first_apply.wait(1)
            return result

        adapter.apply = apply

        @contextmanager
        def transition(_name):
            if first_apply_entered.is_set():
                second_transition_attempted.set()
            with transition_lock:
                yield

        def settings(_name):
            settings_reads.append(state.service if state is not None else None)
            return state

        def record(_name, value):
            nonlocal state
            state = value

        port = ApplicationTargetOperationPort(
            lambda operation, name, parameters, current: adapter,
            lambda name, parameters, current: ApplicationTargetConfiguration(
                "http://127.0.0.1:8766/v1",
                str(parameters["service"]),
                sampling_profiles={"coding": SamplingProfile(temperature=0.0)},
                service_identity=str(parameters["service"]),
            ),
            request=lambda target, endpoint, model, sampling: {},
            settings=settings,
            record=record,
            transition=transition,
        )
        errors = []

        def configure(service):
            try:
                port.execute(
                    "application-target.configure",
                    {"application_target": "codex", "service": service},
                )
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        first = threading.Thread(target=configure, args=("first",))
        second = threading.Thread(target=configure, args=("second",))
        first.start()
        self.assertTrue(first_apply_entered.wait(1))
        second.start()
        self.assertTrue(second_transition_attempted.wait(1))

        self.assertEqual(settings_reads, [None])
        self.assertEqual(
            [call for call in adapter.calls if call[0] == "apply"],
            [("apply", "first", False)],
        )

        release_first_apply.set()
        first.join(1)
        second.join(1)

        self.assertEqual(errors, [])
        self.assertEqual(settings_reads, [None, "first"])
        self.assertEqual(state.service, "second")

    def test_configure_accepts_post_error_state_when_intended_state_committed(
        self,
    ) -> None:
        adapter = FakeApplicationTargetAdapter()
        state = None

        def record(_name, value):
            nonlocal state
            state = value
            raise RuntimeError("post-replace failure")

        port = ApplicationTargetOperationPort(
            lambda operation, name, parameters, settings: adapter,
            lambda name, parameters, settings: ApplicationTargetConfiguration(
                "http://127.0.0.1:8766/v1",
                "coding",
                sampling_profiles={"coding": SamplingProfile(temperature=0.0)},
                service_identity="coding",
            ),
            request=lambda target, endpoint, model, sampling: {},
            settings=lambda name: state,
            record=record,
        )

        result = port.execute(
            "application-target.configure",
            {"application_target": "codex", "service": "coding"},
        )

        self.assertTrue(result["result"]["changed"])
        self.assertEqual(state.service, "coding")
        self.assertNotIn(("rollback",), adapter.calls)

    def test_reconfigure_uses_exact_rollback_when_old_state_is_unchanged(self) -> None:
        adapter = FakeApplicationTargetAdapter()
        stored = _target_settings("old-coding")

        def configuration(name, parameters, settings):
            service = str(parameters.get("service") or settings.service)
            return ApplicationTargetConfiguration(
                "http://127.0.0.1:8766/v1",
                service,
                sampling_profiles={"coding": SamplingProfile(temperature=0.0)},
                service_identity=service,
            )

        def record(_name, _value):
            raise RuntimeError("desired state write failed")

        port = ApplicationTargetOperationPort(
            lambda operation, name, parameters, settings: adapter,
            configuration,
            request=lambda target, endpoint, model, sampling: {},
            settings=lambda name: stored,
            record=record,
        )

        with self.assertRaisesRegex(RuntimeError, "desired state write failed"):
            port.execute(
                "application-target.configure",
                {"application_target": "codex", "service": "new-coding"},
            )

        self.assertEqual(
            adapter.calls,
            [
                ("preview", "new-coding"),
                ("rollback_point",),
                ("apply", "new-coding", False),
                ("rollback",),
            ],
        )

    def test_configure_reports_recovery_without_rollback_for_ambiguous_state(
        self,
    ) -> None:
        adapter = FakeApplicationTargetAdapter()
        stored = _target_settings("old-coding")
        state = stored

        def configuration(name, parameters, settings):
            service = str(parameters.get("service") or settings.service)
            return ApplicationTargetConfiguration(
                "http://127.0.0.1:8766/v1",
                service,
                sampling_profiles={"coding": SamplingProfile(temperature=0.0)},
                service_identity=service,
            )

        def record(_name, _value):
            nonlocal state
            state = _target_settings("other-coding")
            raise RuntimeError("desired state write failed")

        port = ApplicationTargetOperationPort(
            lambda operation, name, parameters, settings: adapter,
            configuration,
            request=lambda target, endpoint, model, sampling: {},
            settings=lambda name: state,
            record=record,
        )

        with self.assertRaises(ApplicationError) as raised:
            port.execute(
                "application-target.configure",
                {"application_target": "codex", "service": "new-coding"},
            )

        self.assertEqual(raised.exception.code, "application_target_recovery_required")
        self.assertNotIn(("rollback",), adapter.calls)

    def test_ownership_recovery_is_typed_and_does_not_expose_evidence(self) -> None:
        secret = "secret-payload-at-/tmp/untrusted-owner.json"

        def blocked_adapter(operation, name, parameters, settings):
            raise ApplicationTargetOwnershipRecoveryRequired(secret)

        port = ApplicationTargetOperationPort(
            blocked_adapter,
            lambda name, parameters, settings: ApplicationTargetConfiguration(
                "http://127.0.0.1:8766/v1", "coding"
            ),
            request=lambda target, endpoint, model, sampling: {},
        )

        with self.assertRaises(ApplicationError) as raised:
            port.execute(
                "application-target.remove", {"application_target": "hindsight"}
            )

        self.assertEqual(raised.exception.code, "application_target_recovery_required")
        self.assertEqual(
            raised.exception.next_actions,
            (
                "move invalid or conflicting ownership manifests out of the mastic application-target ownership directory",
                "mastic application-target inspect hindsight",
            ),
        )
        self.assertNotIn(secret, repr(raised.exception))
        self.assertNotIn("configure", repr(raised.exception.next_actions))

    def test_configure_reports_recovery_without_rollback_when_reload_fails(
        self,
    ) -> None:
        adapter = FakeApplicationTargetAdapter()
        stored = _target_settings("old-coding")
        settings_calls = 0

        def settings(_name):
            nonlocal settings_calls
            settings_calls += 1
            if settings_calls == 1:
                return stored
            raise OSError("desired state is unreadable")

        def configuration(name, parameters, current):
            service = str(parameters.get("service") or current.service)
            return ApplicationTargetConfiguration(
                "http://127.0.0.1:8766/v1",
                service,
                sampling_profiles={"coding": SamplingProfile(temperature=0.0)},
                service_identity=service,
            )

        def record(_name, _value):
            raise RuntimeError("desired state write failed")

        port = ApplicationTargetOperationPort(
            lambda operation, name, parameters, current: adapter,
            configuration,
            request=lambda target, endpoint, model, sampling: {},
            settings=settings,
            record=record,
        )

        with self.assertRaises(ApplicationError) as raised:
            port.execute(
                "application-target.configure",
                {"application_target": "codex", "service": "new-coding"},
            )

        self.assertEqual(raised.exception.code, "application_target_recovery_required")
        self.assertNotIn(("rollback",), adapter.calls)

    def test_remove_rolls_back_exactly_when_desired_state_is_unchanged(
        self,
    ) -> None:
        adapter = FakeApplicationTargetAdapter()
        stored = _target_settings("coding")

        def record(_name, _value):
            raise RuntimeError("desired state delete failed")

        port = ApplicationTargetOperationPort(
            lambda operation, name, parameters, settings: adapter,
            lambda name, parameters, settings: ApplicationTargetConfiguration(
                "http://127.0.0.1:8766/v1",
                settings.service,
                sampling_profiles={"coding": SamplingProfile(temperature=0.0)},
                service_identity=settings.service,
            ),
            request=lambda target, endpoint, model, sampling: {},
            settings=lambda name: stored,
            record=record,
        )

        with self.assertRaisesRegex(RuntimeError, "desired state delete failed"):
            port.execute("application-target.remove", {"application_target": "codex"})

        self.assertEqual(
            [call[0] for call in adapter.calls],
            ["rollback_point", "remove", "rollback"],
        )

    def test_remove_accepts_post_error_state_when_delete_committed(self) -> None:
        adapter = FakeApplicationTargetAdapter()
        state = _target_settings("coding")

        def record(_name, _value):
            nonlocal state
            state = None
            raise RuntimeError("post-replace failure")

        port = ApplicationTargetOperationPort(
            lambda operation, name, parameters, settings: adapter,
            lambda name, parameters, settings: ApplicationTargetConfiguration(
                "http://127.0.0.1:8766/v1", settings.service
            ),
            request=lambda target, endpoint, model, sampling: {},
            settings=lambda name: state,
            record=record,
        )

        result = port.execute(
            "application-target.remove", {"application_target": "codex"}
        )

        self.assertTrue(result["changed"])
        self.assertIsNone(state)
        self.assertNotIn(("rollback",), adapter.calls)

    def test_remove_delegates_manifest_backed_cleanup_without_desired_state(
        self,
    ) -> None:
        adapter = FakeApplicationTargetAdapter()
        recorded = []
        port = ApplicationTargetOperationPort(
            lambda operation, name, parameters, settings: adapter,
            lambda name, parameters, settings: ApplicationTargetConfiguration(
                "http://127.0.0.1:8766/v1", "coding"
            ),
            request=lambda target, endpoint, model, sampling: {},
            settings=lambda name: None,
            record=lambda name, value: recorded.append((name, value)),
        )

        with self.assertRaises(ApplicationError) as raised:
            port.execute("application-target.test", {"application_target": "codex"})
        result = port.execute(
            "application-target.remove", {"application_target": "codex"}
        )

        self.assertEqual(raised.exception.code, "resource_not_found")
        self.assertTrue(result["changed"])
        self.assertEqual(adapter.calls, [("rollback_point",), ("remove",)])
        self.assertEqual(recorded, [])

    def test_configure_reports_recovery_when_exact_rollback_fails(
        self,
    ) -> None:
        adapter = FakeApplicationTargetAdapter()
        adapter.rollback_error = OSError("rollback failed")

        def record(_name, _value):
            raise RuntimeError("desired state write failed")

        port = ApplicationTargetOperationPort(
            lambda operation, name, parameters, settings: adapter,
            lambda name, parameters, settings: ApplicationTargetConfiguration(
                "http://127.0.0.1:8766/v1",
                "coding",
                sampling_profiles={"coding": SamplingProfile(temperature=0.0)},
                service_identity="coding",
            ),
            request=lambda target, endpoint, model, sampling: {},
            record=record,
        )

        with self.assertRaises(ApplicationError) as raised:
            port.execute(
                "application-target.configure",
                {"application_target": "codex", "service": "coding"},
            )

        self.assertEqual(raised.exception.code, "application_target_recovery_required")
        self.assertIn(
            "mastic application-target configure codex", raised.exception.next_actions
        )
        self.assertIn(
            "mastic application-target remove codex", raised.exception.next_actions
        )
        self.assertEqual(
            [call[0] for call in adapter.calls],
            ["preview", "rollback_point", "apply", "rollback"],
        )

    def test_remove_reports_recovery_when_exact_rollback_fails(self) -> None:
        adapter = FakeApplicationTargetAdapter()
        adapter.rollback_error = OSError("rollback failed")
        stored = _target_settings("coding")

        def record(_name, _value):
            raise RuntimeError("desired state delete failed")

        port = ApplicationTargetOperationPort(
            lambda operation, name, parameters, settings: adapter,
            lambda name, parameters, settings: ApplicationTargetConfiguration(
                "http://127.0.0.1:8766/v1",
                settings.service,
                sampling_profiles={"coding": SamplingProfile(temperature=0.0)},
                service_identity=settings.service,
            ),
            request=lambda target, endpoint, model, sampling: {},
            settings=lambda name: stored,
            record=record,
        )

        with self.assertRaises(ApplicationError) as raised:
            port.execute("application-target.remove", {"application_target": "codex"})

        self.assertEqual(raised.exception.code, "application_target_recovery_required")
        self.assertIn(
            "mastic application-target configure codex", raised.exception.next_actions
        )
        self.assertEqual(
            [call[0] for call in adapter.calls],
            ["rollback_point", "remove", "rollback"],
        )


if __name__ == "__main__":
    unittest.main()
