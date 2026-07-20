"""Policy inputs for owner-preserving external-application lifecycle work."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from .canonical import canonical_fingerprint, canonical_timestamp


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _identity(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise ValueError(f"{field_name} must be a nonempty identity")
    return value


def _digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _aware(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class ReleaseTransitionKind(StrEnum):
    """Direction reported by the exact Installation Owner."""

    UPGRADE = "upgrade"
    SAME = "same"
    DOWNGRADE = "downgrade"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class UnattendedUpgradePolicy:
    """Durable rule that may request a new exact Plan assessment, not approval."""

    policy_identity: str
    application_identity: str
    owner_identity: str
    release_channel: str
    validation_profile_identity: str
    data_bearing: bool
    maximum_backup_age: timedelta

    def __post_init__(self) -> None:
        for field_name in (
            "policy_identity",
            "application_identity",
            "owner_identity",
            "release_channel",
            "validation_profile_identity",
        ):
            _identity(getattr(self, field_name), field_name.replace("_", " "))
        if not isinstance(self.data_bearing, bool):
            raise ValueError("data_bearing must be boolean")
        if not isinstance(
            self.maximum_backup_age, timedelta
        ) or self.maximum_backup_age <= timedelta(0):
            raise ValueError("maximum_backup_age must be positive")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "policy_identity": self.policy_identity,
                "application_identity": self.application_identity,
                "owner_identity": self.owner_identity,
                "release_channel": self.release_channel,
                "validation_profile_identity": self.validation_profile_identity,
                "data_bearing": self.data_bearing,
                "maximum_backup_age_microseconds": _duration_microseconds(
                    self.maximum_backup_age
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class UpgradeCandidate:
    """One exact observed installation and owner-resolved target transition."""

    application_identity: str
    installation_identity: str
    installation_observation_fingerprint: str
    owner_identity: str
    owner_installation_identity: str
    release_channel: str
    platform: str
    architecture: str
    source_release: str
    source_artifact_digest: str
    source_observed_at: datetime
    target_release: str
    target_artifact_digest: str
    current_resolution_fingerprint: str
    current_resolution_observed_at: datetime
    current_resolution_expires_at: datetime
    transition: ReleaseTransitionKind

    def __post_init__(self) -> None:
        for field_name in (
            "application_identity",
            "installation_identity",
            "owner_identity",
            "owner_installation_identity",
            "release_channel",
            "platform",
            "architecture",
            "source_release",
            "target_release",
        ):
            _identity(getattr(self, field_name), field_name.replace("_", " "))
        for field_name in (
            "installation_observation_fingerprint",
            "source_artifact_digest",
            "target_artifact_digest",
            "current_resolution_fingerprint",
        ):
            _digest(getattr(self, field_name), field_name.replace("_", " "))
        _aware(self.source_observed_at, "source observation time")
        _aware(
            self.current_resolution_observed_at,
            "current resolution observation time",
        )
        _aware(self.current_resolution_expires_at, "current resolution expiry")
        if not isinstance(self.transition, ReleaseTransitionKind):
            raise ValueError("transition must be owner-classified")

    def payload(self) -> dict[str, object]:
        return {
            "application_identity": self.application_identity,
            "installation_identity": self.installation_identity,
            "installation_observation_fingerprint": (
                self.installation_observation_fingerprint
            ),
            "owner_identity": self.owner_identity,
            "owner_installation_identity": self.owner_installation_identity,
            "release_channel": self.release_channel,
            "platform": self.platform,
            "architecture": self.architecture,
            "source_release": self.source_release,
            "source_artifact_digest": self.source_artifact_digest,
            "source_observed_at": canonical_timestamp(self.source_observed_at),
            "target_release": self.target_release,
            "target_artifact_digest": self.target_artifact_digest,
            "current_resolution_fingerprint": self.current_resolution_fingerprint,
            "current_resolution_observed_at": canonical_timestamp(
                self.current_resolution_observed_at
            ),
            "current_resolution_expires_at": canonical_timestamp(
                self.current_resolution_expires_at
            ),
            "transition": self.transition.value,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.payload())


@dataclass(frozen=True, slots=True)
class ValidatedBackup:
    """Fresh content-addressed backup validated before one data-bearing upgrade."""

    installation_identity: str
    source_observation_fingerprint: str
    candidate_fingerprint: str
    current_resolution_fingerprint: str
    backup_identity: str
    artifact_digest: str
    snapshot_fingerprint: str
    validated_at: datetime

    def __post_init__(self) -> None:
        _identity(self.installation_identity, "installation identity")
        _identity(self.backup_identity, "backup identity")
        _digest(
            self.source_observation_fingerprint,
            "source observation fingerprint",
        )
        _digest(self.candidate_fingerprint, "upgrade candidate fingerprint")
        _digest(
            self.current_resolution_fingerprint,
            "current resolution fingerprint",
        )
        _digest(self.artifact_digest, "backup artifact digest")
        _digest(self.snapshot_fingerprint, "backup snapshot fingerprint")
        _aware(self.validated_at, "backup validation time")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "installation_identity": self.installation_identity,
                "source_observation_fingerprint": (self.source_observation_fingerprint),
                "candidate_fingerprint": self.candidate_fingerprint,
                "current_resolution_fingerprint": (self.current_resolution_fingerprint),
                "backup_identity": self.backup_identity,
                "artifact_digest": self.artifact_digest,
                "snapshot_fingerprint": self.snapshot_fingerprint,
                "validated_at": canonical_timestamp(self.validated_at),
            }
        )


@dataclass(frozen=True, slots=True)
class IsolatedRestoreObservation:
    """Content-free outcome of restoring one exact backup in isolation."""

    candidate_fingerprint: str
    validated_backup_fingerprint: str
    restored_snapshot_fingerprint: str
    successful: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "candidate_fingerprint",
            "validated_backup_fingerprint",
            "restored_snapshot_fingerprint",
        ):
            _digest(getattr(self, field_name), field_name.replace("_", " "))
        if not isinstance(self.successful, bool):
            raise ValueError("restore outcome must be boolean")
        _aware(self.observed_at, "isolated restore observation time")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "candidate_fingerprint": self.candidate_fingerprint,
                "validated_backup_fingerprint": self.validated_backup_fingerprint,
                "restored_snapshot_fingerprint": self.restored_snapshot_fingerprint,
                "successful": self.successful,
                "observed_at": canonical_timestamp(self.observed_at),
            }
        )


@dataclass(frozen=True, slots=True)
class RepresentativeRecallObservation:
    """Content-free recall outcome against one isolated restored backup."""

    candidate_fingerprint: str
    validated_backup_fingerprint: str
    isolated_restore_observation_fingerprint: str
    validation_profile_identity: str
    successful: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        _identity(self.validation_profile_identity, "validation profile identity")
        for field_name in (
            "candidate_fingerprint",
            "validated_backup_fingerprint",
            "isolated_restore_observation_fingerprint",
        ):
            _digest(getattr(self, field_name), field_name.replace("_", " "))
        if not isinstance(self.successful, bool):
            raise ValueError("recall outcome must be boolean")
        _aware(self.observed_at, "representative recall observation time")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "candidate_fingerprint": self.candidate_fingerprint,
                "validated_backup_fingerprint": self.validated_backup_fingerprint,
                "isolated_restore_observation_fingerprint": (
                    self.isolated_restore_observation_fingerprint
                ),
                "validation_profile_identity": self.validation_profile_identity,
                "successful": self.successful,
                "observed_at": canonical_timestamp(self.observed_at),
            }
        )


@dataclass(frozen=True, slots=True)
class RecoveryQualification:
    """Isolated restore and representative recall proof for one exact candidate."""

    candidate_fingerprint: str
    validation_profile_identity: str
    validated_backup_fingerprint: str
    isolated_restore: IsolatedRestoreObservation
    representative_recall: RepresentativeRecallObservation
    qualified_at: datetime

    def __post_init__(self) -> None:
        _identity(self.validation_profile_identity, "validation profile identity")
        for field_name in (
            "candidate_fingerprint",
            "validated_backup_fingerprint",
        ):
            _digest(getattr(self, field_name), field_name.replace("_", " "))
        if not isinstance(self.isolated_restore, IsolatedRestoreObservation):
            raise ValueError("isolated restore observation is required")
        if not isinstance(
            self.representative_recall,
            RepresentativeRecallObservation,
        ):
            raise ValueError("representative recall observation is required")
        _aware(self.qualified_at, "recovery qualification time")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "candidate_fingerprint": self.candidate_fingerprint,
                "validation_profile_identity": self.validation_profile_identity,
                "validated_backup_fingerprint": self.validated_backup_fingerprint,
                "isolated_restore_observation_fingerprint": (
                    self.isolated_restore.fingerprint
                ),
                "representative_recall_observation_fingerprint": (
                    self.representative_recall.fingerprint
                ),
                "qualified_at": canonical_timestamp(self.qualified_at),
            }
        )


@dataclass(frozen=True, slots=True)
class ExactRemovalTarget:
    """One exact observed owner-controlled installation named for removal."""

    application_identity: str
    installation_identity: str
    owner_identity: str
    owner_installation_identity: str
    installation_observation_fingerprint: str

    def __post_init__(self) -> None:
        for field_name in (
            "application_identity",
            "installation_identity",
            "owner_identity",
            "owner_installation_identity",
        ):
            _identity(getattr(self, field_name), field_name.replace("_", " "))
        _digest(
            self.installation_observation_fingerprint,
            "installation observation fingerprint",
        )

    def payload(self) -> dict[str, str]:
        return {
            "application_identity": self.application_identity,
            "installation_identity": self.installation_identity,
            "owner_identity": self.owner_identity,
            "owner_installation_identity": self.owner_installation_identity,
            "installation_observation_fingerprint": (
                self.installation_observation_fingerprint
            ),
        }


@dataclass(frozen=True, slots=True)
class RemovalPlan:
    """Exact Plan required before an external application can be removed."""

    plan_identity: str
    targets: tuple[ExactRemovalTarget, ...]

    def __post_init__(self) -> None:
        _identity(self.plan_identity, "removal Plan identity")
        ordered = tuple(
            sorted(self.targets, key=lambda target: target.installation_identity)
        )
        identities = tuple(target.installation_identity for target in ordered)
        if len(set(identities)) != len(identities):
            raise ValueError("Removal Plan targets must be unique installations")
        if not ordered:
            raise ValueError("Removal Plan must name at least one exact installation")
        object.__setattr__(self, "targets", ordered)

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "plan_identity": self.plan_identity,
                "targets": [target.payload() for target in self.targets],
            }
        )


def _duration_microseconds(value: timedelta) -> int:
    return value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds
