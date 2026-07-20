"""Authoritative immutable storage for version-2 Planning Records."""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Iterator, Mapping, Protocol

from mastic.domain.planning_records import (
    CanonicalInstant,
    CurrentPlanPointer,
    Plan,
    PlanApproval,
    PlanAssessment,
    parse_canonical_time,
)
from mastic.infrastructure.state_store import (
    OperationalStateStore,
    SnapshotCompareError,
)


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PLAN_KIND = "mastic.plan"
_APPROVAL_KIND = "mastic.plan-approval"
_ASSESSMENT_KIND = "mastic.plan-assessment"
_POINTER_KIND = "mastic.current-plan-pointer"


class GrantReceiptVerifier(Protocol):
    """Verify one complete immutable Plan Approval without issuing receipts."""

    def verify(self, approval: PlanApproval) -> bool: ...


class PlanningRecordConflictError(RuntimeError):
    """The current Plan pointer no longer has the expected predecessor."""


class PlanningRecordRepository:
    """Store and resolve exact version-2 Planning Records."""

    def __init__(
        self,
        state: OperationalStateStore,
        receipt_verifier: GrantReceiptVerifier,
    ) -> None:
        self._state = state
        self._receipt_verifier = receipt_verifier

    def put_plan(self, record: Mapping[str, object]) -> Plan:
        """Validate and immutably store one exact Plan."""

        plan = Plan.from_mapping(record)
        self._put_immutable(_PLAN_KIND, plan.plan_identity, plan.to_mapping())
        return plan

    def plan(self, plan_identity: str) -> Plan | None:
        """Resolve one exact Plan identity."""

        identity = _identity(plan_identity, "Plan identity")
        envelope = self._state.snapshot(_PLAN_KIND, identity, version=identity)
        if envelope is None:
            return None
        record = _record_from_envelope(envelope, _PLAN_KIND, identity, identity)
        plan = Plan.from_mapping(record)
        if plan.plan_identity != identity:
            raise ValueError("stored Plan does not match its exact lookup identity")
        return plan

    def put_approval(self, record: Mapping[str, object]) -> PlanApproval:
        """Authenticate and immutably store one exact Plan Approval."""

        approval = PlanApproval.from_mapping(record)
        self._require_valid_receipt(approval)
        self._put_immutable(
            _APPROVAL_KIND, approval.approval_identity, approval.to_mapping()
        )
        return approval

    def approval(self, approval_identity: str) -> PlanApproval | None:
        """Resolve and reauthenticate one exact Plan Approval identity."""

        identity = _identity(approval_identity, "Plan Approval identity")
        envelope = self._state.snapshot(_APPROVAL_KIND, identity, version=identity)
        if envelope is None:
            return None
        record = _record_from_envelope(envelope, _APPROVAL_KIND, identity, identity)
        approval = PlanApproval.from_mapping(record)
        if approval.approval_identity != identity:
            raise ValueError("stored Plan Approval does not match its lookup identity")
        self._require_valid_receipt(approval)
        return approval

    def put_assessment(self, record: Mapping[str, object]) -> PlanAssessment:
        """Validate and immutably store one exact Plan Assessment."""

        assessment = PlanAssessment.from_mapping(record)
        self._validate_assessment_plan(assessment)
        self._put_immutable(
            _ASSESSMENT_KIND,
            assessment.assessment_identity,
            assessment.to_mapping(),
        )
        return assessment

    def assessment(self, assessment_identity: str) -> PlanAssessment | None:
        """Resolve one exact Plan Assessment identity."""

        identity = _identity(assessment_identity, "Plan Assessment identity")
        envelope = self._state.snapshot(_ASSESSMENT_KIND, identity, version=identity)
        if envelope is None:
            return None
        record = _record_from_envelope(envelope, _ASSESSMENT_KIND, identity, identity)
        assessment = PlanAssessment.from_mapping(record)
        if assessment.assessment_identity != identity:
            raise ValueError(
                "stored Plan Assessment does not match its lookup identity"
            )
        self._validate_assessment_plan(assessment)
        return assessment

    def compare_and_put_current(
        self, record: Mapping[str, object]
    ) -> CurrentPlanPointer:
        """Select one current Plan through an exact predecessor CAS."""

        pointer = CurrentPlanPointer.from_mapping(record)
        with self.hold_current(pointer.scope_identity):
            return self._compare_and_put_current(pointer)

    def _compare_and_put_current(
        self, pointer: CurrentPlanPointer
    ) -> CurrentPlanPointer:
        self._validate_pointer_relationships(pointer)
        current = self._load_current(pointer.scope_identity)
        expected_version: str | None
        if current is None:
            if pointer.pointer_version != 1:
                raise PlanningRecordConflictError(
                    "current Plan pointer has no expected predecessor"
                )
            expected_version = None
        else:
            if (
                pointer.scope_identity != current.scope_identity
                or pointer.pointer_version != current.pointer_version + 1
                or pointer.expected_predecessor_identity != current.pointer_identity
            ):
                raise PlanningRecordConflictError(
                    "current Plan pointer predecessor changed"
                )
            expected_version = _pointer_version(current)
        envelope = _pointer_envelope(pointer)
        try:
            self._state.compare_and_put_snapshot(
                envelope,
                expected_current_version=expected_version,
            )
        except SnapshotCompareError as error:
            raise PlanningRecordConflictError(
                "current Plan pointer predecessor changed"
            ) from error
        return pointer

    @contextmanager
    def hold_current(self, scope_identity: str) -> Iterator[None]:
        """Keep one scope's current Plan selection stable for a mutation."""

        identity = _identity(scope_identity, "scope identity")
        with self._state.resource_transition(_POINTER_KIND, identity):
            yield

    def current(self, scope_identity: str) -> CurrentPlanPointer | None:
        """Resolve and revalidate the current Plan selection for one scope."""

        pointer = self._load_current(scope_identity)
        if pointer is not None:
            self._validate_pointer_relationships(pointer)
        return pointer

    def _put_immutable(
        self, kind: str, identity: str, record: Mapping[str, object]
    ) -> None:
        self._state.put_snapshot(
            {
                "kind": kind,
                "id": identity,
                "version": identity,
                "record": dict(record),
            }
        )

    def _require_valid_receipt(self, approval: PlanApproval) -> None:
        try:
            verified = self._receipt_verifier.verify(approval)
        except Exception as error:
            raise ValueError("Plan Approval receipt could not be verified") from error
        if verified is not True:
            raise ValueError("Plan Approval receipt was not verified")

    def _load_current(self, scope_identity: str) -> CurrentPlanPointer | None:
        identity = _identity(scope_identity, "scope identity")
        envelope = self._state.snapshot(_POINTER_KIND, identity)
        if envelope is None:
            return None
        version = envelope.get("version")
        if not isinstance(version, str):
            raise ValueError("stored current Plan pointer version is malformed")
        record = _record_from_envelope(envelope, _POINTER_KIND, identity, version)
        pointer = CurrentPlanPointer.from_mapping(record)
        if pointer.scope_identity != identity or version != _pointer_version(pointer):
            raise ValueError("stored current Plan pointer envelope does not match")
        return pointer

    def _validate_pointer_relationships(self, pointer: CurrentPlanPointer) -> None:
        plan = self.plan(pointer.plan_identity)
        if plan is None:
            raise ValueError("current Plan pointer references a missing Plan")
        assessment = self.assessment(pointer.assessment_identity)
        if assessment is None:
            raise ValueError(
                "current Plan pointer references a missing Plan Assessment"
            )
        if (
            pointer.scope != plan.scope
            or pointer.plan_purpose != plan.purpose
            or assessment.plan_identity != plan.plan_identity
            or assessment.blueprint_identity != plan.blueprint_identity
            or assessment.scope != plan.scope
            or assessment.purpose != plan.purpose
            or assessment.target_ids != plan.target_ids
        ):
            raise ValueError(
                "current Plan pointer, Plan, and Plan Assessment do not agree"
            )
        self._validate_assessment_approvals(plan, assessment)

    def _validate_assessment_plan(self, assessment: PlanAssessment) -> None:
        plan = self.plan(assessment.plan_identity)
        if plan is None:
            raise ValueError("Plan Assessment references a missing Plan")
        if (
            assessment.blueprint_identity != plan.blueprint_identity
            or assessment.scope != plan.scope
            or assessment.purpose != plan.purpose
            or assessment.target_ids != plan.target_ids
        ):
            raise ValueError("Plan Assessment does not exactly project its Plan")
        plan_record = plan.to_mapping()
        assessment_record = assessment.to_mapping()
        steps = plan_record["required_steps"]
        targets = plan_record["targets"]
        operational = assessment_record["operational_assessment"]
        if (
            not isinstance(steps, list)
            or not isinstance(targets, list)
            or not isinstance(operational, Mapping)
        ):
            raise ValueError("Plan assessment relationship is malformed")
        completion = operational.get("completion")
        operational_targets = operational.get("targets")
        if not isinstance(completion, Mapping) or not isinstance(
            operational_targets, list
        ):
            raise ValueError("Plan Operational Assessment is malformed")
        step_ids = tuple(str(item["step_id"]) for item in steps)
        required_ids = completion.get("required_step_ids")
        if required_ids != list(step_ids):
            raise ValueError("Plan Completion does not exactly project required steps")
        expected_targets = tuple(
            (
                str(item["target_id"]),
                str(item["target_kind"]),
                str(item["subject_fingerprint"]),
                str(item["plan_target_fingerprint"]),
            )
            for item in targets
        )
        assessed_targets = tuple(
            (
                str(item["target_id"]),
                str(item["target_kind"]),
                str(item["subject_fingerprint"]),
                str(item["plan_target_fingerprint"]),
            )
            for item in operational_targets
        )
        if assessed_targets != expected_targets:
            raise ValueError("operational targets do not exactly project Plan targets")
        self._validate_assessment_approvals(plan, assessment)

    def _validate_assessment_approvals(
        self, plan: Plan, assessment: PlanAssessment
    ) -> None:
        assessment_record = assessment.to_mapping()
        policy_assessment = assessment_record["policy_assessment"]
        if not isinstance(policy_assessment, Mapping):
            raise ValueError("Plan Assessment policy projection is malformed")
        claims_value = policy_assessment["applicable_claim_ids"]
        if not isinstance(claims_value, list):
            raise ValueError("Plan Assessment applicable Claims are malformed")
        applicable_claim_ids = tuple(str(item) for item in claims_value)
        evaluations_value = policy_assessment["rule_evaluations"]
        if not isinstance(evaluations_value, list):
            raise ValueError("Plan Assessment rule evaluations are malformed")
        evaluation = assessment_record["evaluation"]
        if not isinstance(evaluation, Mapping):
            raise ValueError("Plan Assessment evaluation is malformed")
        policy = evaluation["policy"]
        if not isinstance(policy, Mapping):
            raise ValueError("Plan Assessment policy is malformed")
        rules_value = policy["rules"]
        if not isinstance(rules_value, list):
            raise ValueError("Plan Assessment policy rules are malformed")
        policy_rules: dict[str, tuple[str, bool]] = {}
        for value in rules_value:
            if not isinstance(value, Mapping):
                raise ValueError("Plan Assessment policy rule is malformed")
            policy_rules[str(value["rule_id"])] = (
                str(value["result"]),
                bool(value["overridable"]),
            )
        authority = policy["approval_authority"]
        if not isinstance(authority, Mapping):
            raise ValueError("Plan Assessment Approval authority is malformed")
        bound_rules: dict[str, tuple[set[str], set[str]]] = {}
        for value in evaluations_value:
            if not isinstance(value, Mapping):
                raise ValueError("Plan Assessment rule evaluation is malformed")
            approval_identity = value.get("approval_identity")
            if isinstance(approval_identity, str):
                rule_id = str(value["rule_id"])
                ordinary, overrides = bound_rules.setdefault(
                    approval_identity, (set(), set())
                )
                binding = policy_rules.get(rule_id)
                if binding is None:
                    raise ValueError(
                        "Plan Assessment rule evaluation references an unknown policy rule"
                    )
                base_result, overridable = binding
                if base_result == "approval_required":
                    ordinary.add(rule_id)
                elif base_result == "blocked" and overridable:
                    overrides.add(rule_id)
        applicable = set(assessment.applicable_approval_identities)
        for identity in assessment.approval_identities:
            approval = self.approval(identity)
            if approval is None:
                raise ValueError("Plan Assessment references a missing Plan Approval")
            declared_applicable = identity in applicable
            actually_applicable = _approval_is_applicable(
                approval,
                plan,
                assessment,
                applicable_claim_ids,
                policy_rules,
                authority,
            )
            if declared_applicable and not actually_applicable:
                raise ValueError(
                    "Plan Assessment marks an inapplicable Plan Approval applicable"
                )
            if actually_applicable and not declared_applicable:
                raise ValueError(
                    "Plan Assessment marks an applicable Plan Approval inapplicable"
                )
            if declared_applicable and not _approval_matches_rule_bindings(
                approval, bound_rules.get(identity, (set(), set()))
            ):
                raise ValueError(
                    "Plan Approval rule bindings do not match Assessment evaluations"
                )


