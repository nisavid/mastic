import unittest
from collections.abc import Mapping

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

    def test_application_error_details_are_recursively_immutable(self) -> None:
        error = ApplicationError(
            "setup_interrupted",
            "setup stopped",
            details={
                "completion": "partial",
                "targets": {"codex": "unverified"},
            },
        )

        with self.assertRaises(TypeError):
            error.details["completion"] = "complete"  # type: ignore[index]
        targets = error.details["targets"]
        self.assertIsInstance(targets, Mapping)
        assert isinstance(targets, Mapping)
        with self.assertRaises(TypeError):
            targets["codex"] = "ready"  # type: ignore[index]

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

    def test_no_validated_fit_preview_is_returned_as_terminal_observation(self) -> None:
        self.backend.prepare = lambda _request: PreparedOperation(
            False,
            lambda: self.fail("No Validated Fit must not execute"),
            (
                {
                    "phase": "preview",
                    "state": "no_validated_fit",
                    "completion": "complete",
                    "readiness": "unverified",
                    "mutation_count": 0,
                },
            ),
        )

        result = self.dispatcher.preview(OperationRequest("setup"))

        self.assertEqual(result.value["state"], "no_validated_fit")
        self.assertEqual(result.value["completion"], "complete")
        self.assertNotIn("preview", result.value)

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

    def test_prepare_and_preview_failures_are_stable_application_errors(self) -> None:
        def fail(_request):
            raise OSError("backend unavailable")

        self.backend.prepare = fail

        with self.assertRaises(ApplicationError) as execution:
            self.dispatcher.execute(OperationRequest("status"))
        with self.assertRaises(ApplicationError) as preview:
            self.dispatcher.preview(OperationRequest("status"))

        self.assertEqual(execution.exception.code, "operation_failed")
        self.assertEqual(preview.exception.code, "operation_failed")
        self.assertIn("prepare", execution.exception.message)
        self.assertIn("preview", preview.exception.message)

    def test_activation_failure_is_a_stable_application_error(self) -> None:
        self.backend.require.add("service.start")

        def fail():
            raise OSError("control socket unavailable")

        self.activator.activate = fail

        with self.assertRaises(ApplicationError) as raised:
            self.dispatcher.execute(
                OperationRequest("service.start", {"resource": "coding"})
            )

        self.assertEqual(raised.exception.code, "operation_failed")
        self.assertIn("activate", raised.exception.message)

    def test_backend_result_is_versioned_with_progress_events(self) -> None:
        result = self.dispatcher.execute(OperationRequest("runtime.available"))

        self.assertEqual(result.schema_version, 1)
        self.assertEqual(result.value["operation"], "runtime.available")
        self.assertEqual(result.events[0]["phase"], "preview")


if __name__ == "__main__":
    unittest.main()
