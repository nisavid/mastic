"""Owner-bound identities and current-release evidence for external applications."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from .canonical import canonical_fingerprint, canonical_timestamp


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _required_identity(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} is required")
    if any(character.isspace() for character in value):
        raise ValueError(f"{field_name} cannot contain whitespace")
    return value


def _sha256(value: object, field_name: str) -> str:
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


class ReleaseIntentKind(StrEnum):
    """Whether an installation tracks its channel or selects one exact release."""

    CURRENT = "current"
    EXACT = "exact"


@dataclass(frozen=True, slots=True)
class ReleaseIntent:
    kind: ReleaseIntentKind
    channel: str
    exact_release: str | None = None

    def __post_init__(self) -> None:
        _required_identity(self.channel, "release channel")
        if self.kind is ReleaseIntentKind.CURRENT:
            if self.exact_release is not None:
                raise ValueError(
                    "current Release Intent cannot select an exact release"
                )
        elif self.kind is ReleaseIntentKind.EXACT:
            _required_identity(self.exact_release, "exact release")
        else:
            raise ValueError("unsupported Release Intent kind")

    @classmethod
    def current(cls, *, channel: str) -> ReleaseIntent:
        return cls(kind=ReleaseIntentKind.CURRENT, channel=channel)

    @classmethod
    def exact(cls, *, channel: str, release: str) -> ReleaseIntent:
        return cls(
            kind=ReleaseIntentKind.EXACT,
            channel=channel,
            exact_release=release,
        )


@dataclass(frozen=True, slots=True)
class ExternalApplicationInstallation:
    """One lifecycle unit selected with its native owner and release stream."""

    application_identity: str
    installation_identity: str
    owner_identity: str
    release_intent: ReleaseIntent
    platform: str
    architecture: str

    def __post_init__(self) -> None:
        _required_identity(self.application_identity, "application identity")
        _required_identity(self.installation_identity, "installation identity")
        _required_identity(self.owner_identity, "installation owner identity")
        _required_identity(self.platform, "platform")
        _required_identity(self.architecture, "architecture")


@dataclass(frozen=True, slots=True)
class InstallationObservation:
    """Observed State for one exact owner-controlled installation."""

    application_identity: str
    installation_identity: str
    owner_identity: str
    owner_installation_identity: str
    release_channel: str
    platform: str
    architecture: str
    installed_release: str | None
    installed_artifact_digest: str | None
    active_invocation: str | None
    reachable_invocations: tuple[str, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "application_identity",
            "installation_identity",
            "owner_identity",
            "owner_installation_identity",
            "release_channel",
            "platform",
            "architecture",
        ):
            _required_identity(getattr(self, field_name), field_name.replace("_", " "))
        if self.installed_release is not None:
            _required_identity(self.installed_release, "installed release")
        if self.installed_artifact_digest is not None:
            _sha256(self.installed_artifact_digest, "installed artifact digest")
        if (self.installed_release is None) != (self.installed_artifact_digest is None):
            raise ValueError(
                "installed release and artifact digest must be observed together"
            )
        reachable = tuple(sorted(set(self.reachable_invocations)))
        for invocation in reachable:
            if not isinstance(invocation, str) or not Path(invocation).is_absolute():
                raise ValueError("reachable invocations must be absolute paths")
        if self.active_invocation is not None:
            if (
                not isinstance(self.active_invocation, str)
                or not Path(self.active_invocation).is_absolute()
            ):
                raise ValueError("active invocation must be an absolute path")
            if self.active_invocation not in reachable:
                raise ValueError("active invocation must be reachable")
        object.__setattr__(self, "reachable_invocations", reachable)
        _aware(self.observed_at, "installation observation time")

    def payload(self) -> dict[str, object]:
        return {
            "application_identity": self.application_identity,
            "installation_identity": self.installation_identity,
            "owner_identity": self.owner_identity,
            "owner_installation_identity": self.owner_installation_identity,
            "release_channel": self.release_channel,
            "platform": self.platform,
            "architecture": self.architecture,
            "installed_release": self.installed_release,
            "installed_artifact_digest": self.installed_artifact_digest,
            "active_invocation": self.active_invocation,
            "reachable_invocations": list(self.reachable_invocations),
            "observed_at": canonical_timestamp(self.observed_at),
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.payload())


@dataclass(frozen=True, slots=True)
class AuthorityReleaseObservation:
    """One exact, source-attributed observation from a Release Authority."""

    exact_release: str
    artifact_coordinate: str
    artifact_digest: str
    authority_identity: str
    response_digest: str
    observed_at: datetime
    valid_until: datetime | None = None

    def __post_init__(self) -> None:
        _required_identity(self.exact_release, "exact release")
        _required_identity(self.artifact_coordinate, "artifact coordinate")
        _sha256(self.artifact_digest, "artifact digest")
        _required_identity(self.authority_identity, "release authority identity")
        _sha256(self.response_digest, "authority response digest")
        _aware(self.observed_at, "authority observation time")
        if self.valid_until is not None:
            _aware(self.valid_until, "authority validity time")
            if self.valid_until <= self.observed_at:
                raise ValueError("authority validity must end after observation")

    @property
    def stable_identity(self) -> tuple[object, ...]:
        """Fields that must remain identical across the materialization fence."""

        return (
            self.exact_release,
            self.artifact_coordinate,
            self.artifact_digest,
            self.authority_identity,
            self.response_digest,
            (
                canonical_timestamp(self.valid_until)
                if self.valid_until is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class CurrentReleaseResolution:
    """Direct online Evidence for one stable owner-bound current resolution."""

    installation_identity: str
    installation_observation_fingerprint: str
    owner_identity: str
    release_channel: str
    platform: str
    architecture: str
    exact_release: str
    artifact_coordinate: str
    artifact_digest: str
    authority_identity: str
    authority_response_digest: str
    observed_at: datetime
    expires_at: datetime
    resolver_policy_identity: str
    validation_profile_identity: str
    evidence_provenance: str = field(default="observed", init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "installation_identity",
            "owner_identity",
            "release_channel",
            "platform",
            "architecture",
            "exact_release",
            "artifact_coordinate",
            "authority_identity",
            "resolver_policy_identity",
            "validation_profile_identity",
            "evidence_provenance",
        ):
            _required_identity(getattr(self, field_name), field_name.replace("_", " "))
        _sha256(self.artifact_digest, "artifact digest")
        _sha256(self.authority_response_digest, "authority response digest")
        _sha256(
            self.installation_observation_fingerprint,
            "installation observation fingerprint",
        )
        _aware(self.observed_at, "observation time")
        _aware(self.expires_at, "expiry time")
        if self.expires_at <= self.observed_at:
            raise ValueError("Current Release Resolution must expire after observation")

    def canonical_payload(self) -> dict[str, object]:
        """Return the complete deterministic online observation payload."""

        return {
            "installation_identity": self.installation_identity,
            "installation_observation_fingerprint": (
                self.installation_observation_fingerprint
            ),
            "owner_identity": self.owner_identity,
            "release_channel": self.release_channel,
            "platform": self.platform,
            "architecture": self.architecture,
            "exact_release": self.exact_release,
            "artifact_coordinate": self.artifact_coordinate,
            "artifact_digest": self.artifact_digest,
            "authority_identity": self.authority_identity,
            "authority_response_digest": self.authority_response_digest,
            "observed_at": canonical_timestamp(self.observed_at),
            "expires_at": canonical_timestamp(self.expires_at),
            "resolver_policy_identity": self.resolver_policy_identity,
            "validation_profile_identity": self.validation_profile_identity,
            "evidence_provenance": self.evidence_provenance,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.canonical_payload())
