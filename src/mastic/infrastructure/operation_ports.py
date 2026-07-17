"""Operation-port adapters shared by local interfaces and the Supervisor."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Protocol

from mastic.application.config_schema import (
    ApplicationTargetSamplingSettings,
    ApplicationTargetSettings,
    validate_hindsight_profile_name,
)
from mastic.application.dispatch import ApplicationError
from mastic.infrastructure.control_client import ControlClientError, UnixControlClient
from mastic.infrastructure.application_target_integrations import (
    ApplicationTargetConfiguration,
)
from mastic.infrastructure.supervisor_v1 import Supervisor


_APPLICATION_TARGET_NAMES = frozenset({"codex", "hindsight"})


class ControlClient(Protocol):
    def execute(
        self, operation: str, parameters: Mapping[str, object] | None = None
    ): ...

    def cancel(self, operation_id: str): ...


class ApplicationTargetAdapter(Protocol):
    def preview(self, configuration: ApplicationTargetConfiguration): ...

    def apply(
        self, configuration: ApplicationTargetConfiguration, *, takeover: bool = False
    ): ...

    def remove(self): ...

    def rollback_point(self) -> Callable[[], None]: ...

    def test(
        self, configuration: ApplicationTargetConfiguration, request, *, profile: str
    ): ...


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
                next_actions=("mastic supervisor status", "mastic doctor"),
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
        elif operation == "supervisor.maintain":
            value = self._supervisor.maintain()
        else:
            raise ApplicationError(
                "operation_unavailable",
                f"{operation} is not a Supervisor lifecycle operation",
            )
        return _plain(value)


class ApplicationTargetOperationPort:
    """Operate owned Application Configuration Targets through one contract."""

    def __init__(
        self,
        adapter: Callable[
            [str, str, Mapping[str, object], ApplicationTargetSettings | None],
            ApplicationTargetAdapter,
        ],
        configuration: Callable[
            [str, Mapping[str, object], ApplicationTargetSettings | None],
            ApplicationTargetConfiguration,
        ],
        *,
        request: Callable[[str, str, str, Mapping[str, object]], object],
        settings: Callable[[str], ApplicationTargetSettings | None] | None = None,
        record: Callable[[str, ApplicationTargetSettings | None], object] | None = None,
        transition: Callable[[str], AbstractContextManager[None]] | None = None,
    ) -> None:
        self._adapter = adapter
        self._configuration = configuration
        self._request = request
        self._settings = settings or (lambda _name: None)
        self._record = record or (lambda _name, _configuration: None)
        self._transition = transition or (lambda _name: nullcontext())

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        name = str(parameters.get("application_target", ""))
        if name not in _APPLICATION_TARGET_NAMES:
            raise ApplicationError(
                "invalid_parameter",
                f"unsupported Application Configuration Target: {name!r}",
                next_actions=(f"mastic {operation.replace('.', ' ')} --help",),
            )
        if operation in {
            "application-target.configure",
            "application-target.remove",
        }:
            with self._transition(name):
                return self._execute(operation, parameters, name)
        return self._execute(operation, parameters, name)

    def _execute(
        self,
        operation: str,
        parameters: Mapping[str, object],
        name: str,
    ) -> Mapping[str, object]:
        stored = self._settings(name)
        if (
            operation
            not in {
                "application-target.configure",
                "application-target.inspect",
                "application-target.remove",
            }
            and stored is None
        ):
            raise ApplicationError(
                "resource_not_found",
                f"Application Configuration Target {name!r} is not configured",
                next_actions=(f"mastic application-target configure {name}",),
            )
        try:
            if operation == "application-target.configure" and name == "hindsight":
                profile = validate_hindsight_profile_name(parameters.get("profile"))
                if stored is not None and stored.profile != profile:
                    raise ApplicationError(
                        "integration_conflict",
                        "Remove the owned Hindsight integration before selecting a different profile",
                        next_actions=("mastic application-target remove hindsight",),
                    )
            adapter = self._adapter(operation, name, parameters, stored)
        except ApplicationError:
            raise
        except (KeyError, ValueError) as error:
            raise ApplicationError(
                "invalid_parameter",
                str(error),
                next_actions=(f"mastic {operation.replace('.', ' ')} --help",),
            ) from error
        if operation == "application-target.remove":
            rollback = adapter.rollback_point()
            result = adapter.remove()
            plain_result = _plain(result)
            if isinstance(plain_result, Mapping) and plain_result.get("skipped_paths"):
                return {**plain_result, "desired_state_retained": True}
            if stored is None:
                return plain_result
            try:
                self._record(name, None)
            except Exception:
                if self._settle_record_error(name, stored, None, rollback):
                    return plain_result
                raise
            return plain_result
        if operation == "application-target.inspect":
            inspect = getattr(adapter, "inspect", None)
            if inspect is None:
                return {"state": "healthy", "next_actions": []}
            return _plain(inspect())
        configuration = self._configuration(name, parameters, stored)
        if operation == "application-target.configure":
            required_profiles = (
                {"coding"}
                if name == "codex"
                else {"verification", "retain", "reflect", "consolidation"}
            )
            if set(configuration.sampling_profiles) != required_profiles:
                raise ApplicationError(
                    "invalid_parameter",
                    f"{name} requires sampling profiles: {', '.join(sorted(required_profiles))}",
                    next_actions=(
                        f"mastic application-target configure {name} --help",
                    ),
                )
            desired_settings = _application_target_settings(
                name, parameters, configuration, stored
            )
            preview = adapter.preview(configuration)
            rollback = adapter.rollback_point()
            result = adapter.apply(
                configuration, takeover=bool(parameters.get("takeover", False))
            )
            try:
                self._record(name, desired_settings)
            except Exception:
                if self._settle_record_error(name, stored, desired_settings, rollback):
                    return {"preview": _plain(preview), "result": _plain(result)}
                raise
            return {"preview": _plain(preview), "result": _plain(result)}
        if operation == "application-target.test":
            profile = str(
                parameters.get("profile")
                or ("reflect" if name == "hindsight" else "coding")
            )
            try:
                response = adapter.test(
                    configuration,
                    lambda endpoint, model, payload: self._request(
                        name, endpoint, model, payload
                    ),
                    profile=profile,
                )
            except KeyError as error:
                raise ApplicationError(
                    "invalid_parameter",
                    str(error),
                    next_actions=(f"mastic application-target inspect {name}",),
                ) from error
            return {"profile": profile, "response": _plain(response)}
        raise ApplicationError(
            "operation_unavailable",
            f"{operation} is not an Application Configuration Target operation",
        )

    def _settle_record_error(
        self,
        name: str,
        previous: ApplicationTargetSettings | None,
        intended: ApplicationTargetSettings | None,
        rollback: Callable[[], None],
    ) -> bool:
        try:
            observed = self._settings(name)
        except Exception as reload_error:
            raise _application_target_recovery_required(name) from reload_error
        if observed == intended:
            return True
        if observed != previous:
            raise _application_target_recovery_required(name)
        try:
            rollback()
        except Exception as recovery_error:
            raise _application_target_recovery_required(name) from recovery_error
        return False


def _application_target_recovery_required(name: str) -> ApplicationError:
    return ApplicationError(
        "application_target_recovery_required",
        f"Application Configuration Target {name!r} requires manual recovery",
        next_actions=(
            f"mastic application-target configure {name}",
            f"mastic application-target remove {name}",
        ),
    )


def _application_target_settings(
    name: str,
    parameters: Mapping[str, object],
    configuration: ApplicationTargetConfiguration,
    stored: ApplicationTargetSettings | None,
) -> ApplicationTargetSettings:
    service = str(
        configuration.service_identity
        or parameters.get("service")
        or (stored.service if stored else "")
    )
    if not service:
        raise ApplicationError(
            "invalid_parameter",
            "Application Configuration Target configuration requires an Inference Service",
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
        profile_name: ApplicationTargetSamplingSettings(
            temperature=profile_settings.temperature,
            top_p=profile_settings.top_p,
            top_k=profile_settings.top_k,
            min_p=profile_settings.min_p,
            presence_penalty=profile_settings.presence_penalty,
            repetition_penalty=profile_settings.repetition_penalty,
            max_tokens=profile_settings.max_tokens,
            enable_thinking=profile_settings.enable_thinking,
            preserve_thinking=profile_settings.preserve_thinking,
            upstream_profile=profile_settings.upstream_profile,
            source_url=profile_settings.source_url,
            source_revision=profile_settings.source_revision,
        )
        for profile_name, profile_settings in configuration.sampling_profiles.items()
    }
    return ApplicationTargetSettings(
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
