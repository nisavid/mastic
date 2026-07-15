"""Textual TUI built from the same operation catalogue as the CLI."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from typing import Protocol

from textual import events
from textual.app import App, ComposeResult
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Static

from mlxctl.application.catalogue import Operation
from mlxctl.application.dispatch import OperationRequest

from .cli import Dispatcher


@dataclass(frozen=True, slots=True)
class ServiceSnapshot:
    name: str
    state: str
    model: str
    runtime: str
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

    def open_operation(self, name: str) -> None:
        operation = self.catalogue[name]
        self.current_view = f"operation:{name}"
        self.query_one("#view-title", Static).update(name.replace(".", "  ›  "))
        self.query_one("#view-body", Static).update(
            f"{operation.summary}\n\n"
            "This command is available in both the CLI and TUI. Select or enter "
            "the required resource here; mutations show their complete plan before "
            "confirmation.\n\n"
            f"CLI: {operation.examples[0]} --help"
        )

    def execute_operation(self, name: str, **parameters: object) -> None:
        """Execute one palette or contextual operation through shared dispatch."""
        result = self.dispatcher.execute(OperationRequest(name, parameters))
        self.notify(
            f"{name}: complete"
            + (" · Supervisor started" if result.supervisor_started else ""),
            title="mlxctl",
        )
        self.show_view(self.current_view)

    def show_view(self, name: str) -> None:
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
            return (
                "Resource topology",
                "Model → Runtime → Service → Gateway\n\n"
                "qwen-optiq → optiq@0.2.15 → coding [BLOCKED] → route:coding\n\n"
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
        generic = {
            "runtimes": (
                "Runtime Installations",
                "Known: mlx-lm · MLX-VLM · OptiQ\n\nList tested definitions, exact installed versions, provenance, capabilities, references, updates, rollback, and repair.",
            ),
            "models": (
                "Models",
                "Search mlx-community or all compatible candidates. Keep exact Model Revisions, managed installations, aliases, and shared cache bytes distinct.",
            ),
            "operations": (
                "Durable operations",
                "No active operations. Install, update, verify, repair, move, and prune jobs appear here with phase, progress, resume, cancel, and causal failure details.",
            ),
            "clients": (
                "Client Integrations",
                "Configure, preview, test, and remove Gateway integrations for Codex and Hindsight without disturbing unrelated settings.",
            ),
            "configuration": (
                "Configuration",
                "Edit typed desired state, validate it, preview semantic diffs, inspect history, import or export, and restore a backup without leaving mlxctl.",
            ),
            "doctor": (
                "Doctor",
                "One blocking issue: optiq@0.2.15 does not advertise --max-context. Install a tested compatible runtime side by side, probe it, dry-run coding, then switch atomically.",
            ),
        }
        return generic.get(name, (name.replace("-", " ").title(), "No data."))

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
