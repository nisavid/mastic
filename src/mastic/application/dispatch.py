"""Typed operation dispatch shared by local and Supervisor-backed interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Mapping, Protocol

from .catalogue import Operation, OperationKind, SupervisorRequirement


class SupervisorActivator(Protocol):
    def activate(self) -> None: ...


class ApplicationError(RuntimeError):
    """Stable application failure suitable for human and machine interfaces."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        next_actions: tuple[str, ...] = (),
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_actions = tuple(next_actions)
        self.details = _freeze_error_details(details or {})


def _freeze_error_details(
    details: Mapping[str, object],
) -> Mapping[str, object]:
    return MappingProxyType(
        {str(key): _freeze_error_value(value) for key, value in details.items()}
    )


def _freeze_error_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_error_details(value)
    if isinstance(value, tuple | list):
        return tuple(_freeze_error_value(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class OperationRequest:
    name: str
    parameters: Mapping[str, object] = field(default_factory=dict)
    request_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True, slots=True)
class PreparedOperation:
    """A validated operation preview ready to cross a mutation boundary."""

    requires_supervisor: bool
    execute: Callable[[], Mapping[str, object]]
    events: tuple[Mapping[str, object], ...] = ()


class OperationBackend(Protocol):
    def prepare(self, request: OperationRequest) -> PreparedOperation: ...


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


class OperationDispatch(Protocol):
    def preview(self, request: OperationRequest) -> OperationResult: ...

    def execute(self, request: OperationRequest) -> OperationResult: ...


class OperationDispatcher:
    """Prepare and dispatch catalogue operations through shared policy."""

    def __init__(
        self,
        catalogue: Mapping[str, Operation],
        activator: SupervisorActivator,
        backend: OperationBackend,
    ) -> None:
        self._catalogue = catalogue
        self._activator = activator
        self._backend = backend

    def preview(self, request: OperationRequest) -> OperationResult:
        """Resolve an operation preview without activating or executing it."""

        if request.name not in self._catalogue:
            raise ApplicationError(
                "unknown_operation", f"unknown operation: {request.name}"
            )
        operation = self._catalogue[request.name]
        prepared = self._prepare(request, "preview")
        preview_events = tuple(dict(event) for event in prepared.events)
        if preview_events:
            terminal = preview_events[-1]
            if (
                terminal.get("state") == "no_validated_fit"
                and terminal.get("mutation_count") == 0
            ):
                value = {key: item for key, item in terminal.items() if key != "phase"}
                return OperationResult(request.name, value, events=prepared.events)
        identity = next(
            (
                event["preview_fingerprint"]
                for event in reversed(preview_events)
                if isinstance(event.get("preview_fingerprint"), str)
            ),
            None,
        )
        value = {
            "schema_version": 1,
            "operation": request.name,
            "state": "review_required",
            "confirmation_required": operation.confirmation,
            "requires_supervisor": prepared.requires_supervisor,
            "preview": preview_events,
        }
        if identity is not None:
            value["preview_fingerprint"] = identity
        return OperationResult(request.name, value, events=prepared.events)

    def execute(self, request: OperationRequest) -> OperationResult:
        if request.name not in self._catalogue:
            raise ApplicationError(
                "unknown_operation", f"unknown operation: {request.name}"
            )
        operation = self._catalogue[request.name]
        prepared = self._prepare(request, "prepare")
        if operation.confirmation and request.parameters.get("confirmed") is not True:
            raise ApplicationError(
                "confirmation_required",
                f"{request.name} requires review and explicit confirmation",
                next_actions=(
                    f"mastic {request.name.replace('.', ' ')} --help",
                    "rerun with --yes after reviewing the resolved preview",
                ),
            )
        activated = False
        if prepared.requires_supervisor:
            if operation.kind is OperationKind.QUERY:
                raise ApplicationError(
                    "activation_forbidden",
                    f"read-only operation {request.name} cannot start the Supervisor",
                )
            if operation.supervisor is not SupervisorRequirement.NEVER_START:
                try:
                    self._activator.activate()
                except ApplicationError:
                    raise
                except Exception as error:
                    raise self._operation_failure(request, "activate", error) from error
                activated = True
        try:
            value = prepared.execute()
        except ApplicationError:
            raise
        except Exception as error:
            raise self._operation_failure(request, "execute", error) from error
        return OperationResult(
            request.name,
            value,
            events=prepared.events,
            supervisor_started=activated,
        )

    def _prepare(self, request: OperationRequest, phase: str) -> PreparedOperation:
        try:
            return self._backend.prepare(request)
        except ApplicationError:
            raise
        except Exception as error:
            raise self._operation_failure(request, phase, error) from error

    @staticmethod
    def _operation_failure(
        request: OperationRequest, phase: str, error: Exception
    ) -> ApplicationError:
        return ApplicationError(
            "operation_failed",
            f"{request.name} failed during {phase}: {error}",
            next_actions=(
                "mastic doctor",
                f"mastic {request.name.replace('.', ' ')} --help",
            ),
        )
