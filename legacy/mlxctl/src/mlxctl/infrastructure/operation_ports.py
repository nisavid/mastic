"""Operation-port adapters shared by local interfaces and the Supervisor."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Protocol

from mlxctl.application.dispatch import ApplicationError
from mlxctl.infrastructure.control_client import ControlClientError, UnixControlClient
from mlxctl.infrastructure.client_integrations import ClientConfiguration
from mlxctl.infrastructure.supervisor_v1 import Supervisor


class ControlClient(Protocol):
    def execute(
        self, operation: str, parameters: Mapping[str, object] | None = None
    ): ...

    def cancel(self, operation_id: str): ...


class ClientAdapter(Protocol):
    def preview(self, configuration: ClientConfiguration): ...

    def apply(self, configuration: ClientConfiguration): ...

    def remove(self): ...

    def test(self, configuration: ClientConfiguration, request, *, profile: str): ...


class RemoteOperationPort:
    """Forward Supervisor-owned operations over the bounded control socket."""

    def __init__(self, client: ControlClient | str | Path) -> None:
        self._client = (
            UnixControlClient(client) if isinstance(client, str | Path) else client
        )

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        try:
            if operation == "operation.cancel":
                operation_id = str(
                    parameters.get("resource", parameters.get("operation_id", ""))
                )
                response = self._client.cancel(operation_id)
            else:
                response = self._client.execute(operation, parameters)
        except ControlClientError as error:
            raise ApplicationError(
                error.code,
                error.message,
                next_actions=("mlxctl supervisor status", "mlxctl doctor"),
            ) from error
        return {
            **dict(response.result),
            "operation_id": response.operation_id,
            "progress": [dict(item) for item in response.progress],
        }


class SupervisorOperationPort:
    """Execute lifecycle operations inside the foreground Supervisor."""

    def __init__(self, supervisor: Supervisor) -> None:
        self._supervisor = supervisor

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        resource = str(parameters.get("resource", parameters.get("service", "")))
        if operation == "supervisor.start":
            value = self._supervisor.start()
        elif operation == "supervisor.stop":
            value = self._supervisor.stop()
        elif operation == "supervisor.restart":
            value = self._supervisor.restart()
        elif operation == "gateway.restart":
            value = self._supervisor.restart()
        elif operation == "service.start":
            value = self._supervisor.start_service(resource)
        elif operation == "service.stop":
            value = self._supervisor.stop_service(resource)
        elif operation == "service.restart":
            value = self._supervisor.restart_service(resource)
        elif operation == "pressure.reconcile":
            value = self._supervisor.reconcile_pressure()
        else:
            raise ApplicationError(
                "operation_unavailable",
                f"{operation} is not a Supervisor lifecycle operation",
            )
        return _plain(value)


class ClientOperationPort:
    """Preview, apply, test, and precisely remove owned client integrations."""

    def __init__(
        self,
        adapters: Mapping[str, ClientAdapter],
        configuration: Callable[[str, Mapping[str, object]], ClientConfiguration],
        *,
        request: Callable[[str, str, Mapping[str, object]], object],
        record: Callable[[str, ClientConfiguration | None], object] | None = None,
    ) -> None:
        self._adapters = dict(adapters)
        self._configuration = configuration
        self._request = request
        self._record = record or (lambda _name, _configuration: None)

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        name = str(parameters.get("client", parameters.get("resource", "")))
        try:
            adapter = self._adapters[name]
        except KeyError as error:
            raise ApplicationError(
                "resource_not_found",
                f"Client Integration {name!r} is unavailable",
                next_actions=("mlxctl client list",),
            ) from error
        if operation == "client.remove":
            result = adapter.remove()
            self._record(name, None)
            return _plain(result)
        configuration = self._configuration(name, parameters)
        if operation == "client.configure":
            preview = adapter.preview(configuration)
            result = adapter.apply(configuration)
            self._record(name, configuration)
            return {"preview": _plain(preview), "result": _plain(result)}
        if operation == "client.test":
            profile = str(
                parameters.get("profile")
                or ("reflect" if name == "hindsight" else "coding")
            )
            return {
                "profile": profile,
                "response": _plain(
                    adapter.test(
                        configuration,
                        self._request,
                        profile=profile,
                    )
                ),
            }
        raise ApplicationError(
            "operation_unavailable", f"{operation} is not a client operation"
        )


def _plain(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _plain(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_plain(item) for item in value]
    if hasattr(value, "value"):
        return value.value  # type: ignore[union-attr]
    return value
