"""Concrete supported-v1 setup and removal operation orchestration.

The port keeps resolution side-effect free and crosses owner boundaries only
after the exact rendered preview has been confirmed. Its collaborators are
operation ports so composition can bind real runtime, model, desired-state,
Application Configuration Target, Supervisor, and Gateway implementations without hiding work here.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Protocol
from urllib.parse import urlsplit

from mastic.application.dispatch import ApplicationError
from mastic.application.setup import (
    Completion,
    ExactSetupSelection,
    MutationExecutionError,
    MutationStep,
    NoValidatedFitError,
    PHASE1_APPLICATION_VERSIONS,
    PHASE1_PERFORMANCE_PROFILE_ID,
    PHASE1_PERFORMANCE_PROFILE_VERSION,
    Readiness,
    RemovalInventory,
    ResolvedRemoval,
    SetupEvidence,
    ResolvedSetup,
    SetupResolver,
    SetupPreflight,
    SetupRequest,
    StepState,
)


_GIB = 1024**3
PHASE1_HOST_PERFORMANCE_PROFILE: Mapping[str, object] = {
    "id": PHASE1_PERFORMANCE_PROFILE_ID,
    "version": PHASE1_PERFORMANCE_PROFILE_VERSION,
    "status": "provisional",
    "host": {
        "platform": "darwin",
        "machine": "arm64",
        "minimum_memory_bytes": 48 * _GIB,
        "macos_major_versions": (15, 26),
    },
    "plan": {
        # Empirical validation must publish the exact production Plan digest
        # before this provisional profile may make readiness claims.
        "selection_sha256": "8bedb6280a52b8433da54a485e43b537714980511ae312cd81b8a82769402b56",
        "application_versions": dict(PHASE1_APPLICATION_VERSIONS),
    },
    "metrics": {
        "codex.native_canary.duration_seconds": {
            "unit": "seconds",
            "expected": {"maximum": 60.0},
            "degraded": {"minimum_exclusive": 60.0},
        },
        "hindsight.native_canary.duration_seconds": {
            "unit": "seconds",
            "expected": {"maximum": 180.0},
            "degraded": {"minimum_exclusive": 180.0},
        },
    },
}


class OperationOwner(Protocol):
    """One bounded owner of named product operations."""

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]: ...


class EvidenceStore(Protocol):
    """Durable, content-free completion evidence for resumable operations."""

    def load(self, scope: str) -> Sequence[SetupEvidence]: ...

    def record(self, scope: str, evidence: SetupEvidence) -> object: ...


class OperationalState(Protocol):
    """Subset of OperationalStateStore used by setup evidence."""

    def put_snapshot(self, snapshot: Mapping[str, object]) -> Mapping[str, object]: ...

    def snapshots(self, kind: str) -> Sequence[Mapping[str, object]]: ...


class OperationalSetupEvidenceStore:
    """Persist setup evidence as immutable operational-state snapshots."""

    def __init__(self, state: OperationalState) -> None:
        self._state = state

    def load(self, scope: str) -> tuple[SetupEvidence, ...]:
        return tuple(
            SetupEvidence(
                step_id=str(item["id"]),
                fingerprint=str(item["version"]),
                state=StepState(str(item["state"])),
                detail=str(item.get("detail", "")),
            )
            for item in self._state.snapshots(_evidence_kind(scope))
        )

    def record(self, scope: str, evidence: SetupEvidence) -> Mapping[str, object]:
        return self._state.put_snapshot(
            {
                "kind": _evidence_kind(scope),
                "id": evidence.step_id,
                "version": evidence.fingerprint,
                "state": evidence.state.value,
                "detail": evidence.detail,
            }
        )


PreflightProvider = Callable[[bool], SetupPreflight]
RemovalInventoryProvider = Callable[[], RemovalInventory]


class SetupOperationPort:
    """Preview and apply one exact, resumable setup operation."""

    def __init__(
        self,
        resolver: SetupResolver,
        *,
        preflight: PreflightProvider,
        runtime: OperationOwner,
        model: OperationOwner,
        config: OperationOwner,
        applications: OperationOwner,
        application_targets: OperationOwner,
        supervisor: OperationOwner,
        verifier: OperationOwner,
        evidence: EvidenceStore,
        removal_inventory: RemovalInventoryProvider,
        performance_profile: Mapping[str, object] | None = None,
    ) -> None:
        self._resolver = resolver
        self._preflight = preflight
        self._runtime = runtime
        self._model = model
        self._config = config
        self._applications = applications
        self._application_targets = application_targets
        self._supervisor = supervisor
        self._verifier = verifier
        self._evidence = evidence
        self._removal_inventory = removal_inventory
        selected_profile = (
            PHASE1_HOST_PERFORMANCE_PROFILE
            if performance_profile is None
            else performance_profile
        )
        copied_profile = _plain(selected_profile)
        if not isinstance(copied_profile, Mapping):
            raise ValueError("performance profile must be an object")
        _validate_performance_profile(copied_profile)
        self._performance_profile = copied_profile

    def preview(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        try:
            resolved = self._resolve_setup(parameters)
        except NoValidatedFitError as error:
            return _no_validated_fit(error)
        return self._setup_preview(resolved)

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        if operation != "setup":
            raise ApplicationError(
                "operation_unavailable", f"{operation} is not a setup operation"
            )
        try:
            resolved = self._resolve_setup(parameters)
        except NoValidatedFitError as error:
            return _no_validated_fit(error)
        preview = self._setup_preview(resolved)
        if parameters.get("confirmed") is not True or not parameters.get(
            "preview_fingerprint"
        ):
            return preview
        self._assert_preview_identity(parameters, preview)
        blocked = next(
            (step for step in resolved.steps if step.state is StepState.BLOCKED), None
        )
        if blocked is not None:
            code = "offline_blocked" if resolved.offline else "setup_blocked"
            raise ApplicationError(
                code,
                f"setup is blocked at {blocked.id}: {blocked.reason}",
                next_actions=("connect this Mac and retry", "mastic setup --help"),
            )

        prior = tuple(self._evidence.load("setup"))
        material = _restore_material(prior)
        results: dict[str, object] = {}

        def execute_step(step: MutationStep) -> SetupEvidence:
            result = self._execute_setup_step(resolved, step, material)
            results[step.id] = result
            material[(step.id, step.fingerprint)] = result
            return SetupEvidence.complete(
                step,
                _json(
                    {
                        "result": _content_free_result(
                            resolved,
                            step.id,
                            result,
                            self._performance_profile,
                        )
                    }
                ),
            )

        try:
            execution = self._resolver.apply(
                resolved,
                execute_step,
                evidence=prior,
                record=lambda item: self._evidence.record("setup", item),
            )
        except MutationExecutionError as error:
            completion, readiness, target_readiness = _setup_outcome(
                resolved,
                tuple(self._evidence.load("setup")),
                self._performance_profile,
            )
            raise ApplicationError(
                "setup_interrupted",
                str(error),
                next_actions=(
                    "rerun the same exact setup preview to resume",
                    "mastic operation list",
                ),
                details={
                    "state": "interrupted",
                    "complete": False,
                    "completion": completion,
                    "readiness": readiness,
                    "application_target_readiness": target_readiness,
                    "failed_step": error.step_id,
                },
            ) from error
        completion, readiness, target_readiness = _setup_outcome(
            resolved,
            execution.evidence,
            self._performance_profile,
        )
        return {
            **preview,
            "state": "complete",
            "complete": execution.complete,
            "completion": completion,
            "readiness": readiness,
            "application_target_readiness": target_readiness,
            "results": _plain(results),
            "evidence": [_plain(item) for item in execution.evidence],
        }

    def preview_removal(self) -> Mapping[str, object]:
        return self._removal_preview(
            self._resolver.resolve_removal(self._removal_inventory())
        )

    def remove(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        resolved = self._resolver.resolve_removal(self._removal_inventory())
        preview = self._removal_preview(resolved)
        if parameters.get("confirmed") is not True or not parameters.get(
            "preview_fingerprint"
        ):
            return preview
        self._assert_preview_identity(parameters, preview)
        prior = tuple(self._evidence.load("removal"))
        results: dict[str, object] = {}

        def execute_step(step: MutationStep) -> SetupEvidence:
            result = self._execute_removal_step(step)
            results[step.id] = result
            return SetupEvidence.complete(step, _json({"result": result}))

        try:
            execution = self._resolver.apply_removal(
                resolved,
                execute_step,
                evidence=prior,
                record=self._record_removal_evidence,
            )
        except MutationExecutionError as error:
            raise ApplicationError(
                "removal_interrupted",
                str(error),
                next_actions=("review the removal preview and resume",),
            ) from error
        return {
            **preview,
            "state": "complete",
            "complete": execution.complete,
            "results": _plain(results),
            "evidence": [_plain(item) for item in execution.evidence],
        }

    def _resolve_setup(self, parameters: Mapping[str, object]) -> ResolvedSetup:
        profile = str(parameters.get("profile", "recommended"))
        if profile not in {"recommended", "exact"}:
            raise ApplicationError(
                "invalid_parameter", "setup profile must be recommended or exact"
            )
        offline = bool(parameters.get("offline", False))
        try:
            facts = self._preflight(offline)
            prior = tuple(self._evidence.load("setup"))
            capacity = parameters.get("capacity")
            capacity_name = str(capacity) if capacity is not None else None
            intent = str(parameters.get("intent", "balanced"))
            if profile == "exact":
                missing = _missing_exact_selection(parameters)
                if missing:
                    raise ValueError(
                        "exact setup requires an exact selection: " + ", ".join(missing)
                    )
                selection = _selection(parameters, self._resolver.exact_template)
                explicit_selection = True
            else:
                baseline = self._resolver.resolve(
                    facts,
                    SetupRequest(
                        capacity_profile=capacity_name,
                        intent=intent,
                    ),
                    evidence=prior,
                )
                if capacity_name is None and baseline.capacity_profile is not None:
                    capacity_name = baseline.capacity_profile.name
                selection = _selection(parameters, baseline.selection)
                explicit_selection = _has_selection(parameters)
            request = SetupRequest(
                selection=selection if explicit_selection else None,
                capacity_profile=capacity_name,
                intent=intent,
                skip_canaries=_strings(parameters.get("skip_canaries", ())),
                noninteractive=bool(parameters.get("noninteractive", False)),
                confirmed=parameters.get("confirmed") is True,
            )
            return self._resolver.resolve(facts, request, evidence=prior)
        except NoValidatedFitError:
            raise
        except ValueError as error:
            raise ApplicationError("invalid_setup", str(error)) from error

    def _setup_preview(self, resolved: ResolvedSetup) -> Mapping[str, object]:
        preview = self._resolver.preview(resolved)
        completion, readiness, target_readiness = _setup_outcome(
            resolved,
            tuple(self._evidence.load("setup")),
            self._performance_profile,
        )
        identity = _preview_identity(
            resolved.steps,
            {
                "profile": resolved.profile_name,
                "intent": resolved.intent.value,
                "selection": _selection_value(resolved.selection),
                "offline": resolved.offline,
            },
        )
        return {
            "state": "review_required",
            "complete": completion == Completion.COMPLETE.value,
            "completion": completion,
            "readiness": readiness,
            "application_target_readiness": target_readiness,
            "profile": preview.profile_name,
            "intent": resolved.intent.value,
            "capacity": (
                {
                    "profile": preview.capacity_profile,
                    "context_window": preview.context_window,
                    "max_concurrent": preview.service_options.get("max_concurrent"),
                    "projected_kv_bytes": preview.projected_kv_bytes,
                    "prompt_cache_bytes": preview.service_options.get(
                        "prompt_cache_bytes"
                    ),
                    "description": preview.capacity_description,
                    "note": "Concurrency is the maximum number of simultaneous inference requests; idle Application Configuration Targets use no slot and excess requests queue. With OptiQ 0.3.3, 4-7 requests retain one simultaneous prefill; 8 permits two prefills and is riskier on 48 GiB Macs.",
                }
                if preview.capacity_profile is not None
                else None
            ),
            "editable": preview.editable,
            "confirmation_required": True,
            "preview_fingerprint": identity,
            "selection": {
                "runtime": preview.runtime,
                "runtime_lock_digest": preview.runtime_lock_digest,
                "model_repository": preview.model_repository,
                "model_revision": preview.model_revision,
                "trust_grants": list(preview.trust_grants),
                "service_name": preview.service_name,
                "model_alias": preview.model_alias,
                "service_route": preview.service_route,
                "activation": preview.activation,
                "pinned": preview.pinned,
                "service_options": _plain(preview.service_options),
                "gateway_endpoint": preview.gateway_endpoint,
                "application_targets": list(preview.application_targets),
                "application_target_options": _plain(
                    preview.application_target_options
                ),
                "context_window": preview.context_window,
            },
            "preflight": _plain(resolved.preflight),
            "steps": [_plain(step) for step in preview.steps],
            "offline_note": preview.offline_note,
            "performance_profile": _plain(self._performance_profile),
        }

    def _execute_setup_step(
        self,
        resolved: ResolvedSetup,
        step: MutationStep,
        material: Mapping[tuple[str, str], object],
    ) -> Mapping[str, object]:
        selection = resolved.selection
        if step.id == "preflight":
            return {"validated": True, **_plain(resolved.preflight)}
        if step.id == "supervisor.activate":
            return self._supervisor.execute("supervisor.start", {"confirmed": True})
        if step.id == "runtime.install":
            result = self._runtime.execute(
                "runtime.install",
                {
                    "runtime": selection.runtime_name,
                    "channel": "tested",
                    "expected_version": selection.runtime_version,
                    "expected_lock_digest": selection.runtime_lock_digest.removeprefix(
                        "sha256:"
                    ),
                    "confirmed": True,
                },
            )
            _validate_runtime_result(selection, result)
            return result
        if step.id == "model.install":
            result = self._model.execute(
                "model.install",
                {
                    "repository": selection.model_repository,
                    "revision": selection.model_revision,
                    "alias": selection.model_alias,
                    "offline": resolved.offline,
                    "confirmed": True,
                },
            )
            _validate_model_result(selection, result)
            if selection.trust_grants:
                runtime = _material_result(resolved, material, "runtime.install")
                self._config.execute(
                    "model.trust",
                    {
                        "resource": str(result["installation_id"]),
                        "runtime": str(runtime["installation_id"]),
                        "revision": selection.model_revision,
                        "accepted_risks": selection.trust_grants,
                        "confirmed": True,
                    },
                )
            return result
        if step.id == "service.configure":
            runtime = _material_result(resolved, material, "runtime.install")
            _material_result(resolved, material, "model.install")
            return self._config.execute(
                "service.create",
                {
                    "service": selection.service_name,
                    "resource": selection.service_name,
                    "model_alias": selection.model_alias,
                    "runtime": str(runtime["installation_id"]),
                    "route": selection.service_route,
                    "activation": selection.activation,
                    "pinned": selection.pinned,
                    "options": _plain(selection.service_options),
                    "confirmed": True,
                },
            )
        if step.id == "gateway.configure":
            endpoint = urlsplit(selection.gateway_endpoint)
            return self._config.execute(
                "gateway.configure",
                {
                    "host": str(endpoint.hostname),
                    "port": int(endpoint.port or 0),
                    "confirmed": True,
                },
            )
        if step.id == "application.install":
            result = self._applications.execute(
                "application.install",
                {
                    "application_targets": selection.application_targets,
                    "offline": resolved.offline,
                    "confirmed": True,
                },
            )
            _validate_application_install_result(selection, result)
            return result
        if step.id == "application-target.configure":
            configured = {}
            for application_target in selection.application_targets:
                options = _plain(
                    selection.application_target_options.get(application_target, {})
                )
                assert isinstance(options, Mapping)
                configured[application_target] = self._application_targets.execute(
                    "application-target.configure",
                    {
                        "application_target": application_target,
                        # Desired state refers to the internal service identity;
                        # the application target owner resolves its public Gateway route.
                        "service": selection.service_name,
                        "endpoint": selection.gateway_endpoint,
                        "context_window": selection.context_window,
                        **options,
                        "confirmed": True,
                    },
                )
            return configured
        if step.id == "service.start":
            return self._supervisor.execute(
                "service.start", {"resource": selection.service_name}
            )
        if step.id in {
            "application.canary.codex",
            "application.canary.hindsight",
        }:
            target = str(step.inputs["target"])
            result = self._application_targets.execute(
                "application-target.test",
                {
                    "application_target": target,
                    "profile": str(step.inputs["profile"]),
                },
            )
            response = result.get("response")
            if (
                not isinstance(response, Mapping)
                or response.get("ok") is not True
                or response.get("exact_contract") is not True
            ):
                raise RuntimeError(
                    f"the {target} application-native canary did not return the exact contract"
                )
            _duration_seconds(response)
            return result
        if step.id == "verify.request":
            result = self._verifier.execute("verify.request", step.inputs)
            if result.get("ok") is not True or result.get("text") != "mastic ready":
                raise RuntimeError(
                    "the first Gateway request did not return the exact contract response"
                )
            return result
        raise RuntimeError(f"unsupported setup step: {step.id}")

    def _removal_preview(self, resolved: ResolvedRemoval) -> Mapping[str, object]:
        identity = _preview_identity(
            resolved.steps,
            {
                "references": resolved.references,
                "freed_bytes_estimate": resolved.freed_bytes_estimate,
                "retained_paths": resolved.retained_paths,
                "retained_bytes_estimate": resolved.retained_bytes_estimate,
                "retained_settings": resolved.retained_settings,
            },
        )
        return {
            "state": "review_required",
            "confirmation_required": True,
            "preview_fingerprint": identity,
            "steps": [_plain(step) for step in resolved.steps],
            "references": _plain(resolved.references),
            "freed_bytes_estimate": resolved.freed_bytes_estimate,
            "retained_paths": list(resolved.retained_paths),
            "retained_bytes_estimate": resolved.retained_bytes_estimate,
            "retained_settings": list(resolved.retained_settings),
        }

    def _execute_removal_step(self, step: MutationStep) -> Mapping[str, object]:
        if step.id in {"service.drain", "service.stop"}:
            results = {}
            for service in _strings(step.inputs.get("services", ())):
                results[service] = self._supervisor.execute(
                    step.id, {"resource": service, "confirmed": True}
                )
            return results
        if step.id == "supervisor.unregister":
            return self._supervisor.execute(
                "supervisor.unregister", {"confirmed": True}
            )
        if step.id == "application-target.remove":
            results = {}
            for application_target in _strings(
                step.inputs.get("application_targets", ())
            ):
                results[application_target] = self._application_targets.execute(
                    "application-target.remove",
                    {"application_target": application_target, "confirmed": True},
                )
            return results
        if step.id == "application.remove":
            return self._applications.execute(
                "application.remove",
                {
                    "applications": tuple(
                        _strings(step.inputs.get("applications", ()))
                    ),
                    "confirmed": True,
                },
            )
        if step.id == "state.remove":
            paths = tuple(_strings(step.inputs.get("paths", ())))
            return self._config.execute(
                "state.remove", {"paths": paths, "confirmed": True}
            )
        raise RuntimeError(f"unsupported removal step: {step.id}")

    def _record_removal_evidence(self, evidence: SetupEvidence) -> None:
        # The last step removes the product-owned operational database itself.
        # Reopening it merely to record its own deletion would recreate state.
        if evidence.step_id != "state.remove":
            self._evidence.record("removal", evidence)

    @staticmethod
    def _assert_preview_identity(
        parameters: Mapping[str, object], preview: Mapping[str, object]
    ) -> None:
        if parameters.get("preview_fingerprint") != preview["preview_fingerprint"]:
            raise ApplicationError(
                "preview_changed",
                "the setup or removal preview changed after review",
                next_actions=("review the newly rendered preview",),
            )


def _selection(
    parameters: Mapping[str, object], baseline: ExactSetupSelection
) -> ExactSetupSelection:
    supplied = parameters.get("selection")
    if isinstance(supplied, ExactSetupSelection):
        return supplied
    overrides: dict[str, object] = {}
    if supplied is not None:
        if not isinstance(supplied, Mapping):
            raise ApplicationError(
                "invalid_parameter", "setup selection must be an object"
            )
        overrides.update(supplied)
    overrides.update(
        {
            key: value
            for key, value in parameters.items()
            if key
            in {
                "runtime_name",
                "runtime_version",
                "runtime_lock_digest",
                "model_repository",
                "model_revision",
                "trust_grants",
                "service_name",
                "model_alias",
                "service_route",
                "activation",
                "pinned",
                "service_options",
                "gateway_endpoint",
                "application_targets",
                "application_target_options",
                "context_window",
            }
        }
    )
    runtime_keys = {
        "runtime_name",
        "runtime_version",
        "runtime_lock_digest",
    }
    if runtime_keys & set(overrides) and not runtime_keys <= set(overrides):
        raise ApplicationError(
            "invalid_setup",
            "changing the runtime requires its name, exact version, and lock digest",
        )
    model_keys = {"model_repository", "model_revision"}
    if model_keys & set(overrides) and "trust_grants" not in overrides:
        raise ApplicationError(
            "invalid_setup",
            "changing the model requires explicit revision-scoped trust_grants",
        )
    trust = (
        _strings(overrides["trust_grants"])
        if "trust_grants" in overrides
        else baseline.trust_grants
    )
    application_targets = (
        _strings(overrides["application_targets"])
        if "application_targets" in overrides
        else baseline.application_targets
    )
    application_target_options = overrides.get(
        "application_target_options", baseline.application_target_options
    )
    if not isinstance(application_target_options, Mapping) or not all(
        isinstance(value, Mapping) for value in application_target_options.values()
    ):
        raise ApplicationError(
            "invalid_setup", "application_target_options must be an object"
        )
    service_options = overrides.get("service_options", baseline.service_options)
    if not isinstance(service_options, Mapping):
        raise ApplicationError("invalid_setup", "service_options must be an object")
    service_name = str(overrides.get("service_name", baseline.service_name))
    model_alias = overrides.get("model_alias", baseline.model_alias)
    service_route = overrides.get("service_route", baseline.service_route)
    if "service_name" in overrides:
        if (
            "model_alias" not in overrides
            and baseline.model_alias == baseline.service_name
        ):
            model_alias = service_name
        if (
            "service_route" not in overrides
            and baseline.service_route == baseline.service_name
        ):
            service_route = service_name
    return ExactSetupSelection(
        runtime_name=str(overrides.get("runtime_name", baseline.runtime_name)),
        runtime_version=str(overrides.get("runtime_version", baseline.runtime_version)),
        runtime_lock_digest=str(
            overrides.get("runtime_lock_digest", baseline.runtime_lock_digest)
        ),
        model_repository=str(
            overrides.get("model_repository", baseline.model_repository)
        ),
        model_revision=str(overrides.get("model_revision", baseline.model_revision)),
        trust_grants=trust,
        service_name=service_name,
        model_alias=str(model_alias) if model_alias is not None else None,
        service_route=str(service_route) if service_route is not None else None,
        activation=str(overrides.get("activation", baseline.activation)),
        pinned=overrides.get("pinned", baseline.pinned),  # type: ignore[arg-type]
        service_options=service_options,
        gateway_endpoint=str(
            overrides.get("gateway_endpoint", baseline.gateway_endpoint)
        ),
        application_targets=application_targets,
        application_target_options=application_target_options,  # type: ignore[arg-type]
        context_window=_optional_int(
            overrides.get("context_window", baseline.context_window)
        ),
    )


def _has_selection(parameters: Mapping[str, object]) -> bool:
    return "selection" in parameters or any(
        key
        in {
            "runtime_name",
            "runtime_version",
            "runtime_lock_digest",
            "model_repository",
            "model_revision",
            "trust_grants",
            "service_name",
            "model_alias",
            "service_route",
            "activation",
            "pinned",
            "service_options",
            "gateway_endpoint",
            "application_targets",
            "application_target_options",
            "context_window",
        }
        for key in parameters
    )


def _missing_exact_selection(parameters: Mapping[str, object]) -> tuple[str, ...]:
    supplied = parameters.get("selection")
    if isinstance(supplied, ExactSetupSelection):
        return ()
    values: dict[str, object] = dict(supplied) if isinstance(supplied, Mapping) else {}
    values.update(parameters)
    required = (
        "runtime_name",
        "runtime_version",
        "runtime_lock_digest",
        "model_repository",
        "model_revision",
        "trust_grants",
        "service_name",
        "gateway_endpoint",
    )
    return tuple(name for name in required if name not in values)


def _validate_runtime_result(
    selection: ExactSetupSelection, result: Mapping[str, object]
) -> None:
    expected_digest = selection.runtime_lock_digest.removeprefix("sha256:")
    required = {
        "runtime": selection.runtime_name,
        "version": selection.runtime_version,
        "provenance": "tested",
        "lock_sha256": expected_digest,
    }
    mismatched = [
        key for key, expected in required.items() if result.get(key) != expected
    ]
    if mismatched or not result.get("installation_id") or not result.get("bundle_id"):
        fields = ", ".join(mismatched) or "installation_id or bundle_id"
        raise RuntimeError(f"Runtime Installation evidence did not match: {fields}")


def _validate_model_result(
    selection: ExactSetupSelection, result: Mapping[str, object]
) -> None:
    if result.get("revision") != selection.model_revision or not result.get(
        "installation_id"
    ):
        raise RuntimeError("Model Installation did not match the exact Model Revision")


def _validate_application_install_result(
    selection: ExactSetupSelection, result: Mapping[str, object]
) -> None:
    installed = result.get("applications")
    if not isinstance(installed, Mapping):
        raise RuntimeError("application installer returned no exact application set")
    versions = {"codex": "0.144.1", "hindsight": "0.8.4"}
    for target in selection.application_targets:
        item = installed.get(target)
        if not isinstance(item, Mapping) or item.get("version") != versions[target]:
            raise RuntimeError(
                f"application installer did not return exact {target} {versions[target]}"
            )


def _material_result(
    resolved: ResolvedSetup,
    material: Mapping[tuple[str, str], object],
    step_id: str,
) -> Mapping[str, object]:
    step = next((item for item in resolved.steps if item.id == step_id), None)
    result = material.get((step.id, step.fingerprint)) if step is not None else None
    if not isinstance(result, Mapping):
        raise RuntimeError(
            f"matching {step_id} evidence lacks resumable material; rerun that step"
        )
    return result


def _restore_material(
    evidence: Sequence[SetupEvidence],
) -> dict[tuple[str, str], object]:
    restored: dict[tuple[str, str], object] = {}
    for item in evidence:
        if item.state is not StepState.COMPLETE or not item.detail:
            continue
        try:
            payload = json.loads(item.detail)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping) and isinstance(payload.get("result"), Mapping):
            restored[(item.step_id, item.fingerprint)] = dict(payload["result"])
    return restored


def _content_free_result(
    resolved: ResolvedSetup,
    step_id: str,
    result: Mapping[str, object],
    performance_profile: Mapping[str, object],
) -> Mapping[str, object]:
    if step_id.startswith("application.canary."):
        response = result.get("response")
        if not isinstance(response, Mapping):
            return {"profile": result.get("profile"), "ok": False}
        target = step_id.removeprefix("application.canary.")
        duration_seconds = _duration_seconds(response)
        metric = f"{target}.native_canary.duration_seconds"
        return {
            "profile": result.get("profile"),
            "ok": response.get("ok") is True,
            "exact_contract": response.get("exact_contract") is True,
            "phases": response.get("phases", ()),
            "evidence_sha256": response.get("evidence_sha256"),
            "performance": {
                "metric": metric,
                "value": duration_seconds,
                "unit": "seconds",
                "band": _performance_band(
                    resolved, metric, duration_seconds, performance_profile
                ),
                "profile_id": performance_profile.get("id"),
                "profile_version": performance_profile.get("version"),
            },
        }
    if step_id != "verify.request":
        return result
    text = str(result.get("text", ""))
    return {
        "ok": result.get("ok") is True,
        "response_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def _setup_outcome(
    resolved: ResolvedSetup,
    evidence: Sequence[SetupEvidence],
    performance_profile: Mapping[str, object],
) -> tuple[str, str, Mapping[str, str]]:
    current = {
        (item.step_id, item.fingerprint): item
        for item in evidence
        if item.state in {StepState.COMPLETE, StepState.SKIPPED}
    }
    terminal = all((step.id, step.fingerprint) in current for step in resolved.steps)
    completion = Completion.COMPLETE if terminal else Completion.PARTIAL
    target_readiness: dict[str, str] = {}
    for target in resolved.selection.application_targets:
        step = next(
            item for item in resolved.steps if item.id == f"application.canary.{target}"
        )
        outcome = current.get((step.id, step.fingerprint))
        if outcome is None:
            target_readiness[target] = Readiness.PENDING.value
        elif outcome.state is StepState.SKIPPED:
            target_readiness[target] = Readiness.UNVERIFIED.value
        else:
            target_readiness[target] = _evidenced_canary_readiness(
                resolved,
                target,
                outcome,
                performance_profile,
            )
    if target_readiness:
        values = set(target_readiness.values())
        if Readiness.PENDING.value in values:
            readiness = Readiness.PENDING
        elif Readiness.UNVERIFIED.value in values:
            readiness = Readiness.UNVERIFIED
        elif Readiness.DEGRADED.value in values:
            readiness = Readiness.DEGRADED
        else:
            readiness = Readiness.READY
    else:
        verification = next(
            (item for item in resolved.steps if item.id == "verify.request"),
            None,
        )
        readiness = (
            Readiness.READY
            if verification is not None
            and (verification.id, verification.fingerprint) in current
            else Readiness.PENDING
        )
    return completion.value, readiness.value, target_readiness


def _duration_seconds(response: Mapping[str, object]) -> float:
    raw = response.get("duration_seconds")
    if type(raw) not in {int, float} or not math.isfinite(raw) or raw < 0:
        raise RuntimeError(
            "the application-native canary did not return a finite nonnegative duration"
        )
    return float(raw)


def _performance_plan_fingerprint(selection: ExactSetupSelection) -> str:
    return hashlib.sha256(
        _json(
            {
                "selection": _selection_value(selection),
                "application_versions": PHASE1_APPLICATION_VERSIONS,
            }
        ).encode()
    ).hexdigest()


def _macos_major(version: str) -> int | None:
    major, _, _ = version.partition(".")
    if not major.isdecimal():
        return None
    return int(major)


def _validate_performance_profile(profile: Mapping[str, object]) -> None:
    if not isinstance(profile.get("id"), str) or not profile["id"]:
        raise ValueError("performance profile id must be a nonempty string")
    if type(profile.get("version")) is not int or profile["version"] <= 0:
        raise ValueError("performance profile version must be a positive integer")
    if profile.get("status") not in {"provisional", "validated"}:
        raise ValueError("performance profile status must be provisional or validated")
    host = profile.get("host")
    plan = profile.get("plan")
    metrics = profile.get("metrics")
    if not isinstance(host, Mapping):
        raise ValueError("performance profile host must be an object")
    if not isinstance(plan, Mapping):
        raise ValueError("performance profile plan must be an object")
    if not isinstance(metrics, Mapping):
        raise ValueError("performance profile metrics must be an object")
    if host.get("platform") != "darwin" or host.get("machine") != "arm64":
        raise ValueError("performance profile host must be darwin arm64")
    if (
        type(host.get("minimum_memory_bytes")) is not int
        or host["minimum_memory_bytes"] <= 0
    ):
        raise ValueError(
            "performance profile host minimum_memory_bytes must be positive"
        )
    macos_versions = host.get("macos_major_versions")
    if (
        not isinstance(macos_versions, Sequence)
        or isinstance(macos_versions, (str, bytes))
        or not macos_versions
        or any(type(item) is not int or item <= 0 for item in macos_versions)
    ):
        raise ValueError(
            "performance profile host macos_major_versions must be positive integers"
        )
    selection_sha256 = plan.get("selection_sha256")
    if (
        not isinstance(selection_sha256, str)
        or len(selection_sha256) != 64
        or any(character not in "0123456789abcdef" for character in selection_sha256)
    ):
        raise ValueError("performance profile plan requires an exact selection sha256")
    application_versions = plan.get("application_versions")
    if not isinstance(application_versions, Mapping) or any(
        not isinstance(name, str)
        or not name
        or not isinstance(version, str)
        or not version
        for name, version in (
            application_versions.items()
            if isinstance(application_versions, Mapping)
            else ()
        )
    ):
        raise ValueError(
            "performance profile plan application_versions must be nonempty strings"
        )
    for metric in (
        "codex.native_canary.duration_seconds",
        "hindsight.native_canary.duration_seconds",
    ):
        band = metrics.get(metric)
        if not isinstance(band, Mapping):
            raise ValueError(f"performance profile requires metric {metric}")
        expected = band.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"performance profile metric {metric} requires expected")
        maximum = expected.get("maximum")
        if (
            type(maximum) not in {int, float}
            or not math.isfinite(maximum)
            or maximum < 0
        ):
            raise ValueError(
                f"performance profile metric {metric} maximum must be nonnegative"
            )


def _performance_band(
    resolved: ResolvedSetup,
    metric: str,
    value: float,
    performance_profile: Mapping[str, object],
) -> str:
    if performance_profile.get("status") != "validated":
        return Readiness.UNVERIFIED.value
    host = performance_profile.get("host")
    plan = performance_profile.get("plan")
    metrics = performance_profile.get("metrics")
    assert isinstance(host, Mapping)
    assert isinstance(plan, Mapping)
    assert isinstance(metrics, Mapping)
    if (
        resolved.preflight.platform != host["platform"]
        or resolved.preflight.machine != host["machine"]
        or resolved.preflight.memory_bytes < host["minimum_memory_bytes"]
        or _macos_major(resolved.preflight.os_version)
        not in host.get("macos_major_versions", ())
        or plan.get("application_versions") != dict(PHASE1_APPLICATION_VERSIONS)
        or plan.get("selection_sha256")
        != _performance_plan_fingerprint(resolved.selection)
    ):
        return Readiness.UNVERIFIED.value
    raw_band = metrics.get(metric)
    if not isinstance(raw_band, Mapping):
        return Readiness.UNVERIFIED.value
    expected = raw_band.get("expected")
    if not isinstance(expected, Mapping):
        return Readiness.UNVERIFIED.value
    maximum = expected.get("maximum")
    if type(maximum) not in {int, float}:
        return Readiness.UNVERIFIED.value
    if value <= float(maximum):
        return "expected"
    return Readiness.DEGRADED.value


def _evidenced_canary_readiness(
    resolved: ResolvedSetup,
    target: str,
    evidence: SetupEvidence,
    performance_profile: Mapping[str, object],
) -> str:
    try:
        payload = json.loads(evidence.detail)
    except json.JSONDecodeError:
        return Readiness.UNVERIFIED.value
    if not isinstance(payload, Mapping):
        return Readiness.UNVERIFIED.value
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return Readiness.UNVERIFIED.value
    performance = result.get("performance")
    if not isinstance(performance, Mapping):
        return Readiness.UNVERIFIED.value
    if performance.get("profile_id") != performance_profile.get(
        "id"
    ) or performance.get("profile_version") != performance_profile.get("version"):
        return Readiness.UNVERIFIED.value
    metric = f"{target}.native_canary.duration_seconds"
    if performance.get("metric") != metric:
        return Readiness.UNVERIFIED.value
    try:
        value = _duration_seconds({"duration_seconds": performance.get("value")})
    except RuntimeError:
        return Readiness.UNVERIFIED.value
    band = _performance_band(resolved, metric, value, performance_profile)
    if performance.get("band") != band:
        return Readiness.UNVERIFIED.value
    if band == "expected":
        return Readiness.READY.value
    if band == Readiness.DEGRADED.value:
        return Readiness.DEGRADED.value
    return Readiness.UNVERIFIED.value


def _no_validated_fit(error: NoValidatedFitError) -> Mapping[str, object]:
    outcome = error.outcome
    return {
        "state": outcome.state,
        "complete": True,
        "completion": "complete",
        "confirmation_required": False,
        "readiness": "unverified",
        "limiting_evidence": outcome.limiting_evidence,
        "remediation": list(outcome.remediation),
        "mutation_count": 0,
    }


def _preview_identity(
    steps: Sequence[MutationStep], extra: Mapping[str, object]
) -> str:
    payload = {
        "steps": [{"id": step.id, "fingerprint": step.fingerprint} for step in steps],
        **dict(extra),
    }
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def _selection_value(selection: ExactSetupSelection) -> Mapping[str, object]:
    return {
        "runtime_name": selection.runtime_name,
        "runtime_version": selection.runtime_version,
        "runtime_lock_digest": selection.runtime_lock_digest,
        "model_repository": selection.model_repository,
        "model_revision": selection.model_revision,
        "trust_grants": selection.trust_grants,
        "service_name": selection.service_name,
        "model_alias": selection.model_alias,
        "service_route": selection.service_route,
        "activation": selection.activation,
        "pinned": selection.pinned,
        "service_options": selection.service_options,
        "gateway_endpoint": selection.gateway_endpoint,
        "application_targets": selection.application_targets,
        "application_target_options": selection.application_target_options,
        "context_window": selection.context_window,
    }


def _evidence_kind(scope: str) -> str:
    if scope not in {"setup", "removal"}:
        raise ValueError(f"unknown setup evidence scope: {scope}")
    return f"{scope}_evidence"


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if not all(isinstance(item, str) and item for item in value):
            raise ApplicationError("invalid_parameter", "expected nonempty strings")
        return tuple(value)
    raise ApplicationError("invalid_parameter", "expected strings")


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise ApplicationError(
            "invalid_parameter", "context_window must be a positive integer"
        )
    return value


def _plain(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _plain(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_plain(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def _json(value: object) -> str:
    return json.dumps(_plain(value), separators=(",", ":"), sort_keys=True)
