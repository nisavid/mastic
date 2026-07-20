"""Assess durable upgrade policy without granting or performing mutation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from mastic.domain.application_lifecycle import (
    RecoveryQualification,
    ReleaseTransitionKind,
    UnattendedUpgradePolicy,
    UpgradeCandidate,
    ValidatedBackup,
)
from mastic.domain.canonical import canonical_fingerprint, canonical_timestamp
from mastic.domain.external_applications import (
    CurrentReleaseResolution,
    ExternalApplicationInstallation,
    InstallationObservation,
    ReleaseIntentKind,
)


class UpgradePolicyAssessmentDisposition(StrEnum):
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"


class UpgradePolicyAssessmentFailure(StrEnum):
    POLICY_SUBJECT_MISMATCH = "policy_subject_mismatch"
    TRANSITION_NOT_UPGRADE = "transition_not_upgrade"
    CURRENT_RESOLUTION_EXPIRED = "current_resolution_expired"
    CURRENT_RESOLUTION_TIME_INVALID = "current_resolution_time_invalid"
    RECOVERY_REQUIRED = "recovery_required"
    RECOVERY_NOT_APPLICABLE = "recovery_not_applicable"
    RECOVERY_CANDIDATE_MISMATCH = "recovery_candidate_mismatch"
    RECOVERY_PROFILE_MISMATCH = "recovery_profile_mismatch"
    BACKUP_MISMATCH = "backup_mismatch"
    BACKUP_STALE = "backup_stale"
    RESTORE_MISMATCH = "restore_mismatch"
    RESTORE_FAILED = "restore_failed"
    RECALL_MISMATCH = "recall_mismatch"
    RECALL_FAILED = "recall_failed"
    RECOVERY_TIME_INVALID = "recovery_time_invalid"


@dataclass(frozen=True, slots=True)
class UpgradePolicyAssessment:
    """Policy result that still requires a new exact Plan and Approval."""

    disposition: UpgradePolicyAssessmentDisposition
    policy_identity: str
    policy_fingerprint: str
    candidate_fingerprint: str
    assessed_at: datetime
    reason_codes: tuple[str, ...]
    validated_backup_fingerprint: str | None = None
    recovery_qualification_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if self.assessed_at.tzinfo is None or self.assessed_at.utcoffset() is None:
            raise ValueError("assessment time must be timezone-aware")
        has_backup = self.validated_backup_fingerprint is not None
        has_recovery = self.recovery_qualification_fingerprint is not None
        if has_backup != has_recovery:
            raise ValueError("backup and recovery fingerprints must appear together")
        if self.disposition is UpgradePolicyAssessmentDisposition.APPROVAL_REQUIRED:
            if self.reason_codes:
                raise ValueError("approval-required assessment cannot have failures")
        elif not self.reason_codes:
            raise ValueError("blocked assessment must explain its failures")
        if has_recovery and (
            self.disposition is not UpgradePolicyAssessmentDisposition.APPROVAL_REQUIRED
        ):
            raise ValueError("blocked assessment cannot retain accepted recovery")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "disposition": self.disposition.value,
                "policy_identity": self.policy_identity,
                "policy_fingerprint": self.policy_fingerprint,
                "candidate_fingerprint": self.candidate_fingerprint,
                "assessed_at": canonical_timestamp(self.assessed_at),
                "reason_codes": list(self.reason_codes),
                "validated_backup_fingerprint": (self.validated_backup_fingerprint),
                "recovery_qualification_fingerprint": (
                    self.recovery_qualification_fingerprint
                ),
            }
        )


def build_upgrade_candidate(
    installation: ExternalApplicationInstallation,
    observation: InstallationObservation,
    resolution: CurrentReleaseResolution,
    *,
    transition: ReleaseTransitionKind,
) -> UpgradeCandidate:
    """Bind selected, observed, resolved, and owner-classified upgrade state."""

    if installation.release_intent.kind is not ReleaseIntentKind.CURRENT:
        raise ValueError("upgrade candidate requires Current Release Intent")
    comparisons = (
        (installation.application_identity, observation.application_identity),
        (installation.installation_identity, observation.installation_identity),
        (installation.owner_identity, observation.owner_identity),
        (installation.release_intent.channel, observation.release_channel),
        (installation.platform, observation.platform),
        (installation.architecture, observation.architecture),
        (installation.installation_identity, resolution.installation_identity),
        (installation.owner_identity, resolution.owner_identity),
        (installation.release_intent.channel, resolution.release_channel),
        (installation.platform, resolution.platform),
        (installation.architecture, resolution.architecture),
        (observation.fingerprint, resolution.installation_observation_fingerprint),
    )
    if any(selected != observed for selected, observed in comparisons):
        raise ValueError("upgrade candidate inputs do not bind the same observation")
    if (
        observation.installed_release is None
        or observation.installed_artifact_digest is None
    ):
        raise ValueError("upgrade candidate requires an observed installed release")
    return UpgradeCandidate(
        application_identity=installation.application_identity,
        installation_identity=installation.installation_identity,
        installation_observation_fingerprint=observation.fingerprint,
        owner_identity=installation.owner_identity,
        owner_installation_identity=observation.owner_installation_identity,
        release_channel=installation.release_intent.channel,
        platform=installation.platform,
        architecture=installation.architecture,
        source_release=observation.installed_release,
        source_artifact_digest=observation.installed_artifact_digest,
        source_observed_at=observation.observed_at,
        target_release=resolution.exact_release,
        target_artifact_digest=resolution.artifact_digest,
        current_resolution_fingerprint=resolution.fingerprint,
        current_resolution_observed_at=resolution.observed_at,
        current_resolution_expires_at=resolution.expires_at,
        transition=transition,
    )


def assess_unattended_upgrade(
    policy: UnattendedUpgradePolicy,
    candidate: UpgradeCandidate,
    *,
    now: datetime,
    recovery: RecoveryQualification | None = None,
    backup: ValidatedBackup | None = None,
) -> UpgradePolicyAssessment:
    """Assess whether policy may request a successor exact Plan and Approval."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("assessment time must be timezone-aware")
    failures: list[UpgradePolicyAssessmentFailure] = []
    if (
        policy.application_identity != candidate.application_identity
        or policy.owner_identity != candidate.owner_identity
        or policy.release_channel != candidate.release_channel
    ):
        failures.append(UpgradePolicyAssessmentFailure.POLICY_SUBJECT_MISMATCH)
    if (
        candidate.transition is not ReleaseTransitionKind.UPGRADE
        or candidate.source_release == candidate.target_release
    ):
        failures.append(UpgradePolicyAssessmentFailure.TRANSITION_NOT_UPGRADE)
    if now >= candidate.current_resolution_expires_at:
        failures.append(UpgradePolicyAssessmentFailure.CURRENT_RESOLUTION_EXPIRED)
    if now < candidate.current_resolution_observed_at:
        failures.append(UpgradePolicyAssessmentFailure.CURRENT_RESOLUTION_TIME_INVALID)

    if policy.data_bearing:
        failures.extend(_assess_recovery(policy, candidate, now, recovery, backup))
    elif recovery is not None or backup is not None:
        failures.append(UpgradePolicyAssessmentFailure.RECOVERY_NOT_APPLICABLE)

    reason_codes = tuple(dict.fromkeys(str(failure) for failure in failures))
    accepted_recovery = policy.data_bearing and not reason_codes
    return UpgradePolicyAssessment(
        disposition=(
            UpgradePolicyAssessmentDisposition.BLOCKED
            if reason_codes
            else UpgradePolicyAssessmentDisposition.APPROVAL_REQUIRED
        ),
        policy_identity=policy.policy_identity,
        policy_fingerprint=policy.fingerprint,
        candidate_fingerprint=candidate.fingerprint,
        assessed_at=now,
        reason_codes=reason_codes,
        validated_backup_fingerprint=(
            backup.fingerprint if accepted_recovery and backup is not None else None
        ),
        recovery_qualification_fingerprint=(
            recovery.fingerprint if accepted_recovery and recovery is not None else None
        ),
    )


