import unittest

from mlxctl.application.catalogue import (
    OperationKind,
    SupervisorRequirement,
    build_operation_catalogue,
)


class OperationCatalogueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalogue = build_operation_catalogue()

    def test_contains_the_complete_approved_command_tree(self) -> None:
        required = {
            "setup",
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
            "model.cache.evict",
            "service.create",
            "service.start",
            "service.stop",
            "operation.follow",
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
            "gateway.configure",
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

    def test_mutations_declare_confirmation_and_machine_help(self) -> None:
        install = self.catalogue["model.install"]
        self.assertTrue(install.confirmation)
        self.assertIn("exact revision", install.summary.lower())
        self.assertTrue(install.examples)
        self.assertIn("json", install.output_modes)

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
