import unittest
from types import SimpleNamespace

from mastic.application.dispatch import ApplicationError
from mastic.application.config_schema import ClientSettings, ClientSamplingSettings
from mastic.infrastructure.control_client import SupervisorUnavailableError
from mastic.infrastructure.operation_ports import (
    ClientOperationPort,
    RemoteOperationPort,
    SupervisorOperationPort,
)
from mastic.infrastructure.client_integrations import (
    ClientConfiguration,
    ClientRemovalResult,
    SamplingProfile,
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


class FakeClientAdapter:
    def __init__(self) -> None:
        self.calls = []

    def preview(self, configuration):
        self.calls.append(("preview", configuration.service_name))
        return ("model",)

    def apply(self, configuration, *, takeover=False):
        self.calls.append(("apply", configuration.service_name, takeover))
        return {"changed": True}

    def remove(self):
        self.calls.append(("remove",))
        return {"changed": True}

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

    def test_client_port_uses_one_preview_apply_test_remove_contract(self) -> None:
        adapter = FakeClientAdapter()
        records = []
        persisted = {}

        def configuration(name, parameters, settings):
            return ClientConfiguration(
                "http://127.0.0.1:8766/v1",
                "coding",
                sampling_profiles={"coding": SamplingProfile(temperature=0.0)},
                service_identity="coding-internal",
            )

        port = ClientOperationPort(
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
            "client.configure", {"client": "codex", "service": "coding"}
        )
        tested = port.execute("client.test", {"resource": "codex"})
        removed = port.execute("client.remove", {"resource": "codex"})

        self.assertTrue(configured["result"]["changed"])
        self.assertEqual(records[0][1].service, "coding-internal")
        self.assertEqual(tested["response"]["target"], "codex")
        self.assertEqual(tested["response"]["model"], "coding")
        self.assertTrue(removed["changed"])
        self.assertEqual(
            [call[0] for call in adapter.calls], ["preview", "apply", "test", "remove"]
        )
        self.assertEqual(records[-1], ("codex", None))

    def test_hindsight_profile_is_required_then_persisted_for_test_and_remove(
        self,
    ) -> None:
        adapter = FakeClientAdapter()
        records = {}
        factory_calls = []

        def adapter_factory(operation, name, parameters, settings):
            factory_calls.append((operation, name, dict(parameters), settings))
            return adapter

        def configuration(name, parameters, settings):
            service = settings.service if settings else str(parameters["service"])
            return ClientConfiguration(
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

        port = ClientOperationPort(
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
                "client.configure", {"client": "hindsight", "service": "memory"}
            )

        port.execute(
            "client.configure",
            {
                "client": "hindsight",
                "service": "memory",
                "profile": "agent-memory",
            },
        )
        stored = records["hindsight"]
        self.assertIsInstance(stored, ClientSettings)
        self.assertEqual(stored.profile, "agent-memory")
        self.assertEqual(stored.context_window, 32768)
        self.assertEqual(
            stored.sampling["reflect"], ClientSamplingSettings(temperature=0.9)
        )

        tested = port.execute(
            "client.test", {"resource": "hindsight", "profile": "retain"}
        )
        port.execute("client.remove", {"resource": "hindsight"})

        self.assertEqual(tested["response"]["target"], "hindsight")
        self.assertEqual(factory_calls[1][3].profile, "agent-memory")
        self.assertEqual(factory_calls[2][3].profile, "agent-memory")
        self.assertNotIn("hindsight", records)

    def test_extra_client_profiles_fail_before_external_apply(self) -> None:
        adapter = FakeClientAdapter()
        port = ClientOperationPort(
            lambda operation, name, parameters, settings: adapter,
            lambda name, parameters, settings: ClientConfiguration(
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
            port.execute("client.configure", {"client": "codex", "service": "coding"})

        self.assertEqual(adapter.calls, [])

    def test_hindsight_profile_cannot_change_without_precise_removal(self) -> None:
        stored = ClientSettings(
            name="hindsight",
            kind="hindsight",
            service="memory",
            profile="first",
            context_window=None,
            provider="openai",
            max_concurrent=1,
            sampling={},
        )
        port = ClientOperationPort(
            lambda operation, name, parameters, settings: FakeClientAdapter(),
            lambda name, parameters, settings: ClientConfiguration(
                "http://127.0.0.1:8766/v1", "memory"
            ),
            request=lambda target, endpoint, model, sampling: {},
            settings=lambda name: stored,
        )

        with self.assertRaisesRegex(ApplicationError, "[Rr]emove"):
            port.execute(
                "client.configure",
                {
                    "client": "hindsight",
                    "service": "memory",
                    "profile": "second",
                },
            )

    def test_partial_precise_removal_retains_desired_state_identity(self) -> None:
        stored = ClientSettings(
            name="codex",
            kind="codex",
            service="coding",
            profile=None,
            context_window=32768,
            provider="mlx-local",
            max_concurrent=None,
            sampling={},
        )
        adapter = FakeClientAdapter()
        adapter.remove = lambda: ClientRemovalResult(
            changed=True,
            changes=(),
            skipped_paths=(("model",),),
        )
        recorded = []
        port = ClientOperationPort(
            lambda operation, name, parameters, settings: adapter,
            lambda name, parameters, settings: ClientConfiguration(
                "http://127.0.0.1:8766/v1", "coding"
            ),
            request=lambda target, endpoint, model, sampling: {},
            settings=lambda name: stored,
            record=lambda name, value: recorded.append((name, value)),
        )

        result = port.execute("client.remove", {"resource": "codex"})

        self.assertTrue(result["desired_state_retained"])
        self.assertEqual(result["skipped_paths"], [["model"]])
        self.assertEqual(recorded, [])


if __name__ == "__main__":
    unittest.main()
