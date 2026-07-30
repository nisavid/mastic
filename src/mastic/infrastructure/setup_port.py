"""Operational-state adapters for application-owned setup coordination."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Protocol

from mastic.application.setup import Readiness, SetupEvidence, StepState
from mastic.application.setup_operation import (
    PHASE1_HOST_PERFORMANCE_PROFILE,
    EvidenceStore,
    SetupPlanStore,
    application_target_health_issue,
    application_target_observation_issue,
    combined_readiness,
    conservative_setup_outcome,
    durable_setup_outcome,
    evidence_kind,
    target_readiness,
    validated_plan,
)


class OperationOwner(Protocol):
    """One bounded physical owner used for outcome reconciliation."""

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]: ...


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
            for item in self._state.snapshot_history(evidence_kind(scope))
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
                "kind": evidence_kind(scope),
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
        normalized = validated_plan(plan)
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
        self._application_targets = application_targets

    def outcome(self) -> Mapping[str, object]:
        try:
            plan = self._plans.load()
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            return conservative_setup_outcome(malformed=True)
        if plan is None:
            return conservative_setup_outcome(malformed=False)
        try:
            normalized = validated_plan(plan)
            evidence = tuple(self._evidence.load("setup"))
            outcome = durable_setup_outcome(
                normalized, evidence, self._performance_profile
            )
            return self._reconcile_application_targets(normalized, outcome)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            return conservative_setup_outcome(malformed=True)

    def _reconcile_application_targets(
        self,
        plan: Mapping[str, object],
        outcome: Mapping[str, object],
    ) -> Mapping[str, object]:
        if self._application_targets is None:
            return outcome
        raw_targets = plan["application_targets"]
        assert isinstance(raw_targets, Sequence)
        issues: list[Mapping[str, object]] = []
        if not raw_targets:
            return {**outcome, "application_target_issues": ()}
        readiness = dict(target_readiness(outcome))
        for raw_target in raw_targets:
            target = str(raw_target)
            try:
                inspection = self._application_targets.execute(
                    "application-target.inspect",
                    {"application_target": target},
                )
            except Exception:
                readiness[target] = Readiness.UNVERIFIED.value
                issues.append(application_target_observation_issue(target))
                continue
            if not isinstance(inspection, Mapping):
                readiness[target] = Readiness.UNVERIFIED.value
                issues.append(application_target_observation_issue(target))
                continue
            state = inspection.get("state")
            if state == "healthy":
                continue
            if state in {
                "missing",
                "drifted",
                "incompatible",
                "malformed",
                "unmanaged",
            }:
                readiness[target] = Readiness.UNVERIFIED.value
                issues.append(
                    application_target_health_issue(target, state, inspection)
                )
            else:
                readiness[target] = Readiness.UNVERIFIED.value
                issues.append(application_target_observation_issue(target))
        return {
            **outcome,
            "readiness": combined_readiness(readiness).value,
            "application_target_readiness": readiness,
            "application_target_issues": tuple(issues),
        }
