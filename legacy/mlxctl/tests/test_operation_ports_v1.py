import unittest
from types import SimpleNamespace

from mlxctl.application.dispatch import ApplicationError
from mlxctl.infrastructure.control_client import SupervisorUnavailableError
from mlxctl.infrastructure.operation_ports import (
    RemoteOperationPort,
    SupervisorOperationPort,
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
        self.assertEqual(result["progress"], [{"phase": "start"}])
        self.assertEqual(cancelled["operation_id"], "op-7")
        self.assertIn(("cancel", "op-7"), client.calls)

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
        stopped = port.execute("supervisor.stop", {})

        self.assertEqual(started, {"service": "coding", "state": "ready"})
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(supervisor.calls, [("start_service", "coding"), ("stop",)])


if __name__ == "__main__":
    unittest.main()
