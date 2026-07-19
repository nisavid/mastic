import json
import unittest
from dataclasses import replace

from click.utils import strip_ansi
from typer.testing import CliRunner

from mastic.application.catalogue import build_operation_catalogue
from mastic.application.dispatch import ApplicationError, OperationResult
from mastic.interfaces.cli import build_cli


class _Dispatcher:
    def __init__(self) -> None:
        self.requests = []
        self.previews = []
        self.results = {}

    def preview(self, request):
        self.previews.append(request)
        value = {
            "state": "review_required",
            "operation": request.name,
            "parameters": dict(request.parameters),
        }
        if request.name == "setup":
            value["preview_fingerprint"] = "sha256:exact"
        return OperationResult(request.name, value)

    def execute(self, request):
        self.requests.append(request)
        if request.name == "doctor":
            raise ApplicationError(
                "repair_required",
                "OptiQ capability conflict",
                next_actions=("mastic runtime update optiq",),
                details={"completion": "partial", "readiness": "pending"},
            )
        return OperationResult(
            request.name,
            self.results.get(
                request.name,
                {
                    "operation": request.name,
                    "parameters": dict(request.parameters),
                },
            ),
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
        self.assertIn("remove", result.output)
        self.assertIn("supervisor", result.output)
        self.assertIn("runtime", result.output)
        self.assertIn("model", result.output)
        self.assertIn("service", result.output)
        self.assertIn("application-target", result.output)
        self.assertNotIn("client", result.output)

    def test_required_integer_parameters_are_enforced_before_dispatch(self) -> None:
        catalogue = dict(build_operation_catalogue())
        search = catalogue["model.search"]
        limit = next(
            parameter for parameter in search.parameters if parameter.name == "limit"
        )
        catalogue["model.search"] = replace(
            search,
            parameters=(search.parameters[0], replace(limit, required=True)),
        )
        app = build_cli(self.dispatcher, catalogue, tui_launcher=lambda: 0)

        missing = self.runner.invoke(app, ["model", "search"])
        present = self.runner.invoke(app, ["model", "search", "--limit", "8"])

        self.assertEqual(missing.exit_code, 2)
        self.assertIn("--limit", missing.output)
        self.assertEqual(present.exit_code, 0, present.output)
        self.assertEqual(self.dispatcher.requests[-1].parameters["limit"], 8)

    def test_catalogue_operations_require_a_registered_cli_group(self) -> None:
        catalogue = dict(build_operation_catalogue())
        catalogue["missing.inspect"] = replace(
            catalogue["status"], name="missing.inspect"
        )

        with self.assertRaisesRegex(
            RuntimeError, "missing.inspect.*registered CLI group.*missing"
        ):
            build_cli(self.dispatcher, catalogue, tui_launcher=lambda: 0)

    def test_application_target_is_the_only_configuration_target_command(self) -> None:
        canonical = self.runner.invoke(
            self.app, ["application-target", "list", "--json"]
        )
        prohibited = self.runner.invoke(self.app, ["client", "list", "--json"])

        self.assertEqual(canonical.exit_code, 0, canonical.output)
        self.assertEqual(
            self.dispatcher.requests[-1].name,
            "application-target.list",
        )
        self.assertNotEqual(prohibited.exit_code, 0)

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
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["result"]["parameters"]["resource"], "coding")

    def test_service_edit_can_explicitly_clear_a_boolean(self) -> None:
        result = self.runner.invoke(
            self.app,
            ["service", "edit", "coding", "--no-pinned", "--yes", "--json"],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIs(self.dispatcher.requests[-1].parameters["pinned"], False)

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

    def test_machine_mutation_without_yes_emits_preview_without_execution(self) -> None:
        preview = self.runner.invoke(
            self.app,
            ["model", "cache", "evict", "qwen-exact", "--json"],
        )

        self.assertEqual(preview.exit_code, 0, preview.output)
        self.assertEqual(
            json.loads(preview.output)["result"]["state"], "review_required"
        )
        self.assertEqual(self.dispatcher.previews[-1].name, "model.cache.evict")
        self.assertFalse(self.dispatcher.requests)

    def test_interactive_mutation_renders_backend_preview_before_confirmation(
        self,
    ) -> None:
        result = self.runner.invoke(
            self.app,
            ["service", "remove", "coding"],
            input="n\n",
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Resolved mutation preview", result.output)
        self.assertEqual(self.dispatcher.previews[-1].name, "service.remove")
        self.assertFalse(self.dispatcher.requests)

    def test_setup_confirmation_carries_the_reviewed_preview_fingerprint(self) -> None:
        result = self.runner.invoke(self.app, ["setup"], input="y\n")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            self.dispatcher.requests[-1].parameters["preview_fingerprint"],
            "sha256:exact",
        )

    def test_noninteractive_setup_previews_and_parses_structured_inputs(self) -> None:
        result = self.runner.invoke(
            self.app,
            [
                "setup",
                "--intent",
                "deep",
                "--service-options",
                '{"kv_config":"kv_config.json","mtp":true}',
                "--application-targets",
                '["codex","hindsight"]',
                "--skip-canaries",
                '["hindsight"]',
                "--yes",
                "--json",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.dispatcher.previews[-1].name, "setup")
        parameters = self.dispatcher.requests[-1].parameters
        self.assertEqual(parameters["intent"], "deep")
        self.assertEqual(parameters["service_options"]["kv_config"], "kv_config.json")
        self.assertTrue(parameters["service_options"]["mtp"])
        self.assertEqual(parameters["application_targets"], ["codex", "hindsight"])
        self.assertEqual(parameters["skip_canaries"], ["hindsight"])
        self.assertEqual(parameters["preview_fingerprint"], "sha256:exact")

    def test_setup_help_leads_with_intent_and_keeps_advanced_capacity(self) -> None:
        result = self.runner.invoke(self.app, ["setup", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        output = strip_ansi(result.output)
        self.assertIn("--intent", output)
        self.assertIn("responsive", output)
        self.assertIn("deep", output)
        self.assertIn("--capacity", output)
        self.assertIn("balanced", output)
        self.assertIn("long-context", output)
        self.assertIn("native-context", output)
        normalized = " ".join(output.replace("│", " ").split())
        self.assertIn("advanced", normalized.casefold())
        self.assertIn("simultaneous inference requests", normalized)
        self.assertIn("prefill at 4-7 requests", normalized)
        self.assertIn("8 permits", normalized)

    def test_setup_help_explains_offline_completion_evidence_boundary(self) -> None:
        result = self.runner.invoke(self.app, ["setup", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        output = " ".join(strip_ansi(result.output).replace("│", " ").split())
        self.assertIn("Prevent network access", output)
        self.assertIn("matching durable setup completion evidence", output)
        self.assertIn("not inferred as ready", output)

    def test_json_setup_preserves_independent_degraded_readiness(self) -> None:
        self.dispatcher.results["setup"] = {
            "state": "complete",
            "complete": True,
            "completion": "complete",
            "readiness": "degraded",
            "application_target_readiness": {
                "codex": "degraded",
                "hindsight": "ready",
            },
            "performance_profile": {
                "id": "phase1-qwen36-optiq-apple-silicon",
                "version": 1,
                "status": "provisional",
            },
        }

        result = self.runner.invoke(self.app, ["setup", "--yes", "--json"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            json.loads(result.output),
            {
                "operation": "setup",
                "result": {
                    "application_target_readiness": {
                        "codex": "degraded",
                        "hindsight": "ready",
                    },
                    "complete": True,
                    "completion": "complete",
                    "performance_profile": {
                        "id": "phase1-qwen36-optiq-apple-silicon",
                        "status": "provisional",
                        "version": 1,
                    },
                    "readiness": "degraded",
                    "state": "complete",
                },
                "schema_version": 1,
            },
        )

    def test_machine_output_modes_are_mutually_exclusive(self) -> None:
        result = self.runner.invoke(self.app, ["status", "--json", "--json-lines"])

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("choose exactly one output mode", result.output)
        self.assertFalse(self.dispatcher.requests)

    def test_status_and_check_preserve_setup_outcome_in_human_and_json(self) -> None:
        outcome = {
            "state": "ok",
            "completion": "complete",
            "readiness": "degraded",
            "application_target_readiness": {
                "codex": "ready",
                "hindsight": "degraded",
            },
        }
        self.dispatcher.results.update({"status": outcome, "check": outcome})

        for command in ("status", "check"):
            with self.subTest(command=command):
                machine = self.runner.invoke(self.app, [command, "--json"])
                human = self.runner.invoke(self.app, [command])

                self.assertEqual(machine.exit_code, 0, machine.output)
                self.assertEqual(json.loads(machine.output)["result"], outcome)
                self.assertEqual(human.exit_code, 0, human.output)
                self.assertIn("completion", human.output)
                self.assertIn("complete", human.output)
                self.assertIn("readiness", human.output)
                self.assertIn("degraded", human.output)
                self.assertIn("codex", human.output)
                self.assertIn("ready", human.output)

    def test_machine_errors_are_stable_and_human_errors_offer_next_action(self) -> None:
        machine = self.runner.invoke(self.app, ["doctor", "--json"])
        self.assertEqual(machine.exit_code, 1)
        machine_error = json.loads(machine.output)["error"]
        self.assertEqual(machine_error["code"], "repair_required")
        self.assertEqual(
            machine_error["details"],
            {"completion": "partial", "readiness": "pending"},
        )

        human = self.runner.invoke(self.app, ["doctor"])
        self.assertEqual(human.exit_code, 1)
        self.assertIn("OptiQ capability conflict", human.output)
        self.assertIn("completion: partial", human.output)
        self.assertIn("readiness: pending", human.output)
        self.assertIn("mastic runtime update optiq", human.output)

    def test_check_returns_nonzero_when_the_reported_state_is_unhealthy(self) -> None:
        original = self.dispatcher.execute

        def unhealthy(request):
            if request.name == "check":
                return OperationResult("check", {"state": "stopped", "checks": []})
            return original(request)

        self.dispatcher.execute = unhealthy
        result = self.runner.invoke(self.app, ["check", "--json"])

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(json.loads(result.output)["result"]["state"], "stopped")

    def test_check_returns_nonzero_for_application_target_drift(self) -> None:
        issue = {
            "code": "application_target_drifted",
            "application_target": "hindsight",
            "state": "drifted",
            "message": "Hindsight profile differs from mastic ownership.",
            "next_actions": ["mastic application-target configure hindsight --help"],
        }
        self.dispatcher.results["check"] = {
            "state": "failed",
            "completion": "complete",
            "readiness": "unverified",
            "application_target_readiness": {"hindsight": "unverified"},
            "issues": [issue],
            "checks": [{"name": "application-target:hindsight", "state": "unverified"}],
            "next_actions": issue["next_actions"],
        }

        result = self.runner.invoke(self.app, ["check", "--json"])

        self.assertEqual(result.exit_code, 1, result.output)
        report = json.loads(result.output)["result"]
        self.assertEqual(report["state"], "failed")
        self.assertIn(issue, report["issues"])

    def test_explicit_tui_command_uses_injected_launcher(self) -> None:
        result = self.runner.invoke(self.app, ["tui"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.tui_calls, 1)
        self.assertFalse(self.dispatcher.requests)


if __name__ == "__main__":
    unittest.main()
