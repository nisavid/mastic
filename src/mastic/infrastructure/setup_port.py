"""Concrete supported-v1 setup and removal operation orchestration.

The port keeps planning side-effect free and crosses owner boundaries only
after the exact rendered plan has been confirmed.  Its collaborators are
operation ports so composition can bind real runtime, model, desired-state,
Application Configuration Target, Supervisor, and Gateway implementations without hiding work here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Protocol
from urllib.parse import urlsplit

from mastic.application.dispatch import ApplicationError
from mastic.application.setup import (
    ExactSetupSelection,
    PlanExecutionError,
    PlanStep,
    RemovalInventory,
    RemovalPlan,
    SetupEvidence,
    SetupPlan,
    SetupPlanner,
    SetupPreflight,
    SetupRequest,
    StepState,
)


class OperationOwner(Protocol):
    """One bounded owner of named product operations."""

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]: ...


class EvidenceStore(Protocol):
    """Durable, content-free completion evidence for resumable plans."""

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
    """Preview and apply one exact, resumable Plan."""

    def __init__(
        self,
        planner: SetupPlanner,
        *,
        preflight: PreflightProvider,
        runtime: OperationOwner,
        model: OperationOwner,
        config: OperationOwner,
        application_targets: OperationOwner,
        supervisor: OperationOwner,
        verifier: OperationOwner,
        evidence: EvidenceStore,
        removal_inventory: RemovalInventoryProvider,
    ) -> None:
        self._planner = planner
        self._preflight = preflight
        self._runtime = runtime
        self._model = model
        self._config = config
        self._application_targets = application_targets
        self._supervisor = supervisor
        self._verifier = verifier
        self._evidence = evidence
        self._removal_inventory = removal_inventory

    def preview(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        plan = self._setup_plan(parameters)
        return self._setup_preview(plan)

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        if operation != "setup":
            raise ApplicationError(
                "operation_unavailable", f"{operation} is not a setup operation"
            )
        plan = self._setup_plan(parameters)
        preview = self._setup_preview(plan)
        if parameters.get("confirmed") is not True or not parameters.get(
            "preview_fingerprint"
        ):
            return preview
        self._assert_preview_identity(parameters, preview)
        blocked = next(
            (step for step in plan.steps if step.state is StepState.BLOCKED), None
        )
        if blocked is not None:
            code = "offline_blocked" if plan.offline else "setup_blocked"
            raise ApplicationError(
                code,
                f"setup is blocked at {blocked.id}: {blocked.reason}",
                next_actions=("connect this Mac and retry", "mastic setup --help"),
            )

        prior = tuple(self._evidence.load("setup"))
        material = _restore_material(prior)
        results: dict[str, object] = {}

        def execute_step(step: PlanStep) -> SetupEvidence:
            result = self._execute_setup_step(plan, step, material)
            results[step.id] = result
            material[step.id] = result
            return SetupEvidence.complete(
                step,
                _json({"result": _content_free_result(step.id, result)}),
            )

        try:
            execution = self._planner.apply(
                plan,
                execute_step,
                evidence=prior,
                record=lambda item: self._evidence.record("setup", item),
            )
        except PlanExecutionError as error:
            raise ApplicationError(
                "setup_interrupted",
                str(error),
                next_actions=(
                    "rerun the same exact setup preview to resume",
                    "mastic operation list",
                ),
            ) from error
        return {
            **preview,
            "state": "complete",
            "complete": execution.complete,
            "completion": "complete" if execution.complete else "partial",
            "readiness": "unverified",
            "application_target_readiness": {
                target: "unverified" for target in plan.selection.application_targets
            },
            "results": _plain(results),
            "evidence": [_plain(item) for item in execution.evidence],
        }

    def preview_removal(self) -> Mapping[str, object]:
        return self._removal_preview(
            self._planner.plan_removal(self._removal_inventory())
        )

    def remove(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        plan = self._planner.plan_removal(self._removal_inventory())
        preview = self._removal_preview(plan)
        if parameters.get("confirmed") is not True or not parameters.get(
            "preview_fingerprint"
        ):
            return preview
        self._assert_preview_identity(parameters, preview)
        prior = tuple(self._evidence.load("removal"))
        results: dict[str, object] = {}

        def execute_step(step: PlanStep) -> SetupEvidence:
            result = self._execute_removal_step(step)
            results[step.id] = result
            return SetupEvidence.complete(step, _json({"result": result}))

        try:
            execution = self._planner.apply_removal(
                plan,
                execute_step,
                evidence=prior,
                record=self._record_removal_evidence,
            )
        except PlanExecutionError as error:
            raise ApplicationError(
                "removal_interrupted",
                str(error),
                next_actions=("review the removal plan and resume",),
            ) from error
        return {
            **preview,
            "state": "complete",
            "complete": execution.complete,
            "results": _plain(results),
            "evidence": [_plain(item) for item in execution.evidence],
        }

    def _setup_plan(self, parameters: Mapping[str, object]) -> SetupPlan:
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
            if profile == "exact":
                missing = _missing_exact_selection(parameters)
                if missing:
                    raise ValueError(
                        "exact setup requires an exact selection: " + ", ".join(missing)
                    )
                selection = _selection(parameters, self._planner.exact_template)
                explicit_selection = True
            else:
                baseline = self._planner.plan(
                    facts,
                    SetupRequest(capacity_profile=capacity_name),
                    evidence=prior,
                )
                if capacity_name is None and baseline.capacity_profile is not None:
                    capacity_name = baseline.capacity_profile.name
                selection = _selection(parameters, baseline.selection)
                explicit_selection = _has_selection(parameters)
            request = SetupRequest(
                selection=selection if explicit_selection else None,
                capacity_profile=capacity_name,
                noninteractive=bool(parameters.get("noninteractive", False)),
                confirmed=parameters.get("confirmed") is True,
            )
            return self._planner.plan(facts, request, evidence=prior)
        except ValueError as error:
            raise ApplicationError("invalid_setup", str(error)) from error

    def _setup_preview(self, plan: SetupPlan) -> Mapping[str, object]:
        preview = self._planner.preview(plan)
        identity = _preview_identity(
            plan.steps,
            {
                "profile": plan.profile_name,
                "selection": _selection_value(plan.selection),
                "offline": plan.offline,
            },
        )
        return {
            "state": "review_required",
            "profile": preview.profile_name,
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
                "sampling_profiles": _plain(preview.sampling_profiles),
                "context_window": preview.context_window,
            },
            "preflight": _plain(plan.preflight),
            "steps": [_plain(step) for step in preview.steps],
            "offline_note": preview.offline_note,
        }

    def _execute_setup_step(
        self,
        plan: SetupPlan,
        step: PlanStep,
        material: Mapping[str, object],
    ) -> Mapping[str, object]:
        selection = plan.selection
        if step.id == "preflight":
            return {"validated": True, **_plain(plan.preflight)}
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
                    "offline": plan.offline,
                    "confirmed": True,
                },
            )
            _validate_model_result(selection, result)
            if selection.trust_grants:
                runtime = _material_result(material, "runtime.install")
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
            runtime = _material_result(material, "runtime.install")
            _material_result(material, "model.install")
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
                        "sampling_profiles": selection.sampling_profiles,
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
        if step.id in {"gateway.contract.codex", "gateway.contract.hindsight"}:
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
                or response.get("text") != "mastic gateway contract ok"
            ):
                raise RuntimeError(
                    f"the {target} managed Gateway contract did not return the exact response"
                )
            return result
        if step.id == "verify.request":
            result = self._verifier.execute("verify.request", step.inputs)
            if result.get("ok") is not True or result.get("text") != "mastic ready":
                raise RuntimeError(
                    "the first Gateway request did not return the exact contract response"
                )
            return result
        raise RuntimeError(f"unsupported setup step: {step.id}")

    def _removal_preview(self, plan: RemovalPlan) -> Mapping[str, object]:
        identity = _preview_identity(
            plan.steps,
            {
                "references": plan.references,
                "freed_bytes_estimate": plan.freed_bytes_estimate,
                "retained_paths": plan.retained_paths,
                "retained_bytes_estimate": plan.retained_bytes_estimate,
                "retained_settings": plan.retained_settings,
            },
        )
        return {
            "state": "review_required",
            "confirmation_required": True,
            "preview_fingerprint": identity,
            "steps": [_plain(step) for step in plan.steps],
            "references": _plain(plan.references),
            "freed_bytes_estimate": plan.freed_bytes_estimate,
            "retained_paths": list(plan.retained_paths),
            "retained_bytes_estimate": plan.retained_bytes_estimate,
            "retained_settings": list(plan.retained_settings),
        }

    def _execute_removal_step(self, step: PlanStep) -> Mapping[str, object]:
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
                "sampling_profiles",
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
    sampling = overrides.get("sampling_profiles", baseline.sampling_profiles)
    if not isinstance(sampling, Mapping):
        raise ApplicationError("invalid_setup", "sampling_profiles must be an object")
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
        sampling_profiles=sampling,  # type: ignore[arg-type]
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
            "sampling_profiles",
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


def _material_result(
    material: Mapping[str, object], step_id: str
) -> Mapping[str, object]:
    result = material.get(step_id)
    if not isinstance(result, Mapping):
        raise RuntimeError(
            f"matching {step_id} evidence lacks resumable material; rerun that step"
        )
    return result


def _restore_material(evidence: Sequence[SetupEvidence]) -> dict[str, object]:
    restored = {}
    for item in evidence:
        if item.state is not StepState.COMPLETE or not item.detail:
            continue
        try:
            payload = json.loads(item.detail)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping) and isinstance(payload.get("result"), Mapping):
            restored[item.step_id] = dict(payload["result"])
    return restored


def _content_free_result(
    step_id: str, result: Mapping[str, object]
) -> Mapping[str, object]:
    if step_id.startswith("gateway.contract."):
        response = result.get("response")
        if not isinstance(response, Mapping):
            return {"profile": result.get("profile"), "ok": False}
        text = str(response.get("text", ""))
        return {
            "profile": result.get("profile"),
            "ok": response.get("ok") is True,
            "response_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    if step_id != "verify.request":
        return result
    text = str(result.get("text", ""))
    return {
        "ok": result.get("ok") is True,
        "response_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def _preview_identity(steps: Sequence[PlanStep], extra: Mapping[str, object]) -> str:
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
        "sampling_profiles": selection.sampling_profiles,
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
