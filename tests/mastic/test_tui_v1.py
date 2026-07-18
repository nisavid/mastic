import unittest
from threading import Event

from rich.cells import cell_len
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from mastic.application.catalogue import build_operation_catalogue
from mastic.application.dispatch import ApplicationError, OperationResult
from mastic.application.status import (
    GatewaySnapshot,
    ServiceSnapshot,
    StatusSnapshot,
)
from mastic.interfaces.tui import MasticApp


class _Dispatcher:
    def __init__(self) -> None:
        self.requests = []
        self.previews = []
        self.error = None
        self.result_value = {"state": "complete"}
        self.execution_gate = None

    def preview(self, request):
        self.previews.append(request)
        if self.error is not None:
            raise self.error
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
        if self.execution_gate is not None:
            self.execution_gate.wait(timeout=2)
        if self.error is not None:
            raise self.error
        return OperationResult(request.name, self.result_value)


class _Snapshots:
    def __init__(self, gateway: GatewaySnapshot | None = None) -> None:
        self.gateway = gateway or GatewaySnapshot("ready", "127.0.0.1", 8766)

    def snapshot(self) -> StatusSnapshot:
        return StatusSnapshot(
            supervisor="running",
            gateway=self.gateway,
            services=(
                ServiceSnapshot(
                    name="coding",
                    state="blocked",
                    model="qwen-optiq",
                    runtime="optiq@0.2.15",
                    route="coding",
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
        self.app = MasticApp(self.dispatcher, self.catalogue, _Snapshots())

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

    async def test_machine_state_formats_structured_gateway_status(self) -> None:
        async with self.app.run_test(size=(140, 45)):
            state = str(self.app.query_one("#machine-state", Static).content)
            self.assertIn("Gateway ready · 127.0.0.1:8766", state)

            self.app.snapshots = _Snapshots(GatewaySnapshot("stopped"))
            self.app.show_view("home")

            state = str(self.app.query_one("#machine-state", Static).content)
            self.assertIn("Gateway stopped", state)
            self.assertNotIn("None", state)

    async def test_navigation_preserves_capability_and_changes_context(self) -> None:
        async with self.app.run_test(size=(140, 45)) as pilot:
            targets = self.app.query_one("#nav-application-targets", Button)
            self.assertEqual(
                str(targets.label),
                "⇄  Application Configuration Targets",
            )
            self.assertGreaterEqual(
                targets.size.width,
                cell_len(str(targets.label)) + 2,
            )

            await pilot.click("#nav-services")
            title = str(self.app.query_one("#view-title", Static).content)
            self.assertEqual(title, "Inference Services")
            self.assertIn(
                "coding", str(self.app.query_one("#view-body", Static).content)
            )

            await pilot.click("#nav-topology")
            topology = str(self.app.query_one("#view-body", Static).content)
            self.assertIn("Model → Runtime → Service → Gateway", topology)
            self.assertIn("qwen-optiq → optiq@0.2.15", topology)

            self.app.show_view("first-run")
            first_run = str(self.app.query_one("#view-body", Static).content)
            self.assertIn("Preview Application Configuration Targets", first_run)

    async def test_resource_views_query_live_read_only_operations(self) -> None:
        async with self.app.run_test(size=(120, 40)) as pilot:
            await pilot.click("#nav-models")

            self.assertEqual(self.dispatcher.requests[-1].name, "model.list")
            body = str(self.app.query_one("#view-body", Static).content)
            self.assertIn("State", body)
            self.assertIn("complete", body)

    async def test_context_actions_and_command_catalogue_are_discoverable(self) -> None:
        async with self.app.run_test(size=(140, 45)) as pilot:
            await pilot.click("#find-model")
            self.assertEqual(self.app.selected_operation, "model.search")

            self.app.show_view("commands")
            body = str(self.app.query_one("#view-body", Static).content)
            self.assertIn("Every CLI operation is available here", body)
            self.assertIn("model.search", body)
            self.assertIn("service.stop", body)
            self.assertIn("supervisor.stop", body)

    async def test_first_run_is_intent_first_and_shows_exact_preview_before_change(
        self,
    ) -> None:
        async with self.app.run_test(size=(120, 40)) as pilot:
            await pilot.click("#first-run")
            title = str(self.app.query_one("#view-title", Static).content)
            body = str(self.app.query_one("#view-body", Static).content)
            self.assertEqual(title, "setup")
            self.assertEqual(self.app.selected_operation, "setup")
            self.assertIn("resolved preview", body)
            self.assertTrue(
                self.app.query_one("#operation-form").styles.display == "block"
            )
            capacity = self.app.query_one("#parameter-capacity", Select)
            labels = {str(label) for label, _value in capacity._options}
            self.assertTrue(any("balanced" in label.lower() for label in labels))
            self.assertTrue(any("long-context" in label for label in labels))
            self.assertTrue(any("native-context" in label for label in labels))

    async def test_command_palette_exposes_every_catalogue_operation(self) -> None:
        async with self.app.run_test(size=(120, 40)) as pilot:
            self.assertEqual(self.app.available_operations, tuple(self.catalogue))
            await pilot.press("ctrl+p")
            self.assertTrue(self.app.screen_stack)

    async def test_command_palette_opens_the_selected_operation_workbench(self) -> None:
        async with self.app.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+p")
            await pilot.press(*"resolve cache verify name")
            await pilot.pause()
            await pilot.press("down", "enter")
            await pilot.pause()

            self.assertEqual(self.app.selected_operation, "model.install")
            self.assertIsInstance(self.app.query_one("#parameter-repository"), Input)

    async def test_operation_view_renders_parameter_specific_controls(self) -> None:
        async with self.app.run_test(size=(120, 45)) as pilot:
            await self.app.open_operation("service.create")

            self.assertEqual(
                self.app.query_one("#parameter-service", Input).placeholder,
                "Required",
            )
            self.assertIsInstance(self.app.query_one("#parameter-model_alias"), Input)
            self.assertIsInstance(self.app.query_one("#parameter-runtime"), Input)
            self.assertIsInstance(self.app.query_one("#parameter-route"), Input)
            self.assertIsInstance(self.app.query_one("#parameter-pinned"), Checkbox)
            self.assertFalse(self.app.query_one("#workspace-actions").display)
            self.assertEqual(self.app.focused.id, "parameter-service")
            labels = "\n".join(str(label.content) for label in self.app.query(Label))
            self.assertIn("Service · Argument · required", labels)
            self.assertIn("Model Alias · Option --model-alias · required", labels)

            await self.app.open_operation("runtime.install")
            runtime = self.app.query_one("#parameter-runtime", Select)
            runtime.focus()
            await pilot.press("enter", "o", "p", "t", "i", "q", "enter")
            self.assertEqual(runtime.value, "optiq")

            await self.app.open_operation("service.edit")
            self.assertIsInstance(self.app.query_one("#parameter-pinned"), Select)

    async def test_service_edit_can_explicitly_clear_a_boolean(self) -> None:
        async with self.app.run_test(size=(120, 45)) as pilot:
            await self.app.open_operation("service.edit")
            self.app.query_one("#parameter-resource", Input).value = "coding"
            self.app.query_one("#parameter-pinned", Select).value = "false"

            self.app.query_one("#operation-submit").press()
            await pilot.pause()
            self.app.query_one("#operation-confirm").press()
            await pilot.pause()

            self.assertIs(self.dispatcher.requests[-1].parameters["pinned"], False)

    async def test_long_operation_worker_keeps_navigation_responsive(self) -> None:
        gate = Event()
        self.dispatcher.execution_gate = gate
        try:
            async with self.app.run_test(size=(120, 45)) as pilot:
                await self.app.open_operation("model.search")
                self.app.query_one("#operation-submit").press()
                await pilot.pause()

                await pilot.click("#nav-topology")

                self.assertEqual(
                    str(self.app.query_one("#view-title", Static).content),
                    "Resource topology",
                )
                self.assertFalse(gate.is_set())
                gate.set()
                await pilot.pause()
        finally:
            gate.set()

    async def test_confirmed_mutation_shows_exact_preview_before_dispatch(self) -> None:
        async with self.app.run_test(size=(120, 45)) as pilot:
            await self.app.open_operation("model.install")
            self.app.query_one(
                "#parameter-repository", Input
            ).value = "mlx-community/Qwen3-4B-4bit"
            self.app.query_one("#parameter-revision", Input).value = "abc123"
            self.app.query_one("#parameter-alias", Input).value = "coding"

            self.app.query_one("#operation-submit").press()
            await pilot.pause()

            self.assertEqual(self.dispatcher.requests, [])
            self.assertEqual(self.dispatcher.previews[-1].name, "model.install")
            preview = str(self.app.query_one("#view-body", Static).content)
            self.assertIn("Resolved mutation preview", preview)
            self.assertIn("Resolved backend preview", preview)
            self.assertIn("model.install", preview)
            self.assertIn("mlx-community/Qwen3-4B-4bit", preview)
            self.assertIn("revision: abc123", preview)
            self.assertIn("offline: False", preview)
            self.assertEqual(self.app.focused.id, "operation-confirm")

            self.app.query_one("#operation-confirm").press()
            await pilot.pause()
            self.assertEqual(len(self.dispatcher.requests), 1)
            self.assertEqual(
                dict(self.dispatcher.requests[0].parameters),
                {
                    "repository": "mlx-community/Qwen3-4B-4bit",
                    "revision": "abc123",
                    "alias": "coding",
                    "confirmed": True,
                },
            )
            self.assertEqual(self.app.focused.id, "operation-submit")

    async def test_setup_confirmation_carries_the_reviewed_preview_fingerprint(
        self,
    ) -> None:
        async with self.app.run_test(size=(120, 45)) as pilot:
            await self.app.open_operation("setup")
            self.app.query_one("#operation-submit").press()
            await pilot.pause()
            self.app.query_one("#operation-confirm").press()
            await pilot.pause()

            self.assertEqual(
                self.dispatcher.requests[-1].parameters["preview_fingerprint"],
                "sha256:exact",
            )

    async def test_every_catalogue_operation_can_be_executed_from_tui(self) -> None:
        async with self.app.run_test(size=(140, 55)) as pilot:
            for name, operation in self.catalogue.items():
                with self.subTest(operation=name):
                    await self.app.open_operation(name)
                    for parameter in operation.parameters:
                        control = self.app.query_one(f"#parameter-{parameter.name}")
                        if parameter.required and isinstance(control, Input):
                            if parameter.value_type == "integer":
                                control.value = "1"
                            elif parameter.value_type == "json":
                                control.value = "[]"
                            else:
                                control.value = "example"
                        elif parameter.required and isinstance(control, Select):
                            control.value = parameter.accepted[0]
                    before = len(self.dispatcher.requests)
                    self.app.query_one("#operation-submit").press()
                    await pilot.pause()
                    if operation.confirmation:
                        self.assertEqual(len(self.dispatcher.requests), before)
                        self.app.query_one("#operation-confirm").press()
                        await pilot.pause()
                    self.assertEqual(len(self.dispatcher.requests), before + 1)
                    self.assertEqual(self.dispatcher.requests[-1].name, name)
                    if operation.confirmation:
                        self.assertTrue(
                            self.dispatcher.requests[-1].parameters["confirmed"]
                        )

    async def test_results_errors_and_next_actions_stay_in_the_workspace(self) -> None:
        async with self.app.run_test(size=(120, 45)) as pilot:
            self.dispatcher.result_value = {
                "state": "ready",
                "next_actions": ["mastic service start coding"],
            }
            await self.app.open_operation("status")
            self.app.query_one("#operation-submit").press()
            await pilot.pause()
            success = str(self.app.query_one("#view-body", Static).content)
            self.assertIn("State", success)
            self.assertIn("ready", success)
            self.assertIn("Next actions", success)
            self.assertIn("mastic service start coding", success)

            self.dispatcher.error = ApplicationError(
                "runtime_probe_failed",
                "The selected runtime did not advertise a required capability.",
                next_actions=(
                    "inspect the runtime probe",
                    "install the tested runtime",
                ),
            )
            await self.app.open_operation("runtime.inspect")
            self.app.query_one("#parameter-resource", Input).value = "optiq@0.2.15"
            self.app.query_one("#operation-submit").press()
            await pilot.pause()
            failure = str(self.app.query_one("#view-body", Static).content)
            self.assertIn("runtime_probe_failed", failure)
            self.assertIn("required capability", failure)
            self.assertIn("inspect the runtime probe", failure)
            self.assertIn("install the tested runtime", failure)

    async def test_read_only_browsing_dispatches_only_safe_queries(self) -> None:
        async with self.app.run_test(size=(120, 45)) as pilot:
            await pilot.click("#nav-models")
            await pilot.click("#nav-topology")
            await self.app.open_operation("model.search")

            self.assertEqual(
                [request.name for request in self.dispatcher.requests], ["model.list"]
            )

    async def test_cancelled_preview_makes_no_change_and_preserves_inputs(self) -> None:
        async with self.app.run_test(size=(100, 40)) as pilot:
            await self.app.open_operation("service.remove")
            resource = self.app.query_one("#parameter-resource", Input)
            resource.value = "coding"
            self.app.query_one("#operation-submit").press()
            await pilot.pause()
            self.app.query_one("#operation-cancel").press()
            await pilot.pause()

            self.assertEqual(self.dispatcher.requests, [])
            self.assertEqual(resource.value, "coding")
            body = str(self.app.query_one("#view-body", Static).content)
            self.assertIn("No changes made", body)
            self.assertIn("editable inputs", body)
            self.assertEqual(self.app.focused.id, "operation-submit")

    async def test_required_and_integer_input_errors_are_shown_in_surface(self) -> None:
        async with self.app.run_test(size=(100, 40)) as pilot:
            await self.app.open_operation("service.create")
            self.app.query_one("#operation-submit").press()
            await pilot.pause()
            required = str(self.app.query_one("#view-body", Static).content)
            self.assertIn("Service is required", required)
            self.assertEqual(self.dispatcher.requests, [])

            await self.app.open_operation("model.search")
            self.app.query_one("#parameter-limit", Input).value = "many"
            self.app.query_one("#operation-submit").press()
            await pilot.pause()
            integer = str(self.app.query_one("#view-body", Static).content)
            self.assertIn("Limit must be a whole number", integer)
            self.assertEqual(self.dispatcher.requests, [])

    async def test_narrow_layout_keeps_complete_operation_controls(self) -> None:
        async with self.app.run_test(size=(72, 35)):
            await self.app.open_operation("model.search")

            self.assertEqual(self.app.query_one("#resource-nav").styles.display, "none")
            self.assertEqual(self.app.query_one("#inspector").styles.display, "none")
            self.assertTrue(self.app.query_one("#operation-form").display)
            self.assertIsInstance(self.app.query_one("#parameter-query"), Input)
            self.assertIsInstance(self.app.query_one("#parameter-source"), Select)
            self.assertIsInstance(self.app.query_one("#parameter-limit"), Input)

    async def test_medium_layout_preserves_workspace_width(self) -> None:
        async with self.app.run_test(size=(100, 35)):
            self.assertEqual(
                self.app.query_one("#resource-nav").styles.display, "block"
            )
            self.assertEqual(self.app.query_one("#resource-nav").styles.width.value, 41)
            self.assertEqual(self.app.query_one("#inspector").styles.display, "none")

    async def test_help_explains_current_screen_and_shared_controls(self) -> None:
        async with self.app.run_test(size=(100, 35)) as pilot:
            await pilot.press("question_mark")
            body = str(self.app.query_one("#view-body", Static).content)
            self.assertIn("Ctrl+P", body)
            self.assertIn("same operation catalogue", body)
            self.assertIn("color", body.lower())


if __name__ == "__main__":
    unittest.main()
