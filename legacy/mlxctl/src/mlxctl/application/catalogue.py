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
    "": ("setup", "remove", "status", "check", "doctor", "logs", "metrics", "tui"),
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
    "operation": ("list", "inspect"),
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
        "client.test",
    }
)

_LOCAL_MUTATIONS = frozenset(
    {
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
    return {
        "setup": "Plan and create a complete local inference service on this Mac.",
        "remove": "Preview and remove mlxctl-owned services, state, and integrations.",
        "status": "Show the whole local inference system without starting it.",
        "check": "Run concise health checks across the Supervisor, Gateway, and services.",
        "doctor": "Diagnose configuration, lifecycle, routing, and service failures.",
        "logs": "Read bounded mlxctl logs, optionally for one resource.",
        "metrics": "Read locally recorded operational metrics, optionally for one resource.",
        "tui": "Open the interactive local inference operations console.",
        "supervisor.status": "Show whether the per-user Supervisor is running.",
        "supervisor.start": "Start the Gateway and services configured for supervisor activation.",
        "supervisor.stop": "Drain and stop all Service Runs, the Gateway, and the Supervisor.",
        "supervisor.restart": "Drain and reconstruct the Supervisor and its managed services.",
        "supervisor.logs": "Read the bounded private Supervisor log.",
        "supervisor.inspect": "Inspect Supervisor state and durable operations together.",
        "gateway.status": "Show the stable loopback endpoint and configured route count.",
        "gateway.inspect": "Inspect the Gateway endpoint, routes, and observed runtime state.",
        "gateway.routes": "List stable Gateway model routes and their Inference Services.",
        "gateway.configure": "Change the stopped Gateway's loopback host or stable port.",
        "gateway.restart": "Drain and restart the Gateway through the Supervisor.",
        "gateway.logs": "Read the bounded private Gateway log.",
        "gateway.metrics": "Read locally recorded Gateway metrics.",
        "runtime.list": "List installed and adopted Runtime Installations.",
        "runtime.available": "List built-in Runtime Definitions, including uninstalled ones.",
        "runtime.inspect": "Inspect a Runtime Definition or exact Installation.",
        "runtime.install": "Install and probe a tested or exact custom runtime version.",
        "runtime.adopt": "Probe and adopt an existing runtime environment.",
        "runtime.update": "Install and switch to a newer tested or exact custom runtime.",
        "runtime.rollback": "Switch services to a retained Runtime Installation.",
        "runtime.remove": "Remove one unreferenced Runtime Installation.",
        "runtime.prune": "Remove safe, unreferenced Runtime Installations.",
        "runtime.doctor": "Check installed runtime roots and executable launchers.",
        "model.search": "Search curated, Hugging Face, or local cached models.",
        "model.list": "List configured Model Installations and their stable aliases.",
        "model.inspect": "Inspect model identity, capabilities, trust signals, and Mac fit.",
        "model.install": "Resolve, cache, verify, and name one exact revision of a model.",
        "model.verify": "Verify one installed Model Revision against its cached bytes.",
        "model.repair": "Repair missing or incomplete bytes for an exact Model Revision.",
        "model.update": "Install an exact newer revision beside the current installation.",
        "model.rollback": "Move a Model Alias to a retained Model Installation.",
        "model.uninstall": "Remove one unreferenced Model Installation and its aliases.",
        "model.trust": "Grant named risks to one exact Model Revision and Runtime Installation.",
        "model.cache.list": "List locally cached model revisions and observed disk usage.",
        "model.cache.inspect": "Inspect one physical Cached Revision and its provenance.",
        "model.cache.move": "Move one Cached Revision through the cache owner.",
        "model.cache.evict": "Evict one safe, unreferenced Cached Revision.",
        "model.cache.prune": "Evict safe, unreferenced Cached Revisions.",
        "service.list": "List named Inference Services with desired and live state.",
        "service.create": "Create a named service from a Model Alias and Runtime Installation.",
        "service.inspect": "Inspect one service's desired state, run, route, and next action.",
        "service.edit": "Change a stopped service's model, runtime, route, or pressure policy.",
        "service.start": "Start one service and visibly activate the Supervisor if needed.",
        "service.stop": "Drain and stop one service without stopping the Gateway.",
        "service.restart": "Drain and restart one service with current desired state.",
        "service.remove": "Drain, stop, and delete one Inference Service definition.",
        "service.logs": "Read one service's bounded private runtime log.",
        "service.metrics": "Read locally recorded metrics for one service.",
        "service.check": "Check one service's desired state, run, and Gateway route.",
        "operation.list": "List durable physical operations and their current status.",
        "operation.inspect": "Inspect one durable operation and its recorded events.",
        "client.list": "List mlxctl-owned Client Integrations.",
        "client.inspect": "Inspect one owned client endpoint and sampling configuration.",
        "client.configure": "Configure Codex or Hindsight for one Gateway route.",
        "client.test": "Send a bounded verification request through one configured client.",
        "client.remove": "Remove only the settings owned by one Client Integration.",
        "config.path": "Show the active desired-state file path.",
        "config.show": "Show validated desired state without starting the Supervisor.",
        "config.validate": "Validate current desired state and report its revision.",
        "config.diff": "Compare candidate TOML with current desired state.",
        "config.history": "List retained desired-state revisions.",
        "config.export": "Export canonical validated desired state.",
        "config.import": "Preview and import validated desired state.",
        "config.restore": "Restore one retained desired-state revision.",
    }[name]


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
            _option("runtime_name", "Runtime Definition name."),
            _option("runtime_version", "Exact runtime package version."),
            _option(
                "runtime_lock_digest",
                "Exact sha256 lock digest for the Runtime Installation.",
            ),
            _option("model_repository", "Hugging Face model repository ID."),
            _option("model_revision", "Exact model commit SHA."),
            _option(
                "trust_grants",
                "JSON array of revision-scoped risks explicitly accepted.",
                value_type="json",
            ),
            _option("service_name", "Internal Inference Service name."),
            _option(
                "model_alias",
                "Stable Model Alias; defaults to the service name.",
            ),
            _option(
                "service_route",
                "Public Gateway model route; defaults to the service name.",
            ),
            _option(
                "activation",
                "Service activation policy.",
                accepted=("manual", "supervisor"),
            ),
            _option(
                "pinned",
                "Never auto-stop this service under memory pressure.",
                value_type="boolean",
            ),
            _option(
                "service_options",
                "JSON object of runtime-specific launch options, such as OptiQ KV config and MTP.",
                value_type="json",
            ),
            _option(
                "gateway_endpoint",
                "Stable literal loopback Gateway URL, including /v1.",
            ),
            _option(
                "clients",
                "JSON array of Client Integrations to configure.",
                value_type="json",
            ),
            _option(
                "client_options",
                "JSON object of per-client settings; Hindsight requires a profile.",
                value_type="json",
            ),
            _option(
                "sampling_profiles",
                "JSON object of per-client or per-operation sampling profiles.",
                value_type="json",
            ),
            _option(
                "context_window",
                "Client and inference context window.",
                value_type="integer",
            ),
            _option(
                "yes",
                "Apply an exact noninteractive plan after all required values are supplied.",
                value_type="boolean",
            ),
        )
    if name == "remove":
        return ()
    if name == "model.inspect":
        return (
            _argument(
                "repository",
                "Hugging Face repository ID, configured Model Installation, or Model Alias.",
            ),
            _option("revision", "Revision to resolve exactly before inspection."),
            _option(
                "context_tokens",
                "Context-token scenario for the machine-fit estimate.",
                value_type="integer",
            ),
            _option(
                "concurrency",
                "Concurrent-request scenario for the machine-fit estimate.",
                value_type="integer",
            ),
        )
    if name == "doctor":
        return ()
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
        "runtime.doctor",
        "runtime.prune",
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
    if name == "model.update":
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
    if name == "model.rollback":
        return (
            _argument("resource", resource_help["model"]),
            _option(
                "target",
                "Previously retained Model Installation ID from `mlxctl model list`.",
                required=True,
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
                "JSON array of revision-scoped risks explicitly accepted.",
                value_type="json",
                required=True,
            ),
        )
    if name == "model.cache.move":
        return (
            _argument("resource", "Cached Revision identity."),
            _option("destination", "Existing target cache directory.", required=True),
        )
    if name.startswith("model.cache.") and name not in {
        "model.cache.list",
        "model.cache.prune",
    }:
        return (
            _argument(
                "resource",
                "Cached Revision identity; discover values with `mlxctl model cache list`.",
            ),
        )
    if (
        name.startswith("model.")
        and not name.startswith("model.cache.")
        and name
        not in {
            "model.list",
            "model.search",
            "model.inspect",
        }
    ):
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
            _option(
                "options",
                "JSON object of runtime-specific launch options.",
                value_type="json",
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
            _option(
                "options",
                "Replacement JSON object of runtime-specific launch options.",
                value_type="json",
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
            _option(
                "sampling_profiles",
                "JSON object of named sampling profiles.",
                value_type="json",
            ),
            _option("provider", "Client provider identifier."),
            _option(
                "max_concurrent",
                "Maximum concurrent client requests.",
                value_type="integer",
            ),
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
