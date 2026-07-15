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


@dataclass(frozen=True, slots=True)
class Operation:
    name: str
    summary: str
    kind: OperationKind
    supervisor: SupervisorRequirement
    confirmation: bool = False
    examples: tuple[str, ...] = ()
    output_modes: frozenset[str] = frozenset({"human", "plain", "json"})
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


def build_operation_catalogue() -> Mapping[str, Operation]:
    """Return the immutable parity contract used by CLI and TUI builders."""
    operations: dict[str, Operation] = {}
    for group, commands in _TREE.items():
        for command in commands:
            name = f"{group}.{command}" if group else command
            kind = OperationKind.QUERY if name in _QUERIES else OperationKind.MUTATION
            supervisor = SupervisorRequirement.NEVER_START
            if kind is OperationKind.MUTATION:
                supervisor = (
                    SupervisorRequirement.MAY_START
                    if name in {"setup", "service.start"}
                    else SupervisorRequirement.REQUIRED
                )
            summary = _summary(name)
            operations[name] = Operation(
                name=name,
                summary=summary,
                kind=kind,
                supervisor=supervisor,
                confirmation=kind is OperationKind.MUTATION and name not in _NO_CONFIRM,
                examples=(_example(name),),
                output_modes=frozenset({"human", "plain", "json", "json-lines"}),
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
