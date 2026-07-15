import unittest

from mlxctl.application.catalogue import build_operation_catalogue
from mlxctl.application.dispatch import (
    ApplicationError,
    OperationDispatcher,
    OperationRequest,
)
from mlxctl.application.manager import ApplicationManager, PreparedOperation


class _Activator:
    def __init__(self) -> None:
        self.calls = 0

    def activate(self) -> None:
        self.calls += 1


class _Backend:
    def __init__(self) -> None:
        self.prepared = []
        self.require = set()

    def prepare(self, request: OperationRequest) -> PreparedOperation:
        self.prepared.append(request)
        return PreparedOperation(
            requires_supervisor=request.name in self.require,
            execute=lambda: {
                "operation": request.name,
                "parameters": dict(request.parameters),
            },
            events=({"phase": "plan", "state": "complete"},),
        )


class ApplicationManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalogue = build_operation_catalogue()
        self.activator = _Activator()
        self.dispatcher = OperationDispatcher(self.catalogue, self.activator)
        self.backend = _Backend()
        ApplicationManager(self.catalogue, self.backend).register(self.dispatcher)

    def test_registers_every_cli_and_tui_operation(self) -> None:
        for name in self.catalogue:
            with self.subTest(operation=name):
                result = self.dispatcher.execute(OperationRequest(name))
                self.assertEqual(result.operation, name)

    def test_service_start_can_visibly_activate_supervisor(self) -> None:
        self.backend.require.add("service.start")

        result = self.dispatcher.execute(
            OperationRequest("service.start", {"resource": "coding"})
        )

        self.assertTrue(result.supervisor_started)
        self.assertEqual(self.activator.calls, 1)

    def test_local_config_mutation_does_not_start_supervisor(self) -> None:
        result = self.dispatcher.execute(OperationRequest("config.restore"))

        self.assertFalse(result.supervisor_started)
        self.assertEqual(self.activator.calls, 0)

    def test_backend_cannot_activate_a_read_only_operation(self) -> None:
        self.backend.require.add("status")

        with self.assertRaises(ApplicationError) as raised:
            self.dispatcher.execute(OperationRequest("status"))

        self.assertEqual(raised.exception.code, "activation_forbidden")
        self.assertEqual(self.activator.calls, 0)

    def test_backend_result_and_progress_are_normalized(self) -> None:
        result = self.dispatcher.execute(OperationRequest("runtime.available"))

        self.assertEqual(result.value["operation"], "runtime.available")
        self.assertEqual(result.events[0]["phase"], "plan")


if __name__ == "__main__":
    unittest.main()
