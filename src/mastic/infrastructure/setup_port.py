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
from contextlib import AbstractContextManager, nullcontext
from typing import Protocol
from urllib.parse import urlsplit

from mastic.application.dispatch import ApplicationError
from mastic.application.plan_outcome import (
    ApplicationTargetObservation,
    PlanAssessmentInput,
    PlanOutcome,
    PlanOutcomePolicy,
    PlanStep,
)
from mastic.application.serialization import to_plain_data as _plain
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
        "selection_sha256": "7316e2d9b7271228199254ed30b0d89f243d4ad821502fbbc074c5a9654f5f60",
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


class SetupPlanStore(Protocol):
    """Persist and load the exact content-free Plan used by setup."""

    def record(self, plan: Mapping[str, object]) -> object: ...

    def load(self) -> Mapping[str, object] | None: ...


class OperationalState(Protocol):
    """Subset of OperationalStateStore used by setup evidence."""

    def put_snapshot(self, snapshot: Mapping[str, object]) -> Mapping[str, object]: ...

    def snapshots(self, kind: str) -> Sequence[Mapping[str, object]]: ...

    def snapshot_history(self, kind: str) -> Sequence[Mapping[str, object]]: ...


class OperationalPlanState(Protocol):
    def put_snapshot(self, snapshot: Mapping[str, object]) -> Mapping[str, object]: ...

    def snapshot(
        self, kind: str, resource_id: str, *, version: str | int | None = None
    ) -> Mapping[str, object] | None: ...

    def snapshot_history(self, kind: str) -> Sequence[Mapping[str, object]]: ...