def _assess_recovery(
    policy: UnattendedUpgradePolicy,
    candidate: UpgradeCandidate,
    now: datetime,
    recovery: RecoveryQualification | None,
    backup: ValidatedBackup | None,
) -> list[UpgradePolicyAssessmentFailure]:
    if recovery is None or backup is None:
        return [UpgradePolicyAssessmentFailure.RECOVERY_REQUIRED]
    failures: list[UpgradePolicyAssessmentFailure] = []
    if recovery.candidate_fingerprint != candidate.fingerprint:
        failures.append(UpgradePolicyAssessmentFailure.RECOVERY_CANDIDATE_MISMATCH)
    if recovery.validation_profile_identity != policy.validation_profile_identity:
        failures.append(UpgradePolicyAssessmentFailure.RECOVERY_PROFILE_MISMATCH)
    if (
        backup.installation_identity != candidate.installation_identity
        or backup.source_observation_fingerprint
        != candidate.installation_observation_fingerprint
        or backup.candidate_fingerprint != candidate.fingerprint
        or backup.current_resolution_fingerprint
        != candidate.current_resolution_fingerprint
        or recovery.validated_backup_fingerprint != backup.fingerprint
    ):
        failures.append(UpgradePolicyAssessmentFailure.BACKUP_MISMATCH)
    if (
        backup.validated_at < candidate.current_resolution_observed_at
        or now - backup.validated_at > policy.maximum_backup_age
    ):
        failures.append(UpgradePolicyAssessmentFailure.BACKUP_STALE)
    restore = recovery.isolated_restore
    recall = recovery.representative_recall
    if (
        restore.candidate_fingerprint != candidate.fingerprint
        or restore.validated_backup_fingerprint != backup.fingerprint
        or restore.restored_snapshot_fingerprint != backup.snapshot_fingerprint
    ):
        failures.append(UpgradePolicyAssessmentFailure.RESTORE_MISMATCH)
    if not restore.successful:
        failures.append(UpgradePolicyAssessmentFailure.RESTORE_FAILED)
    if (
        recall.candidate_fingerprint != candidate.fingerprint
        or recall.validated_backup_fingerprint != backup.fingerprint
        or recall.isolated_restore_observation_fingerprint != restore.fingerprint
        or recall.validation_profile_identity != policy.validation_profile_identity
    ):
        failures.append(UpgradePolicyAssessmentFailure.RECALL_MISMATCH)
    if not recall.successful:
        failures.append(UpgradePolicyAssessmentFailure.RECALL_FAILED)
    if (
        restore.observed_at < backup.validated_at
        or recall.observed_at < restore.observed_at
        or recovery.qualified_at < recall.observed_at
        or recovery.qualified_at > now
        or backup.validated_at > now
    ):
        failures.append(UpgradePolicyAssessmentFailure.RECOVERY_TIME_INVALID)
    return failures
