import unittest

from mlxctl.application.dispatch import (
    ApplicationError,
    OperationDispatcher,
    OperationRequest,
    OperationResult,
)
from mlxctl.application.catalogue import build_operation_catalogue


class _SupervisorActivator:
    def __init__(self) -> None:
        self.calls = 0

    def activate(self) -> None:
        self.calls += 1


class OperationDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.activator = _SupervisorActivator()
        self.dispatcher = OperationDispatcher(
            build_operation_catalogue(), self.activator
        )

    def test_query_never_activates_supervisor(self) -> None:
        self.dispatcher.register(
            "status",
            lambda request: OperationResult(request.name, {"state": "stopped"}),
        )

        result = self.dispatcher.execute(OperationRequest("status"))

        self.assertEqual(result.value["state"], "stopped")
        self.assertEqual(self.activator.calls, 0)

    def test_service_start_visibly_activates_when_handler_requires_it(self) -> None:
        def start(request: OperationRequest) -> OperationResult:
            self.dispatcher.require_supervisor(request)
            return OperationResult(
                request.name, {"service": "coding", "state": "ready"}
            )

        self.dispatcher.register("service.start", start)

        result = self.dispatcher.execute(OperationRequest("service.start"))

        self.assertTrue(result.supervisor_started)
        self.assertEqual(self.activator.calls, 1)

    def test_unknown_and_unimplemented_operations_have_stable_errors(self) -> None:
        with self.assertRaises(ApplicationError) as unknown:
            self.dispatcher.execute(OperationRequest("banana"))
        self.assertEqual(unknown.exception.code, "unknown_operation")

        with self.assertRaises(ApplicationError) as unimplemented:
            self.dispatcher.execute(OperationRequest("runtime.install"))
        self.assertEqual(unimplemented.exception.code, "operation_unavailable")
        self.assertTrue(unimplemented.exception.next_actions)

    def test_result_is_versioned_machine_data_with_progress_events(self) -> None:
        self.dispatcher.register(
            "model.install",
            lambda request: OperationResult(
                request.name,
                {"revision": "a" * 40},
                events=(
                    {"phase": "resolve", "state": "complete"},
                    {"phase": "verify", "state": "complete"},
                ),
            ),
        )

        result = self.dispatcher.execute(OperationRequest("model.install"))

        self.assertEqual(result.schema_version, 1)
        self.assertEqual(result.events[-1]["phase"], "verify")


if __name__ == "__main__":
    unittest.main()
