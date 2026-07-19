"""Operation-port adapters shared by local interfaces and the Supervisor."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from mastic.application.application_targets import (
    APPLICATION_CANARY_CONTRACTS,
    ApplicationTargetDriftOutcome,
    ApplicationTargetDriftIntent,
    validate_application_target_sampling_profiles,
)
from mastic.application.config_schema import (
    ApplicationTargetSettings,
    validate_hindsight_profile_name,
)
from mastic.application.dispatch import ApplicationError
from mastic.application.serialization import to_plain_data as _plain
from mastic.infrastructure.control_client import ControlClientError, UnixControlClient
from mastic.infrastructure.application_target_integrations import (
    ApplicationTargetConfiguration,
    ApplicationTargetIntegrationConflict,
    ApplicationTargetOwnershipRecoveryRequired,
    CodexTargetOptions,
    HindsightTargetOptions,
)
from mastic.infrastructure.supervisor_v1 import Supervisor


_APPLICATION_TARGET_NAMES = frozenset({"codex", "hindsight"})


class ControlClient(Protocol):
    def execute(
        self, operation: str, parameters: Mapping[str, object] | None = None
    ): ...

    def cancel(self, operation_id: str): ...


class ApplicationTargetAdapter(Protocol):
    def inspect(self): ...

    def preview(self, configuration: ApplicationTargetConfiguration): ...

    def apply(
        self, configuration: ApplicationTargetConfiguration, *, takeover: bool = False
    ): ...

    def remove(self): ...

    def rollback_point(self) -> Callable[[], None]: ...

    def observe_drift(
        self, configuration: ApplicationTargetConfiguration
    ) -> Mapping[str, object]: ...

    def adopt_drift(self, configuration: ApplicationTargetConfiguration): ...

    def relinquish(self): ...


class ApplicationTargetCanary(Protocol):
    def run(
        self,
        target: str,
        configuration: ApplicationTargetConfiguration,
        settings: ApplicationTargetSettings,
        *,
        profile: str,
    ) -> Mapping[str, object]: ...


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
            previous_route = parameters.get("previous_route")
            value = self._supervisor.remove_service(
                resource,
                previous_route=(
                    str(previous_route) if isinstance(previous_route, str) else None
                ),
            )
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
        canary: ApplicationTargetCanary,
        settings: Callable[[str], ApplicationTargetSettings | None],
        record: Callable[[str, ApplicationTargetSettings | None], object],
        transition: Callable[[str], AbstractContextManager[None]],
    ) -> None:
        self._adapter = adapter
        self._configuration = configuration
        self._canary = canary
        self._settings = settings
        self._record = record
        self._transition = transition

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
                profile = validate_hindsight_profile_name(
                    parameters.get("profile")
                    or (stored.profile if stored is not None else None)
                )
                if stored is not None and stored.profile != profile:
                    raise ApplicationError(
                        "integration_conflict",
                        "Remove the owned Hindsight integration before selecting a different profile",
                        next_actions=("mastic application-target remove hindsight",),
                    )
            adapter = self._adapter(operation, name, parameters, stored)
        except ApplicationError:
            raise
        except ApplicationTargetOwnershipRecoveryRequired as error:
            raise _application_target_ownership_recovery_required(name) from error
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
            return _plain(adapter.inspect())
        if operation == "application-target.configure" and stored is not None:
            inspection = adapter.inspect()
            if not isinstance(inspection, Mapping):
                raise ApplicationError(
                    "application_target_recovery_required",
                    f"Application Configuration Target {name!r} returned an invalid inspection",
                    next_actions=(f"mastic application-target inspect {name}",),
                )
            if inspection.get("state") != "healthy":
                return self._resolve_drift(name, parameters, stored, adapter)
            if parameters.get("drift_resolution") is not None:
                raise ApplicationError(
                    "invalid_parameter",
                    f"Application Configuration Target {name!r} has no drift to resolve",
                )
        try:
            configuration = self._configuration(name, parameters, stored)
        except ApplicationError:
            raise
        except (KeyError, ValueError) as error:
            raise ApplicationError(
                "invalid_parameter",
                str(error),
                next_actions=(f"mastic {operation.replace('.', ' ')} --help",),
            ) from error
        assert configuration is not None
        if operation == "application-target.configure":
            try:
                validate_application_target_sampling_profiles(
                    name, configuration.sampling_profiles
                )
            except ValueError as error:
                raise ApplicationError(
                    "invalid_parameter",
                    str(error),
                    next_actions=(
                        f"mastic application-target configure {name} --help",
                    ),
                ) from error
            takeover = parameters.get("takeover", False)
            if type(takeover) is not bool:
                raise ApplicationError(
                    "invalid_parameter", "takeover must be a boolean"
                )
            desired_settings = _application_target_settings(
                name, parameters, configuration, stored
            )
            preview = adapter.preview(configuration)
            rollback = adapter.rollback_point()
            result = adapter.apply(configuration, takeover=takeover)
            try:
                self._record(name, desired_settings)
            except Exception:
                if self._settle_record_error(name, stored, desired_settings, rollback):
                    return {"preview": _plain(preview), "result": _plain(result)}
                raise
            return {"preview": _plain(preview), "result": _plain(result)}
        if operation == "application-target.test":
            assert stored is not None
            contract = APPLICATION_CANARY_CONTRACTS[name]
            profile = str(parameters.get("profile") or contract.profile)
            try:
                inspection = adapter.inspect()
                if (
                    not isinstance(inspection, Mapping)
                    or inspection.get("state") != "healthy"
                ):
                    raise ApplicationError(
                        "application_target_drifted",
                        f"Application Configuration Target {name!r} is not healthy",
                        next_actions=(f"mastic application-target inspect {name}",),
                    )
                response = self._canary.run(
                    name,
                    configuration,
                    stored,
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

    def _resolve_drift(
        self,
        name: str,
        parameters: Mapping[str, object],
        stored: ApplicationTargetSettings,
        adapter: ApplicationTargetAdapter,
    ) -> Mapping[str, object]:
        raw_resolution = parameters.get("drift_resolution")
        if raw_resolution is None:
            raise _application_target_drifted(name)
        try:
            resolution = ApplicationTargetDriftIntent(str(raw_resolution))
        except ValueError as error:
            raise ApplicationError(
                "invalid_parameter",
                "drift_resolution must be reapply, adopt, or relinquish",
                next_actions=(f"mastic application-target inspect {name}",),
            ) from error
        desired_parameters: dict[str, object] = {"service": stored.service}
        if stored.profile is not None:
            desired_parameters["profile"] = stored.profile
        desired = self._configuration(name, desired_parameters, stored)
        if resolution is ApplicationTargetDriftIntent.REAPPLY:
            preview = adapter.preview(desired)
            result = adapter.apply(desired)
            return {
                **_plain(
                    ApplicationTargetDriftOutcome(
                        name,
                        resolution,
                        external_configuration_changed=_changed(result),
                        desired_state_changed=False,
                        ownership_changed=_changed(result),
                    )
                ),
                "preview": _plain(preview),
            }
        if resolution is ApplicationTargetDriftIntent.RELINQUISH:
            rollback = adapter.rollback_point()
            result = adapter.relinquish()
            try:
                self._record(name, None)
            except Exception:
                if self._settle_record_error(name, stored, None, rollback):
                    pass
                else:
                    raise
            return {
                **_plain(
                    ApplicationTargetDriftOutcome(
                        name,
                        resolution,
                        external_configuration_changed=False,
                        desired_state_changed=True,
                        ownership_changed=_changed(result),
                    )
                )
            }
        try:
            observed_parameters = adapter.observe_drift(desired)
            observed_parameters = {
                **desired_parameters,
                **observed_parameters,
            }
            observed = self._configuration(name, observed_parameters, stored)
            validate_application_target_sampling_profiles(
                name, observed.sampling_profiles
            )
            adopted_settings = _application_target_settings(
                name, observed_parameters, observed, stored
            )
        except (
            ApplicationTargetIntegrationConflict,
            KeyError,
            OSError,
            TypeError,
            UnicodeError,
            ValueError,
        ) as error:
            raise _application_target_adoption_blocked(name) from error
        rollback = adapter.rollback_point()
        try:
            result = adapter.adopt_drift(observed)
        except (
            ApplicationTargetIntegrationConflict,
            UnicodeError,
            ValueError,
        ) as error:
            raise _application_target_adoption_blocked(name) from error
        try:
            self._record(name, adopted_settings)
        except Exception:
            if self._settle_record_error(name, stored, adopted_settings, rollback):
                pass
            else:
                raise
        return {
            **_plain(
                ApplicationTargetDriftOutcome(
                    name,
                    resolution,
                    external_configuration_changed=False,
                    desired_state_changed=adopted_settings != stored,
                    ownership_changed=_changed(result),
                )
            )
        }

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


def _application_target_drifted(name: str) -> ApplicationError:
    command = f"mastic application-target configure {name} --drift-resolution"
    return ApplicationError(
        "application_target_drifted",
        f"Application Configuration Target {name!r} changed outside MASTIC",
        next_actions=(
            f"{command} reapply",
            f"{command} adopt",
            f"{command} relinquish",
        ),
    )


def _application_target_adoption_blocked(name: str) -> ApplicationError:
    return ApplicationError(
        "application_target_adoption_blocked",
        f"Application Configuration Target {name!r} cannot be adopted losslessly",
        next_actions=(
            f"mastic application-target configure {name} --drift-resolution reapply",
            f"mastic application-target configure {name} --drift-resolution relinquish",
        ),
    )


def _changed(result: object) -> bool:
    plain = _plain(result)
    return bool(plain.get("changed")) if isinstance(plain, Mapping) else False


def _application_target_ownership_recovery_required(name: str) -> ApplicationError:
    return ApplicationError(
        "application_target_recovery_required",
        f"Application Configuration Target {name!r} has ownership evidence that requires manual recovery",
        next_actions=(
            "move invalid or conflicting ownership manifests out of the mastic application-target ownership directory",
            f"mastic application-target inspect {name}",
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
        if not isinstance(configuration.target, HindsightTargetOptions):
            raise ValueError(
                "Hindsight configuration requires Hindsight target options"
            )
        profile = validate_hindsight_profile_name(
            parameters.get("profile")
            or (stored.profile if stored is not None else None)
        )
        provider = configuration.target.provider
        max_concurrent: int | None = configuration.target.max_concurrent
    else:
        if not isinstance(configuration.target, CodexTargetOptions):
            raise ValueError("Codex configuration requires Codex target options")
        profile = None
        provider = configuration.target.provider_id
        max_concurrent = None
    return ApplicationTargetSettings(
        name=name,
        kind=name,
        service=service,
        profile=profile,
        context_window=configuration.context_window,
        provider=provider,
        max_concurrent=max_concurrent,
        sampling=configuration.sampling_profiles,
    )
