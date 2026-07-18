import unittest

from mastic.application.catalogue import build_operation_catalogue
from mastic.application.dispatch import (
    ApplicationError,
    OperationDispatcher,
    OperationRequest,
    PreparedOperation,
)


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
            events=({"phase": "preview", "state": "complete"},),
        )


class OperationDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalogue = build_operation_catalogue()
        self.activator = _Activator()
        self.backend = _Backend()
        self.dispatcher = OperationDispatcher(
            self.catalogue, self.activator, self.backend
        )

    def test_dispatches_every_cli_and_tui_operation(self) -> None:
        for name, operation in self.catalogue.items():
            with self.subTest(operation=name):
                parameters = {"confirmed": True} if operation.confirmation else {}
                result = self.dispatcher.execute(OperationRequest(name, parameters))
                self.assertEqual(result.operation, name)

    def test_confirmation_is_enforced_below_both_interfaces(self) -> None:
        with self.assertRaises(ApplicationError) as raised:
            self.dispatcher.execute(
                OperationRequest("model.cache.evict", {"resource": "cached"})
            )

        self.assertEqual(raised.exception.code, "confirmation_required")

    def test_preview_resolves_backend_without_execution_or_activation(self) -> None:
        self.backend.require.add("model.cache.evict")

        result = self.dispatcher.preview(
            OperationRequest("model.cache.evict", {"resource": "cached"})
        )

        self.assertEqual(result.value["state"], "review_required")
        self.assertTrue(result.value["confirmation_required"])
        self.assertTrue(result.value["requires_supervisor"])
        self.assertEqual(result.value["preview"][0]["phase"], "preview")
        self.assertEqual(self.activator.calls, 0)

    def test_preview_promotes_exact_identity_for_interface_confirmation(self) -> None:
        original = self.backend.prepare

        def prepare(request):
            prepared = original(request)
            return PreparedOperation(
                prepared.requires_supervisor,
                prepared.execute,
                ({"phase": "preview", "preview_fingerprint": "sha256:exact"},),
            )

        self.backend.prepare = prepare

        result = self.dispatcher.preview(OperationRequest("setup"))

        self.assertEqual(result.value["preview_fingerprint"], "sha256:exact")

    def test_service_start_visibly_activates_supervisor(self) -> None:
        self.backend.require.add("service.start")

        result = self.dispatcher.execute(
            OperationRequest("service.start", {"resource": "coding"})
        )

        self.assertTrue(result.supervisor_started)
        self.assertEqual(self.activator.calls, 1)

    def test_local_config_mutation_does_not_start_supervisor(self) -> None:
        result = self.dispatcher.execute(
            OperationRequest("config.restore", {"confirmed": True})
        )

        self.assertFalse(result.supervisor_started)
        self.assertEqual(self.activator.calls, 0)

    def test_supervisor_stop_uses_running_supervisor_without_starting_one(self) -> None:
        self.backend.require.add("supervisor.stop")

        result = self.dispatcher.execute(
            OperationRequest("supervisor.stop", {"confirmed": True})
        )

        self.assertFalse(result.supervisor_started)
        self.assertEqual(self.activator.calls, 0)
        self.assertEqual(result.value["operation"], "supervisor.stop")

    def test_backend_cannot_activate_read_only_operation(self) -> None:
        self.backend.require.add("status")

        with self.assertRaises(ApplicationError) as raised:
            self.dispatcher.execute(OperationRequest("status"))

        self.assertEqual(raised.exception.code, "activation_forbidden")
        self.assertEqual(self.activator.calls, 0)

    def test_unknown_operation_has_stable_error(self) -> None:
        with self.assertRaises(ApplicationError) as unknown:
            self.dispatcher.execute(OperationRequest("banana"))
        self.assertEqual(unknown.exception.code, "unknown_operation")

    def test_backend_result_is_versioned_with_progress_events(self) -> None:
        result = self.dispatcher.execute(OperationRequest("runtime.available"))

        self.assertEqual(result.schema_version, 1)
        self.assertEqual(result.value["operation"], "runtime.available")
        self.assertEqual(result.events[0]["phase"], "preview")


if __name__ == "__main__":
    unittest.main()