def _approval_is_applicable(
    approval: PlanApproval,
    plan: Plan,
    assessment: PlanAssessment,
    applicable_claim_ids: tuple[str, ...],
    policy_rules: Mapping[str, tuple[str, bool]],
    authority: Mapping[str, object],
) -> bool:
    evaluated_at = _timestamp(assessment.evaluated_at)
    valid_until = (
        None if approval.valid_until is None else _timestamp(approval.valid_until)
    )
    authorized_subjects = authority["subject_fingerprints"]
    authorized_rules = authority["rule_ids"]
    authorized_overrides = authority["override_rule_ids"]
    if not all(
        isinstance(value, list)
        for value in (authorized_subjects, authorized_rules, authorized_overrides)
    ):
        raise ValueError("Plan Assessment Approval authority is malformed")
    return (
        approval.plan_identity == plan.plan_identity
        and approval.plan_purpose == plan.purpose
        and approval.policy_fingerprint == assessment.policy_fingerprint
        and approval.evidence_set_fingerprint == assessment.evidence_set_fingerprint
        and approval.applicable_claim_ids == applicable_claim_ids
        and _timestamp(approval.granted_at) <= evaluated_at
        and (valid_until is None or evaluated_at < valid_until)
        and approval.authorization_subject.fingerprint in authorized_subjects
        and all(
            policy_rules.get(rule_id) == ("approval_required", False)
            for rule_id in approval.rule_ids
        )
        and all(
            policy_rules.get(rule_id) == ("blocked", True)
            for rule_id in approval.override_rule_ids
        )
        and set(approval.rule_ids).issubset(authorized_rules)
        and set(approval.override_rule_ids).issubset(authorized_overrides)
    )


