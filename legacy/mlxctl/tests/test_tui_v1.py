import unittest

from textual.widgets import Static

from mlxctl.application.catalogue import build_operation_catalogue
from mlxctl.application.dispatch import OperationResult
from mlxctl.interfaces.tui import (
    MlxctlApp,
    ServiceSnapshot,
    TuiSnapshot,
)


class _Dispatcher:
    def __init__(self) -> None:
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return OperationResult(request.name, {"state": "complete"})


class _Snapshots:
    def snapshot(self) -> TuiSnapshot:
        return TuiSnapshot(
            supervisor="running",
            gateway="ready · 127.0.0.1:8766/v1",
            services=(
                ServiceSnapshot(
                    name="coding",
                    state="blocked",
                    model="qwen-optiq",
                    runtime="optiq@0.2.15",
                    pinned=True,
                    detail="--max-context is not advertised",
                ),
            ),
            active_operations=0,
            pressure="normal",
        )


class TuiV1Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.catalogue = build_operation_catalogue()
        self.dispatcher = _Dispatcher()
        self.app = MlxctlApp(self.dispatcher, self.catalogue, _Snapshots())

    async def test_operations_console_has_stable_nav_workspace_and_inspector(
        self,
    ) -> None:
        async with self.app.run_test(size=(140, 45)):
            self.assertIsNotNone(self.app.query_one("#resource-nav"))
            self.assertIsNotNone(self.app.query_one("#workspace"))
            self.assertIsNotNone(self.app.query_one("#inspector"))
            body = str(self.app.query_one("#view-body", Static).content)
            self.assertIn("coding", body)
            self.assertIn("Pinned", body)
            self.assertIn("blocked", body.lower())

    async def test_navigation_preserves_capability_and_changes_context(self) -> None:
        async with self.app.run_test(size=(140, 45)) as pilot:
            await pilot.click("#nav-services")
            title = str(self.app.query_one("#view-title", Static).content)
            self.assertEqual(title, "Inference Services")
            self.assertIn(
                "coding", str(self.app.query_one("#view-body", Static).content)
            )

            await pilot.click("#nav-topology")
            topology = str(self.app.query_one("#view-body", Static).content)
            self.assertIn("Model → Runtime → Service → Gateway", topology)

    async def test_first_run_is_intent_first_and_shows_exact_plan_before_change(
        self,
    ) -> None:
        async with self.app.run_test(size=(120, 40)) as pilot:
            await pilot.click("#first-run")
            title = str(self.app.query_one("#view-title", Static).content)
            body = str(self.app.query_one("#view-body", Static).content)
            self.assertEqual(title, "Create your first useful service")
            self.assertIn("exact Model Revision", body)
            self.assertIn("Nothing changes until", body)

    async def test_command_palette_exposes_every_catalogue_operation(self) -> None:
        async with self.app.run_test(size=(120, 40)) as pilot:
            self.assertEqual(self.app.available_operations, tuple(self.catalogue))
            await pilot.press("ctrl+p")
            self.assertTrue(self.app.screen_stack)

    async def test_help_explains_current_screen_and_shared_controls(self) -> None:
        async with self.app.run_test(size=(100, 35)) as pilot:
            await pilot.press("question_mark")
            body = str(self.app.query_one("#view-body", Static).content)
            self.assertIn("Ctrl+P", body)
            self.assertIn("same operation catalogue", body)
            self.assertIn("color", body.lower())


if __name__ == "__main__":
    unittest.main()
