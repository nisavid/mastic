import json
import unittest

from typer.testing import CliRunner

from mlxctl.application.catalogue import build_operation_catalogue
from mlxctl.application.dispatch import ApplicationError, OperationResult
from mlxctl.interfaces.cli import build_cli


class _Dispatcher:
    def __init__(self) -> None:
        self.requests = []
        self.previews = []

    def preview(self, request):
        self.previews.append(request)
        return OperationResult(
            request.name,
            {
                "state": "planned",
                "operation": request.name,
                "parameters": dict(request.parameters),
            },
        )

    def execute(self, request):
        self.requests.append(request)
        if request.name == "doctor":
            raise ApplicationError(
                "repair_required",
                "OptiQ capability conflict",
                next_actions=("mlxctl runtime update optiq",),
            )
        return OperationResult(
            request.name,
            {
                "operation": request.name,
                "parameters": dict(request.parameters),
            },
        )


class CliV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.dispatcher = _Dispatcher()
        self.tui_calls = 0

        def launch_tui() -> int:
            self.tui_calls += 1
            return 0

        self.app = build_cli(
            self.dispatcher,
            build_operation_catalogue(),
            tui_launcher=launch_tui,
        )
        self.runner = CliRunner()

    def test_root_help_exposes_resource_groups_and_guided_setup(self) -> None:
        result = self.runner.invoke(self.app, ["--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("setup", result.output)
        self.assertIn("supervisor", result.output)
        self.assertIn("runtime", result.output)
        self.assertIn("model", result.output)
        self.assertIn("service", result.output)

    def test_every_catalogue_operation_has_a_cli_help_surface(self) -> None:
        for name in build_operation_catalogue():
            with self.subTest(operation=name):
                result = self.runner.invoke(self.app, [*name.split("."), "--help"])
                self.assertEqual(result.exit_code, 0, result.output)

    def test_status_help_is_machine_overview_not_ambiguous_server_argument(
        self,
    ) -> None:
        result = self.runner.invoke(self.app, ["status", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("SERVER", result.output)
        self.assertIn("Supervisor", result.output)
        self.assertIn("Gateway", result.output)

    def test_nested_resource_command_dispatches_named_resource(self) -> None:
        result = self.runner.invoke(self.app, ["service", "stop", "coding", "--json"])

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["operation"], "service.stop")
        self.assertEqual(payload["parameters"]["resource"], "coding")

    def test_help_and_dispatch_expose_operation_specific_values(self) -> None:
        help_result = self.runner.invoke(self.app, ["runtime", "install", "--help"])
        self.assertEqual(help_result.exit_code, 0, help_result.output)
        self.assertIn("RUNTIME", help_result.output)
        self.assertIn("mlx_lm", help_result.output)
        self.assertIn("mlx_vlm", help_result.output)
        self.assertIn("optiq", help_result.output)

        result = self.runner.invoke(
            self.app,
            [
                "model",
                "search",
                "Qwen",
                "--source",
                "curated",
                "--limit",
                "8",
                "--json",
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            dict(self.dispatcher.requests[-1].parameters),
            {"query": "Qwen", "source": "curated", "limit": 8},
        )

    def test_model_cache_is_a_real_nested_command_group(self) -> None:
        result = self.runner.invoke(
            self.app,
            ["model", "cache", "evict", "qwen-exact", "--yes", "--json"],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.dispatcher.requests[-1].name, "model.cache.evict")
        self.assertTrue(self.dispatcher.requests[-1].parameters["confirmed"])

    def test_destructive_command_requires_prompt_or_explicit_yes(self) -> None:
        denied = self.runner.invoke(
            self.app,
            ["model", "cache", "evict", "qwen-exact", "--json"],
        )

        self.assertNotEqual(denied.exit_code, 0)
        self.assertFalse(self.dispatcher.requests)

    def test_interactive_mutation_renders_backend_plan_before_confirmation(
        self,
    ) -> None:
        result = self.runner.invoke(
            self.app,
            ["service", "remove", "coding"],
            input="n\n",
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Resolved mutation plan", result.output)
        self.assertEqual(self.dispatcher.previews[-1].name, "service.remove")
        self.assertFalse(self.dispatcher.requests)

    def test_machine_errors_are_stable_and_human_errors_offer_next_action(self) -> None:
        machine = self.runner.invoke(self.app, ["doctor", "--json"])
        self.assertEqual(machine.exit_code, 1)
        self.assertEqual(json.loads(machine.output)["error"]["code"], "repair_required")

        human = self.runner.invoke(self.app, ["doctor"])
        self.assertEqual(human.exit_code, 1)
        self.assertIn("OptiQ capability conflict", human.output)
        self.assertIn("mlxctl runtime update optiq", human.output)

    def test_explicit_tui_command_uses_injected_launcher(self) -> None:
        result = self.runner.invoke(self.app, ["tui"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.tui_calls, 1)
        self.assertFalse(self.dispatcher.requests)


if __name__ == "__main__":
    unittest.main()
