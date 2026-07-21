"""Build exact authenticated Planning Records for owner-native upgrades."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from mastic.application.external_application_lifecycle import (
    AuthorizedOwnerUpgrade,
    OwnerUpgradePreview,
    VerifiedArtifactClosure,
)
from mastic.domain.application_lifecycle import ReleaseTransitionKind
from mastic.domain.canonical import canonical_fingerprint, canonical_timestamp
from mastic.domain.external_applications import ExternalApplicationInstallation
from mastic.infrastructure.planning_owner_upgrade_authorization import (
    TrustedPlanningPolicy,
)
from mastic.infrastructure.planning_record_authority import (
    LocalGrantReceiptIssuer,
    PlanApprovalDraft,
)
from mastic.infrastructure.planning_record_repository import PlanningRecordRepository


_NPM_RELEASE = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?\Z"
)
_PURPOSE = "reconciliation"
_RULE_ID = "owner-upgrade-review"
_EVIDENCE_SET = canonical_fingerprint([])


def classify_release_transition(
    source_release: str, target_release: str
) -> ReleaseTransitionKind:
    """Classify exact npm releases without ever defaulting ambiguity to upgrade."""

    if source_release == target_release and _NPM_RELEASE.fullmatch(source_release):
        return ReleaseTransitionKind.SAME
    source = _npm_release_parts(source_release)
    target = _npm_release_parts(target_release)
    if source is None or target is None:
        return ReleaseTransitionKind.UNKNOWN
    source_core, source_prerelease = source
    target_core, target_prerelease = target
    if source_core != target_core:
        return (
            ReleaseTransitionKind.UPGRADE
            if source_core < target_core
            else ReleaseTransitionKind.DOWNGRADE
        )
    if source_prerelease is not None and target_prerelease is None:
        return ReleaseTransitionKind.UPGRADE
    if source_prerelease is None and target_prerelease is not None:
        return ReleaseTransitionKind.DOWNGRADE
    if source_prerelease == target_prerelease:
        return ReleaseTransitionKind.SAME
    if source_prerelease is not None or target_prerelease is not None:
        return ReleaseTransitionKind.UNKNOWN
    return ReleaseTransitionKind.SAME


def authorize_owner_reconciliation(
    selected: ExternalApplicationInstallation,
    preview: OwnerUpgradePreview,
    artifact_closure: VerifiedArtifactClosure,
    *,
    repository: PlanningRecordRepository,
    issuer: LocalGrantReceiptIssuer,
    uid: int,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[AuthorizedOwnerUpgrade, TrustedPlanningPolicy]:
    """Persist one exact Plan, authenticated Approval, Assessment, and pointer."""

    if type(uid) is not int or uid < 0:
        raise ValueError("owner reconciliation requires a nonnegative local UID")
    if (
        selected.application_identity != preview.application_identity
        or selected.installation_identity != preview.installation_identity
        or selected.owner_identity != preview.owner_identity
        or selected.release_intent.channel != preview.release_channel
        or artifact_closure.fingerprint != preview.artifact_closure_fingerprint
        or artifact_closure.exact_release != preview.target_release
        or classify_release_transition(preview.source_release, preview.target_release)
        is not ReleaseTransitionKind.UPGRADE
    ):
        raise ValueError(
            "owner reconciliation is not an exact owner-preserving upgrade"
        )

    evaluated_at = clock()
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("owner reconciliation clock must be timezone-aware")
    scope = _scope(uid, selected)
    policy = _policy(uid)
    selection = _policy_selection(uid, scope, policy)
    trusted = trusted_owner_reconciliation_policy(uid, selected)
    plan_record = _plan(selected, preview, artifact_closure, scope, evaluated_at)
    plan = repository.put_plan(plan_record)
    approval = issuer.issue(
        PlanApprovalDraft(
            plan_identity=plan.plan_identity,
            plan_purpose=_PURPOSE,
            policy_fingerprint=str(policy["fingerprint"]),
            evidence_set_fingerprint=_EVIDENCE_SET,
            applicable_claim_ids=(),
            rule_ids=(_RULE_ID,),
            override_rule_ids=(),
            valid_for=timedelta(hours=1),
        )
    )
    repository.put_approval(approval.to_mapping())
    assessment_record = _assessment(
        plan.to_mapping(), approval.to_mapping(), selection, policy, evaluated_at
    )
    assessment = repository.put_assessment(assessment_record)
    current = repository.current(plan.scope.identity)
    pointer_version = 1 if current is None else current.pointer_version + 1
    pointer = _identified(
        {
            "schema_version": 2,
            "kind": "mastic.current-plan-pointer",
            "scope": scope,
            "scope_identity": plan.scope.identity,
            "pointer_version": pointer_version,
            "expected_predecessor_identity": (
                None if current is None else current.pointer_identity
            ),
            "plan_identity": plan.plan_identity,
            "plan_purpose": _PURPOSE,
            "assessment_identity": assessment.assessment_identity,
            "updated_at": canonical_timestamp(evaluated_at),
        },
        "pointer_identity",
    )
    repository.compare_and_put_current(pointer)
    return (
        AuthorizedOwnerUpgrade(
            plan_identity=plan.plan_identity,
            approval_identity=approval.approval_identity,
            assessment_identity=assessment.assessment_identity,
            preview_fingerprint=preview.fingerprint,
        ),
        trusted,
    )


def trusted_owner_reconciliation_policy(
    uid: int, selected: ExternalApplicationInstallation
) -> TrustedPlanningPolicy:
    """Return the exact policy admitted by production composition."""

    scope = _scope(uid, selected)
    policy = _policy(uid)
    return TrustedPlanningPolicy(
        scope_identity=_scope_identity(scope),
        purpose=_PURPOSE,
        policy_selection=_policy_selection(uid, scope, policy),
        policy=policy,
    )


def _npm_release_parts(value: str) -> tuple[tuple[int, int, int], str | None] | None:
    match = _NPM_RELEASE.fullmatch(value)
    if match is None:
        return None
    return (
        (int(match.group(1)), int(match.group(2)), int(match.group(3))),
        match.group(4),
    )


def _scope(uid: int, selected: ExternalApplicationInstallation) -> dict[str, str]:
    identity = f"user:{uid}/default"
    return {
        "kind": "declared_scope",
        "id": identity,
        "fingerprint": canonical_fingerprint(
            {"scope": identity, "installation": selected.fingerprint}
        ),
    }


def _scope_identity(scope: Mapping[str, str]) -> str:
    return canonical_fingerprint({"kind": scope["kind"], "id": scope["id"]})


def _policy(uid: int) -> dict[str, object]:
    subject = {"kind": "local_user", "id": str(uid)}
    subject_fingerprint = canonical_fingerprint(subject)
    value: dict[str, object] = {
        "id": "codex-current-owner-upgrade",
        "version": 1,
        "input_requirements": [],
        "rules": [
            {
                "rule_id": _RULE_ID,
                "result": "approval_required",
                "overridable": False,
            }
        ],
        "approval_authority": {
            "subject_fingerprints": [subject_fingerprint],
            "rule_ids": [_RULE_ID],
            "override_rule_ids": [],
        },
        "candidate_selection": {
            "rule_id": "prefer-policy-ranked-eligible",
            "version": 1,
        },
    }
    value["fingerprint"] = canonical_fingerprint(value)
    return value


def _policy_selection(
    uid: int, scope: Mapping[str, str], policy: Mapping[str, object]
) -> dict[str, object]:
    value: dict[str, object] = {
        "kind": "policy_selection",
        "id": f"user:{uid}/default:{_PURPOSE}",
        "scope_identity": _scope_identity(scope),
        "purpose": _PURPOSE,
        "policy_fingerprint": policy["fingerprint"],
        "authority_ref": {
            "kind": "local_policy_authority",
            "id": f"user:{uid}",
            "fingerprint": canonical_fingerprint(
                {"authority": "mastic-production", "uid": uid}
            ),
        },
    }
    value["fingerprint"] = canonical_fingerprint(value)
    return value


def _plan(
    selected: ExternalApplicationInstallation,
    preview: OwnerUpgradePreview,
    closure: VerifiedArtifactClosure,
    scope: Mapping[str, str],
    created_at: datetime,
) -> dict[str, object]:
    target: dict[str, object] = {
        "target_id": selected.installation_identity,
        "target_kind": "external_application_installation",
        "subject_fingerprint": selected.fingerprint,
        "lifecycle_applicability": "applicable",
        "expected_lifecycle_state": "active",
        "operational_contract_ref": {
            "kind": "operational_contract",
            "id": "codex-owner-native-upgrade",
            "fingerprint": canonical_fingerprint(
                {"contract": "codex-owner-native-upgrade", "version": 1}
            ),
        },
    }
    target["plan_target_fingerprint"] = canonical_fingerprint(
        {"purpose": _PURPOSE, "target": target}
    )
    step: dict[str, object] = {
        "step_id": "application.codex.upgrade",
        "target_id": selected.installation_identity,
        "plan_target_fingerprint": target["plan_target_fingerprint"],
        "execution_kind": "mutation",
        "mutation_ids": ["application.codex.owner-upgrade"],
        "depends_on": [],
        "skip_rule_id": None,
        "reuse_rule_id": "exact-purpose-step-material",
    }
    step["step_fingerprint"] = canonical_fingerprint(
        {"purpose": _PURPOSE, "step": step}
    )
    primary = closure.artifact("primary")
    value: dict[str, object] = {
        "schema_version": 2,
        "kind": "mastic.plan",
        "blueprint_identity": canonical_fingerprint(
            {
                "blueprint": "codex-current-owner-upgrade",
                "version": 1,
                "selected": selected.fingerprint,
            }
        ),
        "scope": dict(scope),
        "purpose": _PURPOSE,
        "created_at": canonical_timestamp(created_at),
        "targets": [target],
        "required_steps": [step],
        "proposed_declarations": [],
        "mutations": [
            {
                "mutation_id": "application.codex.owner-upgrade",
                "step_id": step["step_id"],
                "target_id": selected.installation_identity,
                "plan_target_fingerprint": target["plan_target_fingerprint"],
                "capability_port": "external_application_installation_lifecycle",
                "operation": "upgrade",
                "request": {
                    "installation_identity": selected.installation_identity,
                    "release_artifact_identity": primary.fingerprint,
                    "source_state_fingerprint": preview.source_state_fingerprint,
                    "preview_fingerprint": preview.fingerprint,
                    "owner_action_fingerprint": preview.action.fingerprint,
                    "artifact_closure_fingerprint": closure.fingerprint,
                },
                "expected_current": {"fingerprint": preview.source_state_fingerprint},
                "recovery": {
                    "mode": "restore",
                    "capability_port": ("external_application_installation_lifecycle"),
                    "operation": "restore",
                    "request": {
                        "installation_identity": selected.installation_identity,
                        "release_artifact_identity": canonical_fingerprint(
                            {
                                "source_release": preview.source_release,
                                "source_observation": (
                                    preview.source_observation_fingerprint
                                ),
                            }
                        ),
                    },
                },
            }
        ],
    }
    return _identified(value, "plan_identity")


def _assessment(
    plan: Mapping[str, object],
    approval: Mapping[str, object],
    selection: Mapping[str, object],
    policy: Mapping[str, object],
    evaluated_at: datetime,
) -> dict[str, object]:
    target = dict(plan["targets"][0])  # type: ignore[index]
    step = dict(plan["required_steps"][0])  # type: ignore[index]
    target_id = str(target["target_id"])
    issues = sorted(
        (
            _identified(
                {
                    "category": "observation",
                    "code": code,
                    "scope": {"kind": "target", "id": target_id},
                    "message": message,
                    "evidence_ids": [],
                    "next_actions": [action],
                },
                "issue_identity",
            )
            for code, message, action in (
                (
                    "lifecycle_not_observed",
                    "Target lifecycle has not been observed.",
                    "inspect target lifecycle",
                ),
                (
                    "condition_not_observed",
                    "Target condition has not been observed.",
                    "run the target canary",
                ),
            )
        ),
        key=lambda item: str(item["issue_identity"]),
    )
    lifecycle_issue = next(
        item for item in issues if item["code"] == "lifecycle_not_observed"
    )
    condition_issue = next(
        item for item in issues if item["code"] == "condition_not_observed"
    )
    approval_identity = str(approval["approval_identity"])
    value: dict[str, object] = {
        "schema_version": 2,
        "kind": "mastic.plan-assessment",
        "plan": {
            "plan_identity": plan["plan_identity"],
            "blueprint_identity": plan["blueprint_identity"],
            "scope": plan["scope"],
            "purpose": _PURPOSE,
            "target_ids": [target_id],
        },
        "evaluation": {
            "evaluated_at": canonical_timestamp(evaluated_at),
            "policy_selection": dict(selection),
            "policy": dict(policy),
            "evidence_set_fingerprint": _EVIDENCE_SET,
        },
        "evidence_ids": [],
        "policy_inputs": {
            "fingerprint": canonical_fingerprint(
                {
                    "claim_ids": [],
                    "claim_conflict_ids": [],
                    "evidence_ids": [],
                    "discovery_evidence_ids": [],
                }
            ),
            "claim_ids": [],
            "claim_conflict_ids": [],
            "evidence_ids": [],
            "discovery_evidence_ids": [],
        },
        "claims": [],
        "claim_qualifications": [],
        "claim_applicability": [],
        "claim_conflicts": [],
        "positions": {"support": [], "permission": [], "searches": []},
        "approvals": [{"identity": approval_identity, "kind": "mastic.plan-approval"}],
        "policy_assessment": {
            "disposition": "eligible",
            "applicable_claim_ids": [],
            "claim_conflict_ids": [],
            "rule_evaluations": [
                {
                    "rule_id": _RULE_ID,
                    "result": "satisfied",
                    "claim_ids": [],
                    "claim_conflict_ids": [],
                    "evidence_ids": [],
                    "approval_identity": approval_identity,
                }
            ],
            "approval_evaluation": {
                "requirement": "evaluated",
                "evaluations": [
                    {
                        "approval_identity": approval_identity,
                        "value": "applicable",
                    }
                ],
            },
        },
        "operational_assessment": {
            "completion": {
                "value": "partial",
                "required_step_ids": [step["step_id"]],
                "step_satisfactions": [],
            },
            "targets": [
                {
                    "target_id": target_id,
                    "target_kind": target["target_kind"],
                    "subject_fingerprint": target["subject_fingerprint"],
                    "plan_target_fingerprint": target["plan_target_fingerprint"],
                    "completion": "partial",
                    "lifecycle": {
                        "observation": "not_observed",
                        "issue_ids": [lifecycle_issue["issue_identity"]],
                    },
                    "operational_condition": {
                        "observation": "not_observed",
                        "issue_ids": [condition_issue["issue_identity"]],
                    },
                    "issue_ids": sorted(str(item["issue_identity"]) for item in issues),
                }
            ],
            "summary": {
                "by_completion": {"partial": [target_id], "complete": []},
                "by_lifecycle_state": {
                    "absent": [],
                    "present": [],
                    "transitioning": [],
                    "active": [],
                },
                "by_operational_condition": {
                    "functional": [],
                    "degraded": [],
                    "nonfunctional": [],
                },
                "lifecycle_not_observed": [target_id],
                "lifecycle_not_applicable": [],
                "condition_not_observed": [target_id],
                "condition_not_applicable": [],
            },
        },
        "issues": issues,
    }
    return _identified(value, "assessment_identity")


def _identified(value: Mapping[str, object], field: str) -> dict[str, object]:
    result = dict(value)
    result[field] = canonical_fingerprint(result)
    return result
