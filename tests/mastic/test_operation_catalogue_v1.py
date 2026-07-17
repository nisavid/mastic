import unittest

from mastic.application.catalogue import (
    OperationKind,
    ParameterKind,
    SupervisorRequirement,
    build_operation_catalogue,
)


class OperationCatalogueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalogue = build_operation_catalogue()

    def test_contains_the_complete_approved_command_tree(self) -> None:
        required = {
            "setup",
            "remove",
            "status",
            "check",
            "doctor",
            "supervisor.start",
            "supervisor.stop",
            "gateway.routes",
            "runtime.available",
            "runtime.install",
            "model.search",
            "model.install",
            "model.adopt",
            "model.cache.evict",
            "service.create",
            "service.start",
            "service.stop",
            "operation.inspect",
            "client.configure",
            "config.restore",
            "logs",
            "metrics",
            "tui",
        }
        self.assertTrue(required.issubset(self.catalogue))

    def test_reads_never_activate_the_supervisor(self) -> None:
        for operation in self.catalogue.values():
            if operation.kind is OperationKind.QUERY:
                with self.subTest(operation=operation.name):
                    self.assertIs(
                        operation.supervisor,
                        SupervisorRequirement.NEVER_START,
                    )

    def test_local_desired_state_mutations_do_not_require_supervisor(self) -> None:
        for name in (
            "remove",
            "gateway.configure",
            "model.uninstall",
            "model.trust",
            "service.create",
            "service.edit",
            "client.configure",
            "client.remove",
            "config.import",
            "config.restore",
        ):
            with self.subTest(operation=name):
                self.assertIs(
                    self.catalogue[name].supervisor,
                    SupervisorRequirement.NEVER_START,
                )

    def test_supervisor_stop_never_starts_the_supervisor_it_is_stopping(self) -> None:
        self.assertIs(
            self.catalogue["supervisor.stop"].supervisor,
            SupervisorRequirement.NEVER_START,
        )

    def test_mutations_declare_confirmation_and_machine_help(self) -> None:
        install = self.catalogue["model.install"]
        self.assertTrue(install.confirmation)
        self.assertIn("exact revision", install.summary.lower())
        self.assertTrue(install.examples)
        self.assertIn("json", install.output_modes)

    def test_summaries_explain_user_visible_effects(self) -> None:
        self.assertEqual(
            self.catalogue["model.search"].summary,
            "Search curated, Hugging Face, or local cached models.",
        )
        self.assertIn(
            "drain and stop one service",
            self.catalogue["service.stop"].summary.casefold(),
        )
        self.assertIn(
            "desired and live state",
            self.catalogue["service.list"].summary.casefold(),
        )

    def test_parameters_explain_accepted_values_and_discovery(self) -> None:
        install = self.catalogue["runtime.install"]
        self.assertEqual(install.parameters[0].kind, ParameterKind.ARGUMENT)
        self.assertEqual(
            install.parameters[0].accepted,
            ("mlx_lm", "mlx_vlm", "optiq"),
        )
        search = self.catalogue["model.search"]
        self.assertEqual(search.parameters[0].name, "query")
        self.assertEqual(search.parameters[1].accepted, ("curated", "broad", "local"))
        self.assertEqual(self.catalogue["status"].parameters, ())
        service = self.catalogue["service.create"]
        required_options = {
            parameter.name
            for parameter in service.parameters
            if parameter.required and parameter.kind is ParameterKind.OPTION
        }
        self.assertEqual(required_options, {"model_alias", "runtime"})
        self.assertEqual(
            self.catalogue["client.configure"].parameters[0].accepted,
            ("codex", "hindsight"),
        )
        rollback = self.catalogue["model.rollback"]
        self.assertEqual(
            [item.name for item in rollback.parameters], ["resource", "target"]
        )
        self.assertTrue(rollback.parameters[1].required)
        adopt = self.catalogue["model.adopt"]
        self.assertEqual(
            [item.name for item in adopt.parameters],
            ["repository", "revision", "path", "alias"],
        )
        self.assertTrue(adopt.parameters[1].required)
        self.assertTrue(adopt.parameters[2].required)
        self.assertEqual(self.catalogue["runtime.doctor"].parameters, ())
        self.assertEqual(self.catalogue["runtime.prune"].parameters, ())
        self.assertEqual(self.catalogue["model.cache.prune"].parameters, ())
        self.assertEqual(self.catalogue["doctor"].parameters, ())
        self.assertNotIn("operation.resume", self.catalogue)
        self.assertNotIn("operation.follow", self.catalogue)
        self.assertNotIn("operation.cancel", self.catalogue)
        setup = {
            parameter.name: parameter
            for parameter in self.catalogue["setup"].parameters
        }
        self.assertEqual(setup["service_options"].value_type, "json")
        self.assertEqual(setup["clients"].value_type, "json")
        self.assertEqual(setup["activation"].accepted, ("manual", "supervisor"))

    def test_cli_and_tui_capabilities_are_derived_from_same_entries(self) -> None:
        for operation in self.catalogue.values():
            with self.subTest(operation=operation.name):
                self.assertTrue(operation.cli)
                self.assertTrue(operation.tui)

    def test_catalogue_is_immutable_and_names_are_unique(self) -> None:
        with self.assertRaises(TypeError):
            self.catalogue["status"] = self.catalogue["check"]  # type: ignore[index]
        self.assertEqual(len(self.catalogue), len(set(self.catalogue)))


if __name__ == "__main__":
    unittest.main()
