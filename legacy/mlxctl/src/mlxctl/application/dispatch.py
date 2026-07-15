"""Typed operation dispatch shared by local and Supervisor-backed interfaces."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Callable, Mapping, Protocol

from .catalogue import Operation, OperationKind


class SupervisorActivator(Protocol):
    def activate(self) -> None: ...


class ApplicationError(RuntimeError):
    """Stable application failure suitable for human and machine interfaces."""

    def __init__(
        self, code: str, message: str, *, next_actions: tuple[str, ...] = ()
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_actions = next_actions


@dataclass(frozen=True, slots=True)
class OperationRequest:
    name: str
    parameters: Mapping[str, object] = field(default_factory=dict)
    request_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True, slots=True)
class OperationResult:
    operation: str
    value: Mapping[str, object]
    events: tuple[Mapping[str, object], ...] = ()
    schema_version: int = 1
    supervisor_started: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", MappingProxyType(dict(self.value)))
        object.__setattr__(
            self,
            "events",
            tuple(MappingProxyType(dict(event)) for event in self.events),
        )


@dataclass(slots=True)
class _Execution:
    request: OperationRequest
    activated: bool = False


Handler = Callable[[OperationRequest], OperationResult]


class OperationDispatcher:
    """Enforce catalogue availability and Supervisor activation policy."""

    def __init__(
        self,
        catalogue: Mapping[str, Operation],
        activator: SupervisorActivator,
    ) -> None:
        self._catalogue = catalogue
        self._activator = activator
        self._handlers: dict[str, Handler] = {}
        self._execution: ContextVar[_Execution | None] = ContextVar(
            "mlxctl_operation_execution", default=None
        )

    def register(self, name: str, handler: Handler) -> None:
        if name not in self._catalogue:
            raise ApplicationError("unknown_operation", f"unknown operation: {name}")
        if name in self._handlers:
            raise ValueError(f"operation already registered: {name}")
        self._handlers[name] = handler

    def execute(self, request: OperationRequest) -> OperationResult:
        if request.name not in self._catalogue:
            raise ApplicationError(
                "unknown_operation", f"unknown operation: {request.name}"
            )
        handler = self._handlers.get(request.name)
        if handler is None:
            raise ApplicationError(
                "operation_unavailable",
                f"{request.name} is not available in this installation",
                next_actions=("run mlxctl doctor", "inspect mlxctl --help"),
            )
        execution = _Execution(request)
        token = self._execution.set(execution)
        try:
            result = handler(request)
        finally:
            self._execution.reset(token)
        if result.operation != request.name:
            raise ValueError("handler returned a result for another operation")
        return replace(result, supervisor_started=execution.activated)

    def require_supervisor(self, request: OperationRequest) -> None:
        operation = self._catalogue.get(request.name)
        if operation is None:
            raise ApplicationError(
                "unknown_operation", f"unknown operation: {request.name}"
            )
        if operation.kind is OperationKind.QUERY:
            raise ApplicationError(
                "activation_forbidden",
                f"read-only operation {request.name} cannot start the Supervisor",
            )
        execution = self._execution.get()
        if execution is None or execution.request is not request:
            raise RuntimeError("Supervisor activation must occur inside dispatch")
        if not execution.activated:
            self._activator.activate()
            execution.activated = True
