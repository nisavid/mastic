"""Operation-port adapters shared by local interfaces and the Supervisor."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Protocol

from mlxctl.application.config_schema import (
    ClientSamplingSettings,
    ClientSettings,
    validate_hindsight_profile_name,
)
from mlxctl.application.dispatch import ApplicationError
from mlxctl.infrastructure.control_client import ControlClientError, UnixControlClient
from mlxctl.infrastructure.client_integrations import (
    ClientConfiguration,
)
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
        result = dict(response.result)
        return {
            **result,
            "operation_id": result.get("operation_id", response.operation_id),
            "control_operation_id": response.operation_id,
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
        elif operation == "service.drain":
            value = self._supervisor.drain_service(resource)
        elif operation == "service.stop":
            value = self._supervisor.stop_service(resource)
        elif operation == "service.restart":
            value = self._supervisor.restart_service(resource)
        elif operation == "service.remove":
            value = self._supervisor.remove_service(resource)
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
        adapter: Callable[
            [str, str, Mapping[str, object], ClientSettings | None], ClientAdapter
        ],
        configuration: Callable[
            [str, Mapping[str, object], ClientSettings | None], ClientConfiguration
        ],
        *,
        request: Callable[[str, str, Mapping[str, object]], object],
        settings: Callable[[str], ClientSettings | None] | None = None,
        record: Callable[[str, ClientSettings | None], object] | None = None,
    ) -> None:
        self._adapter = adapter
        self._configuration = configuration
        self._request = request
        self._settings = settings or (lambda _name: None)
        self._record = record or (lambda _name, _configuration: None)

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        name = str(parameters.get("client", parameters.get("resource", "")))
        stored = self._settings(name)
        if operation != "client.configure" and stored is None:
            raise ApplicationError(
                "resource_not_found",
                f"Client Integration {name!r} is not configured",
                next_actions=(f"mlxctl client configure {name}",),
            )
        try:
            if operation == "client.configure" and name == "hindsight":
                profile = validate_hindsight_profile_name(parameters.get("profile"))
                if stored is not None and stored.profile != profile:
                    raise ApplicationError(
                        "integration_conflict",
                        "Remove the owned Hindsight integration before selecting a different profile",
                        next_actions=("mlxctl client remove hindsight",),
                    )
            adapter = self._adapter(operation, name, parameters, stored)
        except ApplicationError:
            raise
        except (KeyError, ValueError) as error:
            raise ApplicationError(
                "invalid_parameter",
                str(error),
                next_actions=(f"mlxctl {operation.replace('.', ' ')} --help",),
            ) from error
        if operation == "client.remove":
            result = adapter.remove()
            plain_result = _plain(result)
            if isinstance(plain_result, Mapping) and plain_result.get("skipped_paths"):
                return {**plain_result, "desired_state_retained": True}
            self._record(name, None)
            return plain_result
        configuration = self._configuration(name, parameters, stored)
        if operation == "client.configure":
            preview = adapter.preview(configuration)
            result = adapter.apply(configuration)
            self._record(
                name,
                _client_settings(name, parameters, configuration, stored),
            )
            return {"preview": _plain(preview), "result": _plain(result)}
        if operation == "client.test":
            profile = str(
                parameters.get("profile")
                or ("reflect" if name == "hindsight" else "coding")
            )
            try:
                response = adapter.test(
                    configuration,
                    self._request,
                    profile=profile,
                )
            except KeyError as error:
                raise ApplicationError(
                    "invalid_parameter",
                    str(error),
                    next_actions=(f"mlxctl client inspect {name}",),
                ) from error
            return {"profile": profile, "response": _plain(response)}
        raise ApplicationError(
            "operation_unavailable", f"{operation} is not a client operation"
        )


def _client_settings(
    name: str,
    parameters: Mapping[str, object],
    configuration: ClientConfiguration,
    stored: ClientSettings | None,
) -> ClientSettings:
    service = str(parameters.get("service") or (stored.service if stored else ""))
    if not service:
        raise ApplicationError(
            "invalid_parameter",
            "client configure requires an Inference Service",
        )
    if name == "hindsight":
        profile = validate_hindsight_profile_name(parameters.get("profile"))
        provider = configuration.hindsight_provider
        max_concurrent: int | None = configuration.max_concurrent
    else:
        profile = None
        provider = configuration.codex_provider_id
        max_concurrent = None
    sampling = {
        profile_name: ClientSamplingSettings(
            temperature=profile_settings.temperature,
            top_p=profile_settings.top_p,
            max_tokens=profile_settings.max_tokens,
        )
        for profile_name, profile_settings in configuration.sampling_profiles.items()
    }
    return ClientSettings(
        name=name,
        kind=name,
        service=service,
        profile=profile,
        context_window=configuration.context_window,
        provider=provider,
        max_concurrent=max_concurrent,
        sampling=sampling,
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
