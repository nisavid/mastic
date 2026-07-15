"""Typer/Rich CLI generated from the shared operation catalogue."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Annotated, Protocol

import typer
from rich.console import Console
from rich.pretty import Pretty

from mlxctl.application.catalogue import Operation
from mlxctl.application.dispatch import (
    ApplicationError,
    OperationRequest,
    OperationResult,
)


class Dispatcher(Protocol):
    def execute(self, request: OperationRequest) -> OperationResult: ...


_ROOT_COMMANDS = ("setup", "status", "check", "doctor", "logs", "metrics")
_GROUPS = (
    "supervisor",
    "gateway",
    "runtime",
    "model",
    "service",
    "operation",
    "client",
    "config",
)
_NO_RESOURCE = frozenset(
    {
        "runtime.list",
        "runtime.available",
        "model.search",
        "model.list",
        "model.cache.list",
        "service.list",
        "service.create",
        "operation.list",
        "client.list",
        "config.path",
        "config.show",
        "config.validate",
        "config.diff",
        "config.history",
        "config.export",
        "config.import",
        "config.restore",
    }
)


def build_cli(
    dispatcher: Dispatcher,
    catalogue: Mapping[str, Operation],
    *,
    tui_launcher: Callable[[], int],
) -> typer.Typer:
    """Build the complete discoverable command tree from one catalogue."""
    app = typer.Typer(
        name="mlxctl",
        help=(
            "Manage local MLX runtimes, models, named Inference Services, the "
            "stable Gateway, and the explicit Supervisor."
        ),
        invoke_without_command=True,
        no_args_is_help=False,
        rich_markup_mode="rich",
        context_settings={"help_option_names": ["-h", "--help"]},
    )

    @app.callback()
    def root(ctx: typer.Context) -> None:
        """Open the TUI by default when no command is provided."""
        if ctx.invoked_subcommand is None:
            raise typer.Exit(tui_launcher())

    for name in _ROOT_COMMANDS:
        _add_command(app, name, catalogue[name], dispatcher, requires_resource=False)

    @app.command("tui", help=catalogue["tui"].summary)
    def tui() -> None:
        raise typer.Exit(tui_launcher())

    groups: dict[str, typer.Typer] = {}
    for group in _GROUPS:
        group_app = typer.Typer(
            name=group,
            help=_group_help(group),
            no_args_is_help=True,
            rich_markup_mode="rich",
        )
        app.add_typer(group_app, name=group)
        groups[group] = group_app

    cache_app = typer.Typer(
        name="cache",
        help="Inspect and safely manage physical shared model-cache bytes.",
        no_args_is_help=True,
        rich_markup_mode="rich",
    )
    groups["model"].add_typer(cache_app, name="cache")
    groups["model.cache"] = cache_app

    for operation_name, operation in catalogue.items():
        if "." not in operation_name:
            continue
        group, command = operation_name.rsplit(".", 1)
        group_app = groups.get(group)
        if group_app is None:
            continue
        requires_resource = operation_name not in _NO_RESOURCE and group not in {
            "supervisor",
            "gateway",
        }
        _add_command(
            group_app,
            command,
            operation,
            dispatcher,
            requires_resource=requires_resource,
        )
    return app


def _add_command(
    app: typer.Typer,
    command_name: str,
    operation: Operation,
    dispatcher: Dispatcher,
    *,
    requires_resource: bool,
) -> None:
    help_text = operation.summary
    if operation.name == "status":
        help_text = (
            "Show the Supervisor, Gateway, Inference Services, durable operations, "
            "and memory-pressure overview."
        )

    if requires_resource:

        def command(
            resource: Annotated[
                str,
                typer.Argument(
                    help=(
                        "Named resource. Use the corresponding list or available "
                        "command to discover accepted values."
                    ),
                ),
            ],
            json_output: Annotated[
                bool, typer.Option("--json", help="Emit deterministic versioned JSON.")
            ] = False,
            json_lines: Annotated[
                bool,
                typer.Option("--json-lines", help="Emit events as versioned NDJSON."),
            ] = False,
            plain: Annotated[
                bool, typer.Option("--plain", help="Disable terminal decoration.")
            ] = False,
        ) -> None:
            _invoke(
                dispatcher,
                operation.name,
                {"resource": resource},
                json_output=json_output,
                json_lines=json_lines,
                plain=plain,
            )

    else:

        def command(
            json_output: Annotated[
                bool, typer.Option("--json", help="Emit deterministic versioned JSON.")
            ] = False,
            json_lines: Annotated[
                bool,
                typer.Option("--json-lines", help="Emit events as versioned NDJSON."),
            ] = False,
            plain: Annotated[
                bool, typer.Option("--plain", help="Disable terminal decoration.")
            ] = False,
        ) -> None:
            _invoke(
                dispatcher,
                operation.name,
                {},
                json_output=json_output,
                json_lines=json_lines,
                plain=plain,
            )

    command.__name__ = "command_" + operation.name.replace(".", "_")
    command.__doc__ = help_text
    app.command(command_name, help=help_text)(command)


def _invoke(
    dispatcher: Dispatcher,
    operation: str,
    parameters: Mapping[str, object],
    *,
    json_output: bool,
    json_lines: bool,
    plain: bool,
) -> None:
    try:
        result = dispatcher.execute(OperationRequest(operation, parameters))
    except ApplicationError as error:
        _render_error(error, json_output=json_output or json_lines)
        raise typer.Exit(1) from error
    if json_lines:
        for event in result.events:
            typer.echo(json.dumps(_plain(event), sort_keys=True, separators=(",", ":")))
        typer.echo(
            json.dumps(
                {
                    "schema_version": result.schema_version,
                    "operation": result.operation,
                    "result": _plain(result.value),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    elif json_output:
        typer.echo(
            json.dumps(_plain(result.value), sort_keys=True, separators=(",", ":"))
        )
    elif plain:
        typer.echo(json.dumps(_plain(result.value), sort_keys=True, indent=2))
    else:
        Console().print(Pretty(_plain(result.value), expand_all=True))


def _render_error(error: ApplicationError, *, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "error": {
                        "code": error.code,
                        "message": error.message,
                        "next_actions": list(error.next_actions),
                    },
                    "schema_version": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    typer.echo(f"Error: {error.message}", err=True)
    for action in error.next_actions:
        typer.echo(f"Next: {action}", err=True)


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value


def _group_help(group: str) -> str:
    return {
        "supervisor": "Explicitly inspect and control the per-user Supervisor.",
        "gateway": "Inspect and configure the stable loopback Gateway.",
        "runtime": "Discover and manage exact Runtime Installations.",
        "model": "Search, inspect, install, verify, and trust exact Model Revisions.",
        "service": "Create and control named Inference Services.",
        "operation": "Inspect, follow, cancel, and resume durable operations.",
        "client": "Configure and verify supported Gateway Client Integrations.",
        "config": "Inspect, edit, validate, diff, and restore desired configuration.",
    }[group]
