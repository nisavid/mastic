"""The single CLI/TUI operation and capability catalogue."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class OperationKind(StrEnum):
    QUERY = "query"
    MUTATION = "mutation"


class SupervisorRequirement(StrEnum):
    NEVER_START = "never_start"
    MAY_START = "may_start"
    REQUIRED = "required"


class ParameterKind(StrEnum):
    ARGUMENT = "argument"
    OPTION = "option"


@dataclass(frozen=True, slots=True)
class Parameter:
    name: str
    kind: ParameterKind
    help: str
    required: bool = False
    value_type: str = "string"
    accepted: tuple[str, ...] = ()
    flag: str | None = None


@dataclass(frozen=True, slots=True)
class Operation:
    name: str
    summary: str
    kind: OperationKind
    supervisor: SupervisorRequirement
    confirmation: bool = False
    examples: tuple[str, ...] = ()
    output_modes: frozenset[str] = frozenset({"human", "plain", "json"})
    parameters: tuple[Parameter, ...] = ()
    cli: bool = True
    tui: bool = True


_TREE: Mapping[str, tuple[str, ...]] = {
    "": ("setup", "status", "check", "doctor", "logs", "metrics", "tui"),
    "supervisor": ("status", "start", "stop", "restart", "logs", "inspect"),
    "gateway": (
        "status",
        "inspect",
        "routes",
        "configure",
        "restart",
        "logs",
        "metrics",
    ),
    "runtime": (
        "list",
        "available",
        "inspect",
        "install",
        "adopt",
        "update",
        "rollback",
        "remove",
        "prune",
        "doctor",
    ),
    "model": (
        "search",
        "list",
        "inspect",
        "install",
        "verify",
        "repair",
        "update",
        "rollback",
        "uninstall",
        "trust",
    ),
    "model.cache": ("list", "inspect", "move", "evict", "prune"),
    "service": (
        "list",
        "create",
        "inspect",
        "edit",
        "start",
        "stop",
        "restart",
        "remove",
        "logs",
        "metrics",
        "check",
    ),
    "operation": ("list", "inspect", "follow", "cancel", "resume"),
    "client": ("list", "inspect", "configure", "test", "remove"),
    "config": (
        "path",
        "show",
        "validate",
        "diff",
        "history",
        "export",
        "import",
        "restore",
    ),
}

_QUERIES = frozenset(
    {
        "status",
        "check",
        "logs",
        "metrics",
        "tui",
        "doctor",
        "supervisor.status",
        "supervisor.logs",
        "supervisor.inspect",
        "gateway.status",
        "gateway.inspect",
        "gateway.routes",
        "gateway.logs",
        "gateway.metrics",
        "runtime.list",
        "runtime.available",
        "runtime.inspect",
        "runtime.doctor",
        "model.search",
        "model.list",
        "model.inspect",
        "model.verify",
        "model.cache.list",
        "model.cache.inspect",
        "service.list",
        "service.inspect",
        "service.logs",
        "service.metrics",
        "service.check",
        "operation.list",
        "operation.inspect",
        "operation.follow",
        "client.list",
        "client.inspect",
        "client.test",
        "config.path",
        "config.show",
        "config.validate",
        "config.diff",
        "config.history",
        "config.export",
    }
)

_NO_CONFIRM = frozenset(
    {
        "supervisor.start",
        "supervisor.restart",
        "gateway.restart",
        "service.start",
        "service.stop",
        "service.restart",
        "operation.cancel",
        "operation.resume",
        "client.test",
    }
)

_LOCAL_MUTATIONS = frozenset(
    {
        "gateway.configure",
        "model.trust",
        "service.create",
        "service.edit",
        "client.configure",
        "client.remove",
        "config.import",
        "config.restore",
    }
)


def build_operation_catalogue() -> Mapping[str, Operation]:
    """Return the immutable parity contract used by CLI and TUI builders."""
    operations: dict[str, Operation] = {}
    for group, commands in _TREE.items():
        for command in commands:
            name = f"{group}.{command}" if group else command
            kind = OperationKind.QUERY if name in _QUERIES else OperationKind.MUTATION
            supervisor = SupervisorRequirement.NEVER_START
            if kind is OperationKind.MUTATION:
                if name in {"setup", "service.start"}:
                    supervisor = SupervisorRequirement.MAY_START
                elif name not in _LOCAL_MUTATIONS:
                    supervisor = SupervisorRequirement.REQUIRED
            summary = _summary(name)
            operations[name] = Operation(
                name=name,
                summary=summary,
                kind=kind,
                supervisor=supervisor,
                confirmation=kind is OperationKind.MUTATION and name not in _NO_CONFIRM,
                examples=(_example(name),),
                output_modes=frozenset({"human", "plain", "json", "json-lines"}),
                parameters=_parameters(name),
            )
    return MappingProxyType(operations)


def _summary(name: str) -> str:
    if name == "model.install":
        return "Install and verify one exact revision of a model."
    if name == "runtime.available":
        return "List known Runtime Definitions, including those not installed."
    if name == "service.start":
        return "Start a named Inference Service and visibly activate the Supervisor if needed."
    if name == "supervisor.stop":
        return "Drain and stop all Service Runs, the Gateway, and the Supervisor."
    return name.replace(".", " ").replace("-", " ").capitalize() + "."


def _example(name: str) -> str:
    return "mlxctl " + name.replace(".", " ")


def _argument(
    name: str,
    help: str,
    *,
    required: bool = True,
    accepted: tuple[str, ...] = (),
) -> Parameter:
    return Parameter(
        name=name,
        kind=ParameterKind.ARGUMENT,
        help=help,
        required=required,
        accepted=accepted,
    )


def _option(
    name: str,
    help: str,
    *,
    value_type: str = "string",
    accepted: tuple[str, ...] = (),
    flag: str | None = None,
    required: bool = False,
) -> Parameter:
    return Parameter(
        name=name,
        kind=ParameterKind.OPTION,
        help=help,
        value_type=value_type,
        accepted=accepted,
        flag=flag or "--" + name.replace("_", "-"),
        required=required,
    )


def _parameters(name: str) -> tuple[Parameter, ...]:
    resource_help = {
        "runtime": "Runtime Definition or Installation ID; discover values with `mlxctl runtime available` or `runtime list`.",
        "model": "Model repository, installation, alias, or exact revision; discover values with `mlxctl model search` or `model list`.",
        "service": "Inference Service name; discover values with `mlxctl service list`.",
        "operation": "Durable operation ID; discover values with `mlxctl operation list`.",
        "client": "Client Integration name; discover values with `mlxctl client list`.",
    }
    if name == "setup":
        return (
            _option(
                "profile",
                "Setup profile to preselect; the complete plan remains editable.",
                accepted=("recommended", "expert"),
            ),
            _option(
                "offline",
                "Use only installed definitions, local evidence, and cached bytes.",
                value_type="boolean",
            ),
            _option(
                "yes",
                "Apply an exact noninteractive plan after all required values are supplied.",
                value_type="boolean",
            ),
        )
    if name == "doctor":
        return (
            _option(
                "fix",
                "Preview and apply the selected safe repairs.",
                value_type="boolean",
            ),
        )
    if name in {"logs", "metrics"}:
        return (
            _argument(
                "resource", "Optional resource identity to filter.", required=False
            ),
        )
    if name == "gateway.configure":
        return (
            _option("host", "Literal loopback bind address."),
            _option("port", "Stable Gateway port.", value_type="integer"),
        )
    if name == "runtime.install":
        return (
            _argument(
                "runtime",
                resource_help["runtime"],
                accepted=("mlx_lm", "mlx_vlm", "optiq"),
            ),
            _option(
                "version", "Exact custom upstream version; omit for the tested channel."
            ),
            _option("channel", "Installation channel.", accepted=("tested", "custom")),
        )
    if name == "runtime.adopt":
        return (
            _argument(
                "runtime",
                resource_help["runtime"],
                accepted=("mlx_lm", "mlx_vlm", "optiq"),
            ),
            _option(
                "path",
                "Existing runtime environment to probe and adopt.",
                required=True,
            ),
        )
    if name == "runtime.update":
        return (
            _argument("resource", resource_help["runtime"]),
            _option(
                "channel", "Target installation channel.", accepted=("tested", "custom")
            ),
            _option("version", "Exact custom target version."),
        )
    if name == "runtime.rollback":
        return (
            _argument("resource", resource_help["runtime"]),
            _option(
                "target", "Previously retained Runtime Installation ID.", required=True
            ),
        )
    if name.startswith("runtime.") and name not in {
        "runtime.list",
        "runtime.available",
    }:
        return (_argument("resource", resource_help["runtime"]),)
    if name == "model.search":
        return (
            _argument(
                "query", "Repository or model text to search for.", required=False
            ),
            _option(
                "source", "Candidate source.", accepted=("curated", "broad", "local")
            ),
            _option("limit", "Maximum candidates to return.", value_type="integer"),
        )
    if name == "model.install":
        return (
            _argument(
                "repository", "Hugging Face repository ID or declared local model path."
            ),
            _option(
                "revision",
                "Exact commit SHA or reference to resolve before confirmation.",
            ),
            _option("alias", "Stable Model Alias to create."),
            _option(
                "offline", "Require already-cached exact content.", value_type="boolean"
            ),
        )
    if name in {"model.update", "model.rollback"}:
        return (
            _argument("resource", resource_help["model"]),
            _option(
                "revision",
                "Exact target commit SHA or reference to resolve.",
                required=True,
            ),
            _option(
                "offline", "Require already-cached exact content.", value_type="boolean"
            ),
        )
    if name == "model.trust":
        return (
            _argument("resource", resource_help["model"]),
            _option(
                "runtime",
                "Exact Runtime Installation receiving the grant.",
                required=True,
            ),
            _option(
                "accepted_risks",
                "Comma-separated revision-scoped risks explicitly accepted.",
                required=True,
            ),
        )
    if name == "model.cache.move":
        return (
            _argument("resource", "Cached Revision identity."),
            _option("destination", "Existing target cache directory.", required=True),
        )
    if name.startswith("model.cache.") and name != "model.cache.list":
        return (
            _argument(
                "resource",
                "Cached Revision identity; discover values with `mlxctl model cache list`.",
            ),
        )
    if name.startswith("model.") and name not in {"model.list", "model.search"}:
        return (_argument("resource", resource_help["model"]),)
    if name == "service.create":
        return (
            _argument("service", "New Inference Service name."),
            _option(
                "model_alias", "Model Alias selected by this service.", required=True
            ),
            _option("runtime", "Exact Runtime Installation ID.", required=True),
            _option("route", "Stable Gateway route; defaults to the service name."),
            _option(
                "activation",
                "Activation policy.",
                accepted=("manual", "supervisor"),
            ),
            _option(
                "pinned",
                "Never auto-stop this service under memory pressure.",
                value_type="boolean",
            ),
        )
    if name == "service.edit":
        return (
            _argument("resource", resource_help["service"]),
            _option("model_alias", "Replacement Model Alias."),
            _option("runtime", "Replacement Runtime Installation ID."),
            _option("route", "Replacement stable Gateway route."),
            _option(
                "activation", "Activation policy.", accepted=("manual", "supervisor")
            ),
            _option(
                "pinned",
                "Pin against automatic pressure eviction.",
                value_type="boolean",
            ),
        )
    if name.startswith("service.") and name != "service.list":
        return (_argument("resource", resource_help["service"]),)
    if name.startswith("operation.") and name != "operation.list":
        return (_argument("resource", resource_help["operation"]),)
    if name == "client.configure":
        return (
            _argument(
                "client", "Client Integration name.", accepted=("codex", "hindsight")
            ),
            _option(
                "service",
                "Inference Service route the client should use.",
                required=True,
            ),
            _option("profile", "Hindsight profile name when configuring Hindsight."),
            _option("context_window", "Client context window.", value_type="integer"),
        )
    if name == "client.test":
        return (
            _argument("resource", resource_help["client"]),
            _option("profile", "Sampling profile to exercise."),
        )
    if name.startswith("client.") and name != "client.list":
        return (_argument("resource", resource_help["client"]),)
    if name == "config.import":
        return (
            _argument("source", "TOML file to validate and preview before import."),
        )
    if name == "config.restore":
        return (
            _argument(
                "revision", "Configuration revision from `mlxctl config history`."
            ),
        )
    if name == "config.diff":
        return (_option("source", "TOML file to compare with current desired state."),)
    return ()
