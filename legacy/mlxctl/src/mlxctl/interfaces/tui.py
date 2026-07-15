"""Textual TUI built from the same operation catalogue as the CLI."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from typing import Protocol

from textual import events
from textual.app import App, ComposeResult
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Footer, Input, Label, Select, Static

from mlxctl.application.catalogue import Operation, ParameterKind
from mlxctl.application.dispatch import ApplicationError, OperationRequest

from .cli import Dispatcher


@dataclass(frozen=True, slots=True)
class ServiceSnapshot:
    name: str
    state: str
    model: str
    runtime: str
    route: str | None = None
    pinned: bool = False
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class TuiSnapshot:
    supervisor: str
    gateway: str
    services: tuple[ServiceSnapshot, ...] = ()
    active_operations: int = 0
    pressure: str = "unknown"


class SnapshotProvider(Protocol):
    def snapshot(self) -> TuiSnapshot: ...


class OperationCommands(Provider):
    """Expose every shared operation through Textual's command palette."""

    async def search(self, query: str) -> Hits:
        app = self.app
        assert isinstance(app, MlxctlApp)
        matcher = self.matcher(query)
        for name, operation in app.catalogue.items():
            prompt = name.replace(".", "  ›  ")
            score = matcher.match(prompt + " " + operation.summary)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(prompt),
                    partial(app.open_operation, name),
                    help=operation.summary,
                )