def _approval_matches_rule_bindings(
    approval: PlanApproval,
    bound_rules: tuple[set[str], set[str]],
) -> bool:
    ordinary_bound, override_bound = bound_rules
    return (
        set(approval.rule_ids) == ordinary_bound
        and set(approval.override_rule_ids) == override_bound
    )


def _timestamp(value: str) -> CanonicalInstant:
    return parse_canonical_time(value, "Planning Record timestamp")


def _pointer_version(pointer: CurrentPlanPointer) -> str:
    return f"{pointer.pointer_version}:{pointer.pointer_identity}"


def _pointer_envelope(pointer: CurrentPlanPointer) -> dict[str, object]:
    return {
        "kind": _POINTER_KIND,
        "id": pointer.scope_identity,
        "version": _pointer_version(pointer),
        "record": pointer.to_mapping(),
    }


def _record_from_envelope(
    envelope: Mapping[str, object],
    kind: str,
    identity: str,
    version: str,
) -> Mapping[str, object]:
    if (
        set(envelope) != {"kind", "id", "version", "record"}
        or envelope.get("kind") != kind
        or envelope.get("id") != identity
        or envelope.get("version") != version
        or not isinstance(envelope.get("record"), Mapping)
    ):
        raise ValueError("stored Planning Record envelope is malformed")
    return envelope["record"]  # type: ignore[return-value]


def _identity(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value
