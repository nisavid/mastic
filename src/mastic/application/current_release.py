"""Resolve current Release Intent without installing or selecting an owner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Callable, Protocol

from mastic.domain.external_applications import (
    AuthorityReleaseObservation,
    CurrentReleaseResolution,
    ExternalApplicationInstallation,
    InstallationObservation,
    ReleaseIntentKind,
)


@dataclass(frozen=True, slots=True)
class ArtifactMaterialization:
    coordinate: str
    digest: str


@dataclass(frozen=True, slots=True)
class CurrentReleaseAuthorityQuery:
    application_identity: str
    installation_identity: str
    installation_observation_fingerprint: str
    owner_identity: str
    owner_installation_identity: str
    release_channel: str
    platform: str
    architecture: str


class ReleaseAuthority(Protocol):
    """Resolve one owner-native release channel without mutating it."""

    def resolve_current(
        self, query: CurrentReleaseAuthorityQuery
    ) -> AuthorityReleaseObservation: ...


class ArtifactMaterializer(Protocol):
    """Materialize exact artifact evidence without installing the application."""

    def materialize(
        self, release: AuthorityReleaseObservation
    ) -> ArtifactMaterialization: ...


class CurrentReleaseResolutionFailure(StrEnum):
    NOT_CURRENT_INTENT = "not_current_intent"
    INSTALLATION_OBSERVATION_MISMATCH = "installation_observation_mismatch"
    OWNER_MISMATCH = "owner_mismatch"
    CHANNEL_MISMATCH = "channel_mismatch"
    PLATFORM_MISMATCH = "platform_mismatch"
    ARCHITECTURE_MISMATCH = "architecture_mismatch"
    AUTHORITY_UNAVAILABLE = "authority_unavailable"
    AUTHORITY_INVALID_RESPONSE = "authority_invalid_response"
    AUTHORITY_UNSTABLE = "authority_unstable"
    ARTIFACT_UNAVAILABLE = "artifact_unavailable"
    ARTIFACT_MISMATCH = "artifact_mismatch"


class CurrentReleaseResolutionError(RuntimeError):
    """A stable Current Release Resolution could not be produced."""

    def __init__(
        self,
        reason_code: str | CurrentReleaseResolutionFailure,
        installation_identity: str,
        detail: str,
    ) -> None:
        super().__init__(detail)
        self.reason_code = str(reason_code)
        self.installation_identity = installation_identity


class ReleaseAuthorityUnavailableError(RuntimeError):
    """The selected Release Authority could not be queried."""


class ReleaseAuthorityInvalidResponseError(RuntimeError):
    """The selected Release Authority returned inadmissible data."""


class ReleaseArtifactUnavailableError(RuntimeError):
    """The exact authority-selected artifact could not be materialized."""


def resolve_current_release(
    installation: ExternalApplicationInstallation,
    observation: InstallationObservation,
    *,
    authority: ReleaseAuthority,
    materializer: ArtifactMaterializer,
    maximum_age: timedelta,
    resolver_policy_identity: str,
    validation_profile_identity: str,
    max_attempts: int = 3,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CurrentReleaseResolution:
    """Resolve, materialize, and re-resolve one current owner-native release."""

    if installation.release_intent.kind is not ReleaseIntentKind.CURRENT:
        raise CurrentReleaseResolutionError(
            CurrentReleaseResolutionFailure.NOT_CURRENT_INTENT,
            installation.installation_identity,
            "exact Release Intent cannot produce a Currency Claim",
        )
    _validate_observation_binding(installation, observation)
    _validate_identity(resolver_policy_identity, "resolver policy identity")
    _validate_identity(validation_profile_identity, "validation profile identity")
    if maximum_age <= timedelta(0):
        raise ValueError("maximum_age must be positive")
    if type(max_attempts) is not int or max_attempts <= 0:
        raise ValueError("max_attempts must be a positive integer")

    for _attempt in range(max_attempts):
        before = _read_authority_checked(installation, observation, authority)
        try:
            artifact = materializer.materialize(before)
        except ReleaseArtifactUnavailableError as error:
            raise CurrentReleaseResolutionError(
                CurrentReleaseResolutionFailure.ARTIFACT_UNAVAILABLE,
                installation.installation_identity,
                "authority-selected artifact is unavailable",
            ) from error
        after = _read_authority_checked(installation, observation, authority)
        if before.stable_identity != after.stable_identity:
            continue
        if after.observed_at < before.observed_at:
            raise CurrentReleaseResolutionError(
                CurrentReleaseResolutionFailure.AUTHORITY_INVALID_RESPONSE,
                installation.installation_identity,
                "release authority observation time moved backward",
            )
        resolved_at = clock()
        if resolved_at.tzinfo is None or resolved_at.utcoffset() is None:
            raise ValueError("current release clock must be timezone-aware")
        if after.observed_at > resolved_at:
            raise CurrentReleaseResolutionError(
                CurrentReleaseResolutionFailure.AUTHORITY_INVALID_RESPONSE,
                installation.installation_identity,
                "release authority observation time is in the future",
            )
        if (
            artifact.coordinate != after.artifact_coordinate
            or artifact.digest != after.artifact_digest
        ):
            raise CurrentReleaseResolutionError(
                CurrentReleaseResolutionFailure.ARTIFACT_MISMATCH,
                installation.installation_identity,
                "materialized artifact does not match the stable authority result",
            )
        expires_at = after.observed_at + maximum_age
        if after.valid_until is not None:
            expires_at = min(expires_at, after.valid_until)
        if expires_at <= resolved_at:
            raise CurrentReleaseResolutionError(
                CurrentReleaseResolutionFailure.AUTHORITY_INVALID_RESPONSE,
                installation.installation_identity,
                "release authority observation is too stale",
            )
        return CurrentReleaseResolution(
            installation_identity=installation.installation_identity,
            installation_observation_fingerprint=observation.fingerprint,
            owner_identity=installation.owner_identity,
            release_channel=installation.release_intent.channel,
            platform=installation.platform,
            architecture=installation.architecture,
            exact_release=after.exact_release,
            artifact_coordinate=after.artifact_coordinate,
            artifact_digest=after.artifact_digest,
            authority_identity=after.authority_identity,
            authority_response_digest=after.response_digest,
            observed_at=after.observed_at,
            expires_at=expires_at,
            resolver_policy_identity=resolver_policy_identity,
            validation_profile_identity=validation_profile_identity,
        )

    raise CurrentReleaseResolutionError(
        CurrentReleaseResolutionFailure.AUTHORITY_UNSTABLE,
        installation.installation_identity,
        "release authority changed across every materialization fence",
    )


def _read_authority(
    installation: ExternalApplicationInstallation,
    observation: InstallationObservation,
    authority: ReleaseAuthority,
) -> AuthorityReleaseObservation:
    return authority.resolve_current(
        CurrentReleaseAuthorityQuery(
            application_identity=installation.application_identity,
            installation_identity=installation.installation_identity,
            installation_observation_fingerprint=observation.fingerprint,
            owner_identity=installation.owner_identity,
            owner_installation_identity=observation.owner_installation_identity,
            release_channel=installation.release_intent.channel,
            platform=installation.platform,
            architecture=installation.architecture,
        )
    )


def _read_authority_checked(
    installation: ExternalApplicationInstallation,
    observation: InstallationObservation,
    authority: ReleaseAuthority,
) -> AuthorityReleaseObservation:
    try:
        return _read_authority(installation, observation, authority)
    except ReleaseAuthorityUnavailableError as error:
        raise CurrentReleaseResolutionError(
            CurrentReleaseResolutionFailure.AUTHORITY_UNAVAILABLE,
            installation.installation_identity,
            "release authority is unavailable",
        ) from error
    except ReleaseAuthorityInvalidResponseError as error:
        raise CurrentReleaseResolutionError(
            CurrentReleaseResolutionFailure.AUTHORITY_INVALID_RESPONSE,
            installation.installation_identity,
            "release authority response is invalid",
        ) from error


def _validate_observation_binding(
    installation: ExternalApplicationInstallation,
    observation: InstallationObservation,
) -> None:
    comparisons: tuple[tuple[CurrentReleaseResolutionFailure, object, object], ...] = (
        (
            CurrentReleaseResolutionFailure.INSTALLATION_OBSERVATION_MISMATCH,
            installation.application_identity,
            observation.application_identity,
        ),
        (
            CurrentReleaseResolutionFailure.INSTALLATION_OBSERVATION_MISMATCH,
            installation.installation_identity,
            observation.installation_identity,
        ),
        (
            CurrentReleaseResolutionFailure.OWNER_MISMATCH,
            installation.owner_identity,
            observation.owner_identity,
        ),
        (
            CurrentReleaseResolutionFailure.CHANNEL_MISMATCH,
            installation.release_intent.channel,
            observation.release_channel,
        ),
        (
            CurrentReleaseResolutionFailure.PLATFORM_MISMATCH,
            installation.platform,
            observation.platform,
        ),
        (
            CurrentReleaseResolutionFailure.ARCHITECTURE_MISMATCH,
            installation.architecture,
            observation.architecture,
        ),
    )
    for reason_code, selected, observed in comparisons:
        if selected != observed:
            raise CurrentReleaseResolutionError(
                reason_code,
                installation.installation_identity,
                "selected installation does not match its exact observation",
            )


def _validate_identity(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise ValueError(f"{field_name} must be a nonempty identity")
    return value