class MlxctlApp(App[None]):
    """A quiet operations console with intent and topology workspaces."""

    TITLE = "mlxctl"
    SUB_TITLE = "local inference control room"
    COMMANDS = App.COMMANDS | {OperationCommands}
    COMMAND_PALETTE_BINDING = "ctrl+p"
    BINDINGS = [
        ("ctrl+p", "command_palette", "Commands"),
        ("question_mark", "help", "Help"),
        ("q", "quit", "Quit"),
    ]
    CSS = """
    Screen {
        background: #090d0c;
        color: #e7ecea;
    }
    #topbar {
        height: 3;
        padding: 1 2;
        background: #0c1210;
        border-bottom: solid #2b3733;
        color: #89e2a2;
        text-style: bold;
    }
    #machine-state {
        height: 2;
        padding: 0 2;
        background: #0c1210;
        color: #8a9691;
    }
    #shell { height: 1fr; }
    #resource-nav {
        width: 25;
        padding: 1;
        background: #0c1210;
        border-right: solid #2b3733;
    }
    #resource-nav Button {
        width: 100%;
        height: 3;
        margin: 0;
        border: none;
        background: transparent;
        color: #8a9691;
        text-align: left;
    }
    #resource-nav Button:hover, #resource-nav Button:focus {
        background: #171f1d;
        color: #e7ecea;
        text-style: bold;
    }
    #workspace {
        width: 1fr;
        padding: 1 2;
        background: #090d0c;
    }
    #view-title {
        height: 3;
        color: #e7ecea;
        text-style: bold;
    }
    #view-body {
        width: 100%;
        min-height: 12;
        padding: 1;
        background: #111715;
        border: solid #2b3733;
    }
    #workspace-actions {
        height: 4;
        margin-top: 1;
    }
    #workspace-actions Button {
        margin-right: 1;
        min-width: 18;
    }
    #operation-form {
        display: none;
        width: 100%;
        height: auto;
        margin-top: 1;
        padding: 1;
        background: #111715;
        border: solid #2b3733;
    }
    #operation-form .parameter-label {
        height: auto;
        margin-top: 1;
        color: #e7ecea;
        text-style: bold;
    }
    #operation-form .parameter-help {
        height: auto;
        color: #8a9691;
    }
    #operation-form Input, #operation-form Select {
        width: 100%;
        margin-bottom: 1;
    }
    #operation-form Checkbox { margin-bottom: 1; }
    #operation-form .operation-buttons {
        height: 4;
        margin-top: 1;
    }
    #operation-form .operation-buttons Button {
        min-width: 18;
        margin-right: 1;
    }
    #first-run {
        background: #e7ecea;
        color: #090d0c;
        text-style: bold;
    }
    #inspector {
        width: 30;
        padding: 1 2;
        background: #0c1210;
        border-left: solid #2b3733;
        color: #8a9691;
    }
    Footer { background: #111715; color: #8a9691; }
    """

    def __init__(
        self,
        dispatcher: Dispatcher,
        catalogue: Mapping[str, Operation],
        snapshots: SnapshotProvider,
    ) -> None:
        super().__init__()
        self.dispatcher = dispatcher
        self.catalogue = catalogue
        self.snapshots = snapshots
        self.current_view = "home"
        self.selected_operation: str | None = None
        self.pending_parameters: Mapping[str, object] | None = None

    @property
    def available_operations(self) -> tuple[str, ...]:
        return tuple(self.catalogue)

    def compose(self) -> ComposeResult:
        yield Static("◈  mlxctl", id="topbar")
        yield Static("", id="machine-state")
        with Horizontal(id="shell"):
            with Vertical(id="resource-nav"):
                yield Button("⌂  Overview", id="nav-home")
                yield Button("⬡  Runtimes", id="nav-runtimes")
                yield Button("▱  Models", id="nav-models")
                yield Button("◆  Services", id="nav-services")
                yield Button("↻  Operations", id="nav-operations")
                yield Button("⇄  Clients", id="nav-clients")
                yield Button("⌁  Topology", id="nav-topology")
                yield Button("⚙  Configuration", id="nav-configuration")
                yield Button("✚  Doctor", id="nav-doctor")
            with VerticalScroll(id="workspace"):
                yield Static("", id="view-title")
                yield Static("", id="view-body")
                yield Vertical(id="operation-form")
                with Horizontal(id="workspace-actions"):
                    yield Button("Create service", id="first-run")
                    yield Button("Open topology", id="open-topology")
                    yield Button("Refresh", id="refresh")
            yield Static("", id="inspector")
        yield Footer()

    def on_mount(self) -> None:
        self.show_view("home")
        self._apply_responsive_layout(self.size.width)

    def on_resize(self, event: events.Resize) -> None:
        self._apply_responsive_layout(event.size.width)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        identity = event.button.id or ""
        if identity.startswith("nav-"):
            self.show_view(identity.removeprefix("nav-"))
        elif identity == "first-run":
            self.show_view("first-run")
        elif identity == "open-topology":
            self.show_view("topology")
        elif identity == "refresh":
            self.show_view(self.current_view)
        elif identity == "operation-submit":
            self._submit_operation()
        elif identity == "operation-confirm":
            self._confirm_operation()
        elif identity == "operation-cancel":
            self._cancel_operation()

    def action_help(self) -> None:
        self.show_view("help")

    def _apply_responsive_layout(self, width: int) -> None:
        nav = self.query_one("#resource-nav", Vertical)
        inspector = self.query_one("#inspector", Static)
        workspace = self.query_one("#workspace", VerticalScroll)
        nav.styles.display = "none" if width < 80 else "block"
        nav.styles.width = 21 if width < 100 else 25
        inspector.styles.display = "none" if width < 100 else "block"
        workspace.styles.padding = (1, 1) if width < 80 else (1, 2)

    async def open_operation(self, name: str) -> None:
        operation = self.catalogue[name]
        self.selected_operation = name
        self.pending_parameters = None
        self.current_view = f"operation:{name}"
        self.query_one("#view-title", Static).update(name.replace(".", "  ›  "))
        self.query_one("#view-body", Static).update(
            f"{operation.summary}\n\n"
            f"{operation.kind.value.title()} · Supervisor: "
            f"{operation.supervisor.value.replace('_', ' ')}\n\n"
            "Set the operation inputs below. Read-only operations do not start the "
            "Supervisor. Confirmed mutations show their complete plan before any "
            "change.\n\n"
            f"CLI equivalent: {operation.examples[0]} --help"
        )
        form = self.query_one("#operation-form", Vertical)
        form.styles.display = "block"
        self.query_one("#workspace-actions", Horizontal).styles.display = "none"
        await form.remove_children()
        widgets: list[Static | Input | Select[str] | Checkbox | Horizontal] = []
        for parameter in operation.parameters:
            requirement = "required" if parameter.required else "optional"
            surface = (
                "Argument"
                if parameter.kind is ParameterKind.ARGUMENT
                else f"Option {parameter.flag or '--' + parameter.name.replace('_', '-')}"
            )
            widgets.append(
                Label(
                    f"{parameter.name.replace('_', ' ').title()} · {surface} · {requirement}",
                    classes="parameter-label",
                )
            )
            accepted = (
                f" Accepted values: {', '.join(parameter.accepted)}."
                if parameter.accepted
                else ""
            )
            widgets.append(
                Static(
                    parameter.help + accepted,
                    classes="parameter-help",
                )
            )
            identifier = f"parameter-{parameter.name}"
            if parameter.value_type == "boolean":
                widgets.append(
                    Checkbox(f"Set {parameter.flag or parameter.name}", id=identifier)
                )
            elif parameter.accepted:
                widgets.append(
                    Select(
                        [(value, value) for value in parameter.accepted],
                        prompt="Choose a value",
                        allow_blank=not parameter.required,
                        id=identifier,
                    )
                )
            else:
                widgets.append(
                    Input(
                        placeholder="Required" if parameter.required else "Optional",
                        type="integer" if parameter.value_type == "integer" else "text",
                        id=identifier,
                    )
                )
        submit_label = (
            "Review complete plan" if operation.confirmation else "Run operation"
        )
        widgets.append(
            Horizontal(
                Button(submit_label, id="operation-submit", variant="primary"),
                Button("Confirm change", id="operation-confirm", variant="warning"),
                Button("Cancel", id="operation-cancel"),
                classes="operation-buttons",
            )
        )
        await form.mount(*widgets)
        self.query_one("#operation-confirm", Button).styles.display = "none"
        self.query_one("#operation-cancel", Button).styles.display = "none"
        focus_target = (
            f"#parameter-{operation.parameters[0].name}"
            if operation.parameters
            else "#operation-submit"
        )
        self.set_focus(self.query_one(focus_target))

    def execute_operation(self, name: str, **parameters: object) -> None:
        """Execute one palette or contextual operation through shared dispatch."""
        try:
            result = self.dispatcher.execute(OperationRequest(name, parameters))
        except ApplicationError as error:
            actions = "\n".join(f"  → {action}" for action in error.next_actions)
            body = f"{error.code}\n\n{error.message}"
            if actions:
                body += f"\n\nNext actions\n{actions}"
            self.query_one("#view-title", Static).update(
                f"{name.replace('.', '  ›  ')} · failed"
            )
            self.query_one("#view-body", Static).update(body)
            self.notify(error.message, title="Operation failed", severity="error")
            return
        self.notify(
            f"{name}: complete"
            + (" · Supervisor started" if result.supervisor_started else ""),
            title="mlxctl",
        )
        rendered = json.dumps(dict(result.value), indent=2, sort_keys=True, default=str)
        events = ""
        if result.events:
            events = "\n\nEvents\n" + "\n".join(
                json.dumps(dict(event), sort_keys=True, default=str)
                for event in result.events
            )
        next_actions_value = result.value.get("next_actions", ())
        if isinstance(next_actions_value, str):
            next_actions = (next_actions_value,)
        elif isinstance(next_actions_value, tuple | list):
            next_actions = tuple(str(item) for item in next_actions_value)
        else:
            next_actions = ()
        next_text = "\n".join(f"  → {action}" for action in next_actions)
        if not next_text:
            next_text = "  → Open Ctrl+P for another operation"
        self.query_one("#view-title", Static).update(
            f"{name.replace('.', '  ›  ')} · complete"
        )
        self.query_one("#view-body", Static).update(
            f"Result\n{rendered}{events}\n\nNext actions\n{next_text}"
        )
        self.pending_parameters = None
        self.query_one("#operation-confirm", Button).styles.display = "none"
        self.query_one("#operation-cancel", Button).styles.display = "none"
        self.query_one("#operation-submit", Button).styles.display = "block"
        self.set_focus(self.query_one("#operation-submit", Button))

    def show_view(self, name: str) -> None:
        self.selected_operation = None
        self.pending_parameters = None
        self.query_one("#operation-form", Vertical).styles.display = "none"
        self.query_one("#workspace-actions", Horizontal).styles.display = "block"
        snapshot = self.snapshots.snapshot()
        self.current_view = name
        self.query_one("#machine-state", Static).update(
            f"● Supervisor {snapshot.supervisor}   ● Gateway {snapshot.gateway}   "
            f"Pressure {snapshot.pressure}   Operations {snapshot.active_operations}"
        )
        title, body = self._content(name, snapshot)
        self.query_one("#view-title", Static).update(title)
        self.query_one("#view-body", Static).update(body)
        self.query_one("#inspector", Static).update(self._inspector(snapshot))

    def _submit_operation(self) -> None:
        if self.selected_operation is None:
            return
        operation = self.catalogue[self.selected_operation]
        try:
            parameters = self._operation_parameters(operation)
        except ValueError as error:
            self.query_one("#view-body", Static).update(
                f"Check the highlighted operation inputs.\n\n{error}"
            )
            self.notify(str(error), title="Input needed", severity="warning")
            return
        if not operation.confirmation:
            self.execute_operation(operation.name, **parameters)
            return
        try:
            preview = self.dispatcher.preview(
                OperationRequest(operation.name, parameters)
            )
        except ApplicationError as error:
            actions = "\n".join(f"  → {action}" for action in error.next_actions)
            body = f"{error.code}\n\n{error.message}"
            if actions:
                body += f"\n\nNext actions\n{actions}"
            self.query_one("#view-body", Static).update(body)
            self.notify(error.message, title="Plan failed", severity="error")
            return
        self.pending_parameters = parameters
        resolved = json.dumps(
            dict(preview.value), indent=2, sort_keys=True, default=str
        )
        self.query_one("#view-body", Static).update(
            self._mutation_plan(operation, parameters)
            + f"\n\nResolved backend plan\n{resolved}"
        )
        self.query_one("#operation-submit", Button).styles.display = "none"
        self.query_one("#operation-confirm", Button).styles.display = "block"
        self.query_one("#operation-cancel", Button).styles.display = "block"
        self.set_focus(self.query_one("#operation-confirm", Button))

    def _confirm_operation(self) -> None:
        if self.selected_operation is None or self.pending_parameters is None:
            return
        operation = self.catalogue[self.selected_operation]
        parameters = dict(self.pending_parameters)
        parameters["confirmed"] = True
        self.execute_operation(operation.name, **parameters)

    def _cancel_operation(self) -> None:
        if self.selected_operation is None:
            return
        operation = self.catalogue[self.selected_operation]
        self.pending_parameters = None
        self.query_one("#view-body", Static).update(
            f"No changes made.\n\n{operation.summary}\n\n"
            "The editable inputs are preserved. Review them, then preview the "
            "complete plan again."
        )
        self.query_one("#operation-submit", Button).styles.display = "block"
        self.query_one("#operation-confirm", Button).styles.display = "none"
        self.query_one("#operation-cancel", Button).styles.display = "none"
        self.set_focus(self.query_one("#operation-submit", Button))

    def _operation_parameters(self, operation: Operation) -> dict[str, object]:
        values: dict[str, object] = {}
        for parameter in operation.parameters:
            control = self.query_one(f"#parameter-{parameter.name}")
            value: object
            if isinstance(control, Checkbox):
                value = control.value
            elif isinstance(control, Select):
                value = None if control.value is Select.BLANK else control.value
            elif isinstance(control, Input):
                value = control.value.strip() or None
                if value is not None and parameter.value_type == "integer":
                    try:
                        value = int(value)
                    except ValueError as error:
                        raise ValueError(
                            f"{parameter.name.replace('_', ' ').title()} must be a whole number."
                        ) from error
            else:  # pragma: no cover - Textual form construction is exhaustive.
                raise TypeError(f"unsupported control for {parameter.name}")
            if parameter.required and value is None:
                raise ValueError(
                    f"{parameter.name.replace('_', ' ').title()} is required."
                )
            if value is not None and value is not False:
                values[parameter.name] = value
        return values

    @staticmethod
    def _mutation_plan(operation: Operation, parameters: Mapping[str, object]) -> str:
        rows = []
        for parameter in operation.parameters:
            value = parameters.get(
                parameter.name,
                False if parameter.value_type == "boolean" else "not set",
            )
            rows.append(f"  {parameter.name}: {value}")
        parameter_text = "\n".join(rows) if rows else "  No operation inputs"
        supervisor = operation.supervisor.value.replace("_", " ")
        return (
            "Complete mutation plan\n\n"
            f"Operation: {operation.name}\n"
            f"Intent: {operation.summary}\n"
            f"Supervisor: {supervisor}\n"
            f"Confirmation: required\n\nInputs\n{parameter_text}\n\n"
            "No change has been made. Confirm to apply this exact plan, or cancel "
            "to return to the editable inputs."
        )

    def _content(self, name: str, snapshot: TuiSnapshot) -> tuple[str, str]:
        if name == "home":
            services = self._service_rows(snapshot)
            empty = (
                "No Inference Services yet. Create one to choose an exact model, "
                "tested runtime, stable route, and verification request."
                if not snapshot.services
                else services
            )
            return (
                "Operations",
                "Your local inference system at a glance.\n\n"
                + empty
                + "\n\nNext useful action: resolve blockers, or create a service.",
            )
        if name == "services":
            return (
                "Inference Services",
                self._service_rows(snapshot)
                or "No services. Create one through guided setup or the service builder.",
            )
        if name == "topology":
            topology = "\n".join(
                f"{item.model} → {item.runtime} → {item.name} "
                f"[{item.state.upper()}] → route:{item.route or item.name}"
                for item in snapshot.services
            )
            return (
                "Resource topology",
                "Model → Runtime → Service → Gateway\n\n"
                + (topology or "No configured topology yet.")
                + "\n\n"
                "Desired state and the latest Service Run stay separate. Open a "
                "node for evidence, configuration, runs, logs, metrics, and repair.",
            )
        if name == "first-run":
            return (
                "Create your first useful service",
                "1  Check this Mac\n"
                "2  Review the machine-aware recommended profile\n"
                "3  Pin an exact Model Revision and inspect fit, trust, and cache\n"
                "4  Install and probe the tested Runtime Installation\n"
                "5  Name the service and stable Gateway route\n"
                "6  Preview clients, resource effects, and verification request\n\n"
                "Nothing changes until the complete plan is reviewed and confirmed. "
                "Every recommendation remains editable.",
            )
        if name == "help":
            return (
                "Help for this screen",
                "Ctrl+P opens every operation from the same operation catalogue used "
                "by the CLI. Tab and arrow keys move focus; Enter opens; / filters "
                "resource lists; q quits.\n\nState uses words and symbols as well as "
                "color. Narrow terminals collapse panes without removing operations. "
                "Read-only screens never start the Supervisor.",
            )
        query_views = {
            "runtimes": ("Runtime Installations", "runtime.list"),
            "models": ("Models", "model.list"),
            "operations": ("Durable operations", "operation.list"),
            "clients": ("Client Integrations", "client.list"),
            "configuration": ("Configuration", "config.show"),
            "doctor": ("Doctor", "doctor"),
        }
        if name in query_views:
            title, operation = query_views[name]
            try:
                result = self.dispatcher.execute(OperationRequest(operation))
                body = json.dumps(
                    dict(result.value), indent=2, sort_keys=True, default=str
                )
            except ApplicationError as error:
                body = f"{error.code}\n\n{error.message}"
                if error.next_actions:
                    body += "\n\nNext actions\n" + "\n".join(
                        f"  → {action}" for action in error.next_actions
                    )
            return title, body
        return name.replace("-", " ").title(), "No data."

    @staticmethod
    def _service_rows(snapshot: TuiSnapshot) -> str:
        return "\n".join(
            f"{'📌 Pinned' if item.pinned else 'Unpinned'}  {item.name:<16} "
            f"{item.state.upper():<10}  {item.model} · {item.runtime}"
            + (f"\n    {item.detail}" if item.detail else "")
            for item in snapshot.services
        )

    @staticmethod
    def _inspector(snapshot: TuiSnapshot) -> str:
        if not snapshot.services:
            return (
                "SELECTED\n\nNo service selected.\n\n"
                "Create a service or open Ctrl+P to choose an operation."
            )
        service = snapshot.services[0]
        return (
            f"SELECTED SERVICE\n\n{service.name}\n{service.state.upper()}\n\n"
            f"Model\n{service.model}\n\nRuntime\n{service.runtime}\n\n"
            f"Pressure policy\n{'📌 Pinned · never auto-stopped' if service.pinned else 'LRU idle eviction allowed'}"
        )
