"""Repository-backed authorization for one exact owner-native upgrade."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Callable, Iterator, Mapping

from mastic.application.external_application_lifecycle import (
    AuthorizedOwnerUpgrade,
    OwnerUpgradeMaterialVerifier,
    OwnerUpgradePreview,
    VerifiedArtifactClosure,
)
from mastic.domain.canonical import canonical_fingerprint, canonical_json_bytes
from mastic.domain.external_applications import ExternalApplicationInstallation
from mastic.domain.planning_records import (
    CanonicalInstant,
    PlanAssessment,
    PlanDisposition,
    datetime_instant,
    parse_canonical_time,
)
from mastic.infrastructure.planning_record_repository import PlanningRecordRepository


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TrustedPlanningPolicy:
    """One composition-selected policy and its exact Selection record."""

    scope_identity: str
    purpose: str
    policy_selection: Mapping[str, object]
    policy: Mapping[str, object]
    _selection_bytes: bytes = field(init=False, repr=False)
    _policy_bytes: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        selection = dict(self.policy_selection)
        policy = dict(self.policy)
        selection_fingerprint = selection.get("fingerprint")
        policy_fingerprint = policy.get("fingerprint")
        if (
            not isinstance(selection_fingerprint, str)
            or not isinstance(policy_fingerprint, str)
            or canonical_fingerprint(
                {
                    name: value
                    for name, value in selection.items()
                    if name != "fingerprint"
                }
            )
            != selection_fingerprint
            or canonical_fingerprint(
                {name: value for name, value in policy.items() if name != "fingerprint"}
            )
            != policy_fingerprint
            or selection.get("scope_identity") != self.scope_identity
            or selection.get("purpose") != self.purpose
            or selection.get("policy_fingerprint") != policy_fingerprint
        ):
            raise ValueError("trusted Planning Policy selection is inconsistent")
        object.__setattr__(self, "_selection_bytes", canonical_json_bytes(selection))
        object.__setattr__(self, "_policy_bytes", canonical_json_bytes(policy))

    def accepts(self, assessment: PlanAssessment) -> bool:
        record = assessment.to_mapping()
        evaluation = record.get("evaluation")
        if not isinstance(evaluation, Mapping):
            return False
        return (
            canonical_json_bytes(evaluation.get("policy_selection"))
            == self._selection_bytes
            and canonical_json_bytes(evaluation.get("policy")) == self._policy_bytes
        )


class StaticPlanningPolicyRegistry:
    """Resolve trusted policies selected by production composition only."""

    def __init__(self, policies: tuple[TrustedPlanningPolicy, ...]) -> None:
        entries = {(item.scope_identity, item.purpose): item for item in policies}
        if len(entries) != len(policies):
            raise ValueError("trusted Planning Policies must be unique by context")
        self._policies = entries

    def accepts(self, assessment: PlanAssessment) -> bool:
        selected = self._policies.get((assessment.scope.identity, assessment.purpose))
        return selected is not None and selected.accepts(assessment)


class PlanningRecordOwnerUpgradeAuthorizationVerifier:
    """Resolve all untrusted references through current authoritative records."""

    def __init__(
        self,
        repository: PlanningRecordRepository,
        policy_registry: StaticPlanningPolicyRegistry,
        owner_material_verifier: OwnerUpgradeMaterialVerifier,
        *,
        maximum_assessment_age: timedelta,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(
            maximum_assessment_age, timedelta
        ) or maximum_assessment_age <= timedelta(0):
            raise ValueError("maximum Plan Assessment age must be positive")
        self._repository = repository
        self._policies = policy_registry
        self._owner_material_verifier = owner_material_verifier
        self._maximum_assessment_age = maximum_assessment_age
        self._clock = clock

    def verify(
        self,
        selected: ExternalApplicationInstallation,
        preview: OwnerUpgradePreview,
        authorization: AuthorizedOwnerUpgrade,
        artifact_closure: VerifiedArtifactClosure,
    ) -> bool:
        try:
            return self._verify(selected, preview, authorization, artifact_closure)
        except Exception as error:
            _log_authorization_failure("verification", error)
            return False

    @contextmanager
    def hold_authorization(
        self,
        selected: ExternalApplicationInstallation,
        preview: OwnerUpgradePreview,
        authorization: AuthorizedOwnerUpgrade,
        artifact_closure: VerifiedArtifactClosure,
    ) -> Iterator[bool]:
        """Keep the selected current Plan stable through one mutation attempt."""

        try:
            plan = self._repository.plan(authorization.plan_identity)
        except Exception as error:
            _log_authorization_failure("Plan lookup", error)
            plan = None
        if plan is None:
            yield False
            return
        with self._repository.hold_current(plan.scope.identity):
            yield self.verify(selected, preview, authorization, artifact_closure)

    def _verify(
        self,
        selected: ExternalApplicationInstallation,
        preview: OwnerUpgradePreview,
        authorization: AuthorizedOwnerUpgrade,
        artifact_closure: VerifiedArtifactClosure,
    ) -> bool:
        if (
            authorization.preview_fingerprint != preview.fingerprint
            or preview.plan_purpose != "reconciliation"
            or artifact_closure.fingerprint != preview.artifact_closure_fingerprint
            or artifact_closure.application_identity != selected.application_identity
            or artifact_closure.exact_release != preview.target_release
            or artifact_closure.artifact("primary").archive_digest
            != preview.target_artifact_digest
            or not self._owner_material_verifier.verify_authorization_material(
                selected, preview, artifact_closure
            )
        ):
            return False
        plan = self._repository.plan(authorization.plan_identity)
        approval = self._repository.approval(authorization.approval_identity)
        assessment = self._repository.assessment(authorization.assessment_identity)
        if plan is None or approval is None or assessment is None:
            return False
        pointer = self._repository.current(plan.scope.identity)
        if (
            pointer is None
            or pointer.plan_identity != plan.plan_identity
            or pointer.plan_purpose != preview.plan_purpose
            or pointer.assessment_identity != assessment.assessment_identity
            or plan.purpose != preview.plan_purpose
            or assessment.plan_identity != plan.plan_identity
            or assessment.purpose != plan.purpose
            or assessment.disposition is not PlanDisposition.ELIGIBLE
            or approval.approval_identity
            not in assessment.applicable_approval_identities
            or approval.plan_identity != plan.plan_identity
            or approval.plan_purpose != plan.purpose
            or approval.policy_fingerprint != assessment.policy_fingerprint
            or approval.evidence_set_fingerprint != assessment.evidence_set_fingerprint
            or not self._policies.accepts(assessment)
        ):
            return False
        now = datetime_instant(self._clock(), "authorization clock")
        created_at = _timestamp(plan.created_at)
        evaluated_at = _timestamp(assessment.evaluated_at)
        granted_at = _timestamp(approval.granted_at)
        valid_until = (
            None if approval.valid_until is None else _timestamp(approval.valid_until)
        )
        if (
            created_at > evaluated_at
            or evaluated_at > now
            or granted_at > evaluated_at
            or granted_at > now
            or now >= evaluated_at.plus(self._maximum_assessment_age)
            or (valid_until is not None and now >= valid_until)
        ):
            return False
        record = plan.to_mapping()
        targets = record.get("targets")
        if not isinstance(targets, list) or len(targets) != 1:
            return False
        target = targets[0]
        if not isinstance(target, Mapping):
            return False
        mutation = plan.owner_upgrade_mutation
        primary = artifact_closure.artifact("primary")
        return (
            target.get("target_id") == selected.installation_identity
            and target.get("subject_fingerprint") == selected.fingerprint
            and mutation.target_id == selected.installation_identity
            and mutation.plan_target_fingerprint
            == target.get("plan_target_fingerprint")
            and mutation.request.installation_identity == selected.installation_identity
            and mutation.request.release_artifact_identity == primary.fingerprint
            and mutation.request.source_state_fingerprint
            == preview.source_state_fingerprint
            and mutation.expected_current_fingerprint
            == preview.source_state_fingerprint
            and mutation.request.preview_fingerprint == preview.fingerprint
            and mutation.request.owner_action_fingerprint == preview.action.fingerprint
            and mutation.request.artifact_closure_fingerprint
            == artifact_closure.fingerprint
        )


def _timestamp(value: str) -> CanonicalInstant:
    return parse_canonical_time(value, "Planning Record timestamp")


def _log_authorization_failure(operation: str, error: Exception) -> None:
    _LOGGER.warning(
        "owner upgrade authorization %s failed with %s",
        operation,
        type(error).__name__,
    )
