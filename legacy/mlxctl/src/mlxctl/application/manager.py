"""Register the full operation catalogue against one prepared-operation backend."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from .catalogue import Operation, SupervisorRequirement
from .dispatch import (
    ApplicationError,
    OperationDispatcher,
    OperationRequest,
    OperationResult,
)


@dataclass(frozen=True, slots=True)
class PreparedOperation:
    """A validated operation plan ready to cross a mutation boundary."""

    requires_supervisor: bool
    execute: Callable[[], Mapping[str, object]]
    events: tuple[Mapping[str, object], ...] = ()


class OperationBackend(Protocol):
    def prepare(self, request: OperationRequest) -> PreparedOperation: ...


class ApplicationManager:
    """Bind every catalogue entry to the backend through identical policy."""

    def __init__(
        self,
        catalogue: Mapping[str, Operation],
        backend: OperationBackend,
    ) -> None:
        self._catalogue = catalogue
        self._backend = backend

    def register(self, dispatcher: OperationDispatcher) -> None:
        for name in self._catalogue:
            dispatcher.register(name, self._handler(dispatcher))

    def _handler(self, dispatcher: OperationDispatcher):
        def handle(request: OperationRequest) -> OperationResult:
            operation = self._catalogue[request.name]
            prepared = self._backend.prepare(request)
            if prepared.requires_supervisor:
                if operation.supervisor is SupervisorRequirement.NEVER_START:
                    raise ApplicationError(
                        "activation_forbidden",
                        f"{request.name} must not start the Supervisor",
                        next_actions=(
                            "start the Supervisor explicitly if mutation is intended",
                        ),
                    )
                dispatcher.require_supervisor(request)
            try:
                value = prepared.execute()
            except ApplicationError:
                raise
            except Exception as error:
                raise ApplicationError(
                    "operation_failed",
                    f"{request.name} failed: {error}",
                    next_actions=(
                        "mlxctl doctor",
                        f"mlxctl {request.name.replace('.', ' ')} --help",
                    ),
                ) from error
            return OperationResult(request.name, value, events=prepared.events)

        return handle