class OperationalSetupEvidenceStore:
    """Persist setup evidence as immutable operational-state snapshots."""

    def __init__(self, state: OperationalState) -> None:
        self._state = state

    def load(self, scope: str) -> tuple[SetupEvidence, ...]:
        return tuple(
            SetupEvidence(
                step_id=str(item["id"]),
                fingerprint=str(item.get("fingerprint", item["version"])),
                state=StepState(str(item["state"])),
                detail=str(item.get("detail", "")),
            )
            for item in self._state.snapshot_history(_evidence_kind(scope))
        )

    def record(self, scope: str, evidence: SetupEvidence) -> Mapping[str, object]:
        record_version = hashlib.sha256(
            json.dumps(
                {
                    "detail": evidence.detail,
                    "fingerprint": evidence.fingerprint,
                    "state": evidence.state.value,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return self._state.put_snapshot(
            {
                "kind": _evidence_kind(scope),
                "id": evidence.step_id,
                "version": record_version,
                "fingerprint": evidence.fingerprint,
                "state": evidence.state.value,
                "detail": evidence.detail,
            }
        )


class OperationalSetupPlanStore:
    """Persist the active setup Plan envelope without selection or user content."""

    def __init__(self, state: OperationalPlanState) -> None:
        self._state = state

    def record(self, plan: Mapping[str, object]) -> Mapping[str, object]:
        normalized = _validated_plan(plan)
        activation = len(self._state.snapshot_history("setup_plan")) + 1
        return self._state.put_snapshot(
            {
                "kind": "setup_plan",
                "id": "active",
                "version": f"{activation}:{normalized['plan_identity']}",
                **normalized,
            }
        )

    def load(self) -> Mapping[str, object] | None:
        return self._state.snapshot("setup_plan", "active")


class DurableSetupOutcomeProvider:
    """Reconstruct setup completion and readiness from immutable evidence."""

    def __init__(
        self,
        plans: SetupPlanStore,
        evidence: EvidenceStore,
        performance_profile: Mapping[str, object] | None = None,
        *,
        application_targets: OperationOwner | None = None,
    ) -> None:
        self._plans = plans
        self._evidence = evidence
        self._performance_profile = (
            PHASE1_HOST_PERFORMANCE_PROFILE
            if performance_profile is None
            else performance_profile
        )
        self._outcome_policy = PlanOutcomePolicy(self._performance_profile)
        self._application_targets = application_targets

    def outcome(self) -> Mapping[str, object]:
        try:
            plan = self._plans.load()
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            return _conservative_setup_outcome(malformed=True)
        if plan is None:
            return _conservative_setup_outcome(malformed=False)
        try:
            normalized = _validated_plan(plan)
            assessment_plan = _plan_from_stored(normalized)
            evidence = tuple(self._evidence.load("setup"))
            observations, include_issues = self._observe_application_targets(
                assessment_plan
            )
            outcome = self._outcome_policy.assess(
                assessment_plan, evidence, observations
            )
            result: dict[str, object] = {
                "completion": outcome.completion.value,
                "readiness": outcome.readiness.value,
                "application_target_readiness": {
                    target: readiness.value
                    for target, readiness in outcome.application_target_readiness.items()
                },
                "evidence": ("setup-plan", "setup-evidence"),
            }
            if include_issues:
                result["application_target_issues"] = tuple(
                    {
                        "code": issue.code,
                        "application_target": issue.application_target,
                        "state": issue.state,
                        "message": issue.message,
                        "next_actions": issue.next_actions,
                    }
                    for issue in outcome.application_target_issues
                )
            return result
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            return _conservative_setup_outcome(malformed=True)

    def _observe_application_targets(
        self, plan: PlanAssessmentInput
    ) -> tuple[tuple[ApplicationTargetObservation, ...], bool]:
        if self._application_targets is None:
            return (), False
        observations: list[ApplicationTargetObservation] = []
        for target in plan.application_targets:
            try:
                inspection = self._application_targets.execute(
                    "application-target.inspect",
                    {"application_target": target},
                )
            except Exception:
                observations.append(ApplicationTargetObservation(target, "unknown"))
                continue
            if not isinstance(inspection, Mapping):
                observations.append(ApplicationTargetObservation(target, "unknown"))
                continue
            state = inspection.get("state")
            detail = inspection.get("detail")
            observations.append(
                ApplicationTargetObservation(
                    target,
                    state if isinstance(state, str) else "unknown",
                    detail if isinstance(detail, str) and detail else None,
                    _inspection_next_actions(inspection, target),
                )
            )
        return tuple(observations), True


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
        transition: Callable[[], AbstractContextManager[None]] | None = None,
        removal_transition: Callable[[], AbstractContextManager[None]] | None = None,
        plan_store: SetupPlanStore | None = None,
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
        self._transition = transition or nullcontext
        self._removal_transition = removal_transition or self._transition
        self._plan_store = plan_store
        selected_profile = (
            PHASE1_HOST_PERFORMANCE_PROFILE
            if performance_profile is None
            else performance_profile
        )
        copied_profile = _plain(selected_profile)
        if not isinstance(copied_profile, Mapping):
            raise ValueError("performance profile must be an object")
        self._performance_profile = copied_profile
        self._outcome_policy = PlanOutcomePolicy(copied_profile)

    def preview(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        try:
            resolved = self._resolve_setup(parameters)
        except NoValidatedFitError as error:
            return _no_validated_fit(error)
        return self._setup_preview(resolved)

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        if parameters.get("confirmed") is True and parameters.get(
            "preview_fingerprint"
        ):
            with self._transition():
                return self._execute(operation, parameters)
        return self._execute(operation, parameters)

    def _execute(
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

        assessment_plan = _plan_from_resolved(resolved)
        if self._plan_store is not None:
            self._plan_store.record(
                {
                    "plan_identity": str(preview["preview_fingerprint"]),
                    "steps": tuple(
                        _durable_plan_step(resolved.selection, step)
                        for step in resolved.steps
                    ),
                    "application_targets": resolved.selection.application_targets,
                    "performance_binding": assessment_plan.performance_binding,
                }
            )

        prior = self._outcome_policy.assess(
            assessment_plan, tuple(self._evidence.load("setup"))
        ).reusable_evidence
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
                            assessment_plan,
                            step.id,
                            result,
                            self._outcome_policy,
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
            current_evidence = tuple(self._evidence.load("setup"))
            completion, readiness, target_readiness = _outcome_values(
                self._outcome_policy.assess(assessment_plan, current_evidence)
            )
            terminal = {
                (item.step_id, item.fingerprint)
                for item in current_evidence
                if item.state in {StepState.COMPLETE, StepState.SKIPPED}
            }
            terminal.update(
                (step.id, step.fingerprint)
                for step in resolved.steps
                if step.state in {StepState.COMPLETE, StepState.SKIPPED}
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
                    "remaining_steps": [
                        step.id
                        for step in resolved.steps
                        if (step.id, step.fingerprint) not in terminal
                    ],
                    "observations": {
                        "preflight": _plain(resolved.preflight),
                        "completed_steps": [
                            step.id
                            for step in resolved.steps
                            if (step.id, step.fingerprint) in terminal
                        ],
                        "application_target_readiness": target_readiness,
                    },
                },
            ) from error
        completion, readiness, target_readiness = _outcome_values(
            self._outcome_policy.assess(assessment_plan, execution.evidence)
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
        if parameters.get("confirmed") is True and parameters.get(
            "preview_fingerprint"
        ):
            with self._removal_transition():
                return self._remove(parameters)
        return self._remove(parameters)

    def _remove(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
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
        offline = _boolean(parameters, "offline")
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
                noninteractive=_boolean(parameters, "noninteractive"),
                confirmed=parameters.get("confirmed") is True,
            )
            resolved = self._resolver.resolve(facts, request, evidence=prior)
            reusable = self._outcome_policy.assess(
                _plan_from_resolved(resolved), prior
            ).reusable_evidence
            if reusable != prior:
                resolved = self._resolver.resolve(facts, request, evidence=reusable)
            return resolved
        except NoValidatedFitError:
            raise
        except ValueError as error:
            raise ApplicationError("invalid_setup", str(error)) from error

    def _setup_preview(self, resolved: ResolvedSetup) -> Mapping[str, object]:
        preview = self._resolver.preview(resolved)
        completion, readiness, target_readiness = _outcome_values(
            self._outcome_policy.assess(
                _plan_from_resolved(resolved),
                tuple(self._evidence.load("setup")),
            )
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
    identities = (result.get("installation_id"), result.get("bundle_id"))
    if mismatched or any(
        not isinstance(identity, str) or not identity for identity in identities
    ):
        fields = ", ".join(mismatched) or "installation_id or bundle_id"
        raise RuntimeError(f"Runtime Installation evidence did not match: {fields}")


def _validate_model_result(
    selection: ExactSetupSelection, result: Mapping[str, object]
) -> None:
    installation_id = result.get("installation_id")
    if (
        result.get("revision") != selection.model_revision
        or not isinstance(installation_id, str)
        or not installation_id
    ):
        raise RuntimeError("Model Installation did not match the exact Model Revision")


def _validate_application_install_result(
    selection: ExactSetupSelection, result: Mapping[str, object]
) -> None:
    installed = result.get("applications")
    if not isinstance(installed, Mapping):
        raise RuntimeError("application installer returned no exact application set")
    for target in selection.application_targets:
        item = installed.get(target)
        expected_version = PHASE1_APPLICATION_VERSIONS[target]
        if not isinstance(item, Mapping) or item.get("version") != expected_version:
            raise RuntimeError(
                f"application installer did not return exact {target} {expected_version}"
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
        result = _evidence_result(item)
        if result is not None:
            restored[(item.step_id, item.fingerprint)] = dict(result)
    return restored


def _evidence_result(evidence: SetupEvidence) -> Mapping[str, object] | None:
    try:
        payload = json.loads(evidence.detail)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping):
        return None
    result = payload.get("result")
    return result if isinstance(result, Mapping) else None


def _material_expectation(
    selection: ExactSetupSelection, step_id: str
) -> Mapping[str, object] | None:
    if step_id == "runtime.install":
        return {
            "runtime": selection.runtime_name,
            "version": selection.runtime_version,
            "provenance": "tested",
            "lock_sha256": selection.runtime_lock_digest.removeprefix("sha256:"),
        }
    if step_id == "model.install":
        return {"revision": selection.model_revision}
    return None


def _material_expectation_valid(step_id: str, value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    if step_id == "runtime.install":
        if set(value) != {"runtime", "version", "provenance", "lock_sha256"}:
            return False
        lock_sha256 = value.get("lock_sha256")
        return bool(
            isinstance(value.get("runtime"), str)
            and bool(value.get("runtime"))
            and isinstance(value.get("version"), str)
            and bool(value.get("version"))
            and value.get("provenance") == "tested"
            and isinstance(lock_sha256, str)
            and len(lock_sha256) == 64
            and all(character in "0123456789abcdef" for character in lock_sha256)
        )
    if step_id == "model.install":
        revision = value.get("revision")
        return bool(
            set(value) == {"revision"}
            and isinstance(revision, str)
            and len(revision) in {40, 64}
            and all(character in "0123456789abcdef" for character in revision)
        )
    return False


def _durable_plan_step(
    selection: ExactSetupSelection, step: MutationStep
) -> Mapping[str, object]:
    record: dict[str, object] = {
        "id": step.id,
        "fingerprint": step.fingerprint,
        "state": step.state.value,
    }
    expected_result = _material_expectation(selection, step.id)
    if expected_result is not None:
        record["expected_result"] = expected_result
    return record


def _plan_from_resolved(resolved: ResolvedSetup) -> PlanAssessmentInput:
    return PlanAssessmentInput(
        steps=tuple(
            PlanStep(
                step.id,
                step.fingerprint,
                step.state,
                _material_expectation(resolved.selection, step.id),
            )
            for step in resolved.steps
        ),
        application_targets=resolved.selection.application_targets,
        performance_binding=_performance_binding(resolved),
    )


def _outcome_values(
    outcome: PlanOutcome,
) -> tuple[str, str, Mapping[str, str]]:
    return (
        outcome.completion.value,
        outcome.readiness.value,
        {
            target: readiness.value
            for target, readiness in outcome.application_target_readiness.items()
        },
    )


def _content_free_result(
    resolved: ResolvedSetup,
    plan: PlanAssessmentInput,
    step_id: str,
    result: Mapping[str, object],
    outcome_policy: PlanOutcomePolicy,
) -> Mapping[str, object]:
    if step_id.startswith("application.canary."):
        response = result.get("response")
        if not isinstance(response, Mapping):
            return {"profile": result.get("profile"), "ok": False}
        target = step_id.removeprefix("application.canary.")
        duration_seconds = _duration_seconds(response)
        return {
            "profile": result.get("profile"),
            "service": resolved.selection.service_name,
            "ok": response.get("ok") is True,
            "exact_contract": response.get("exact_contract") is True,
            "phases": response.get("phases", ()),
            "evidence_sha256": response.get("evidence_sha256"),
            "performance": outcome_policy.canary_performance(
                plan, target, duration_seconds
            ),
        }
    if step_id != "verify.request":
        return result
    text = str(result.get("text", ""))
    return {
        "ok": result.get("ok") is True,
        "response_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def _validated_plan(plan: Mapping[str, object]) -> dict[str, object]:
    unknown = set(plan) - {
        "application_targets",
        "id",
        "kind",
        "performance_binding",
        "plan_identity",
        "steps",
        "version",
    }
    if unknown:
        raise ValueError("setup Plan contains unsupported fields")
    identity = plan.get("plan_identity")
    if (
        not isinstance(identity, str)
        or len(identity) != 64
        or any(character not in "0123456789abcdef" for character in identity)
    ):
        raise ValueError("setup Plan identity must be an exact sha256 digest")
    raw_steps = plan.get("steps")
    binding = plan.get("performance_binding")
    if binding is not None and not isinstance(binding, Mapping):
        raise ValueError("setup Plan performance binding must be an object")
    if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, str | bytes):
        raise ValueError("setup Plan steps must be a sequence")
    steps: list[dict[str, object]] = []
    identities: set[str] = set()
    for raw_step in raw_steps:
        if not isinstance(raw_step, Mapping):
            raise ValueError("setup Plan steps must be objects")
        if not {"id", "fingerprint"}.issubset(raw_step) or set(raw_step) - {
            "id",
            "fingerprint",
            "state",
            "expected_result",
        }:
            raise ValueError("setup Plan steps have invalid fields")
        step_id = raw_step.get("id")
        fingerprint = raw_step.get("fingerprint")
        if not isinstance(step_id, str) or not step_id:
            raise ValueError("setup Plan step id must be nonempty")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError("setup Plan step fingerprint must be nonempty")
        if step_id in identities:
            raise ValueError("setup Plan step ids must be unique")
        state = raw_step.get("state")
        if state is not None and state not in {item.value for item in StepState}:
            raise ValueError("setup Plan step state is invalid")
        expected_result = raw_step.get("expected_result")
        if expected_result is not None and not _material_expectation_valid(
            step_id, expected_result
        ):
            raise ValueError("setup Plan step expected result is invalid")
        if (
            step_id not in {"runtime.install", "model.install"}
            and expected_result is not None
        ):
            raise ValueError("setup Plan step has an unexpected result contract")
        identities.add(step_id)
        normalized_step: dict[str, object] = {
            "id": step_id,
            "fingerprint": fingerprint,
        }
        if state is not None:
            normalized_step["state"] = state
        if expected_result is not None:
            normalized_step["expected_result"] = dict(expected_result)
        steps.append(normalized_step)
    if not steps:
        raise ValueError("setup Plan requires steps")
    raw_targets = plan.get("application_targets")
    if not isinstance(raw_targets, Sequence) or isinstance(raw_targets, str | bytes):
        raise ValueError("setup Plan application targets must be a sequence")
    targets = tuple(raw_targets)
    if any(
        not isinstance(target, str) or target not in {"codex", "hindsight"}
        for target in targets
    ) or len(set(targets)) != len(targets):
        raise ValueError("setup Plan application targets are invalid")
    if any(f"application.canary.{target}" not in identities for target in targets):
        raise ValueError("setup Plan is missing an application canary")
    normalized = {
        "plan_identity": identity,
        "steps": tuple(steps),
        "application_targets": targets,
    }
    if binding is not None:
        normalized["performance_binding"] = dict(binding)
    return normalized


def _plan_from_stored(plan: Mapping[str, object]) -> PlanAssessmentInput:
    raw_steps = plan["steps"]
    raw_targets = plan["application_targets"]
    assert isinstance(raw_steps, Sequence)
    assert isinstance(raw_targets, Sequence)
    return PlanAssessmentInput(
        steps=tuple(
            PlanStep(
                id=str(step["id"]),
                fingerprint=str(step["fingerprint"]),
                state=StepState(str(step.get("state", StepState.READY.value))),
                expected_result=(
                    dict(step["expected_result"])
                    if isinstance(step.get("expected_result"), Mapping)
                    else None
                ),
            )
            for step in raw_steps
            if isinstance(step, Mapping)
        ),
        application_targets=tuple(str(target) for target in raw_targets),
        performance_binding=(
            dict(plan["performance_binding"])
            if isinstance(plan.get("performance_binding"), Mapping)
            else None
        ),
    )


def _conservative_setup_outcome(*, malformed: bool) -> Mapping[str, object]:
    return {
        "completion": Completion.PARTIAL.value,
        "readiness": (
            Readiness.UNVERIFIED.value if malformed else Readiness.PENDING.value
        ),
        "application_target_readiness": {},
    }


def _inspection_next_actions(
    inspection: Mapping[str, object], target: str
) -> tuple[str, ...]:
    raw = inspection.get("next_actions")
    if isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
        actions = tuple(action for action in raw if isinstance(action, str) and action)
        if actions:
            return actions
    return (f"mastic application-target inspect {target}",)


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


def _performance_binding(resolved: ResolvedSetup) -> Mapping[str, object]:
    return {
        "selection_sha256": _performance_plan_fingerprint(resolved.selection),
        "application_versions": dict(PHASE1_APPLICATION_VERSIONS),
        "platform": resolved.preflight.platform,
        "machine": resolved.preflight.machine,
        "memory_bytes": resolved.preflight.memory_bytes,
        "macos_major": _macos_major(resolved.preflight.os_version),
        "service": resolved.selection.service_name,
    }


def _macos_major(version: str) -> int | None:
    major, _, _ = version.partition(".")
    if not major.isdecimal():
        return None
    return int(major)


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


def _boolean(parameters: Mapping[str, object], name: str) -> bool:
    value = parameters.get(name, False)
    if type(value) is not bool:
        raise ApplicationError("invalid_parameter", f"{name} must be a boolean")
    return value


def _json(value: object) -> str:
    return json.dumps(_plain(value), separators=(",", ":"), sort_keys=True)
