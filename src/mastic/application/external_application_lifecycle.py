"""Apply one exact owner-native application upgrade behind fresh evidence fences."""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable, Iterator, Protocol

from mastic.application.application_upgrade_policy import (
    UpgradePolicyAssessment,
    UpgradePolicyAssessmentDisposition,
)
from mastic.application.current_release import CurrentReleaseResolutionError
from mastic.domain.application_lifecycle import UpgradeCandidate
from mastic.domain.canonical import (
    canonical_fingerprint,
    require_artifact_digest as _artifact_digest,
    require_aware as _aware,
    require_identity as _identity,
    require_sha256 as _sha256,
)
from mastic.domain.external_applications import (
    CurrentReleaseResolution,
    ExternalApplicationInstallation,
    InstallationObservation,
)


class InstallationDiscoveryError(RuntimeError):
    """Declared operational failure from an installation discovery port."""


class OwnerUpgradeCommandError(RuntimeError):
    """Declared content-free failure from an owner mutation port."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class OwnerUpgradeNotAttemptedFailure(StrEnum):
    OWNER_ACTION_INVALID = "owner_action_invalid"
    OWNER_RUNTIME_INVALID = "owner_runtime_invalid"
    ARTIFACT_CONFIG_PREPARE_FAILED = "artifact_config_prepare_failed"
    ARTIFACT_CACHE_PREPARE_FAILED = "artifact_cache_prepare_failed"
    ARTIFACT_PREPARATION_FAILED = "artifact_preparation_failed"
    ARTIFACT_DIRECTORY_NOT_PRIVATE = "artifact_directory_not_private"
    STAGED_ARCHIVE_CHANGED = "staged_archive_changed"
    STAGED_PAYLOAD_CHANGED = "staged_payload_changed"
    STAGED_ARTIFACT_CHANGED = "staged_artifact_changed"
    EXPECTED_CURRENT_UNAVAILABLE = "expected_current_unavailable"
    EXPECTED_CURRENT_CHANGED = "expected_current_changed"


class OwnerUpgradeNotAttemptedError(OwnerUpgradeCommandError):
    """Owner port rejected expected-current before spawning the mutation."""

    def __init__(self, reason_code: str | OwnerUpgradeNotAttemptedFailure) -> None:
        reason = OwnerUpgradeNotAttemptedFailure(reason_code)
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class VerifiedArtifact:
    """One retained, verified archive and its canonical installed payload."""

    role: str
    package_identity: str
    exact_release: str
    coordinate: str
    archive_digest: str
    installed_payload_digest: str
    staged_path: Path

    def __post_init__(self) -> None:
        for field_name in (
            "role",
            "package_identity",
            "exact_release",
            "coordinate",
        ):
            _identity(getattr(self, field_name), field_name.replace("_", " "))
        _artifact_digest(self.archive_digest, "archive digest")
        _sha256(self.installed_payload_digest, "installed payload digest")
        if not isinstance(self.staged_path, Path) or not self.staged_path.is_absolute():
            raise ValueError("verified artifact staged path must be absolute")

    def canonical_payload(self) -> dict[str, str]:
        return {
            "role": self.role,
            "package_identity": self.package_identity,
            "exact_release": self.exact_release,
            "coordinate": self.coordinate,
            "archive_digest": self.archive_digest,
            "installed_payload_digest": self.installed_payload_digest,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class VerifiedArtifactClosure:
    """Complete retained artifact closure required by one exact installation."""

    application_identity: str
    exact_release: str
    artifacts: tuple[VerifiedArtifact, ...]
    staging_directory: Path
    cache_directory: Path

    def __post_init__(self) -> None:
        _identity(self.application_identity, "application identity")
        _identity(self.exact_release, "exact release")
        if (
            not isinstance(self.staging_directory, Path)
            or not self.staging_directory.is_absolute()
            or not isinstance(self.cache_directory, Path)
            or not self.cache_directory.is_absolute()
        ):
            raise ValueError("verified artifact closure directories must be absolute")
        ordered = tuple(sorted(self.artifacts, key=lambda artifact: artifact.role))
        if not ordered or len({artifact.role for artifact in ordered}) != len(ordered):
            raise ValueError("verified artifact closure requires unique artifact roles")
        if any(not isinstance(artifact, VerifiedArtifact) for artifact in ordered):
            raise ValueError("verified artifact closure contains an invalid artifact")
        if any(
            artifact.staged_path.parent != self.staging_directory
            for artifact in ordered
        ):
            raise ValueError(
                "verified artifacts must be direct staging-directory files"
            )
        if len({artifact.staged_path for artifact in ordered}) != len(ordered):
            raise ValueError("verified artifacts must use unique staged paths")
        if not self.cache_directory.is_relative_to(self.staging_directory):
            raise ValueError("artifact cache must reside in the staging directory")
        object.__setattr__(self, "artifacts", ordered)

    def artifact(self, role: str) -> VerifiedArtifact:
        for artifact in self.artifacts:
            if artifact.role == role:
                return artifact
        raise ValueError(f"verified artifact closure is missing role {role}")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "application_identity": self.application_identity,
                "exact_release": self.exact_release,
                "artifacts": [
                    artifact.canonical_payload() for artifact in self.artifacts
                ],
            }
        )


@dataclass(frozen=True, slots=True)
class OwnerUpgradeAction:
    """One inspectable exact command selected by the Installation Owner."""

    owner_identity: str
    action_kind: str
    argv: tuple[str, ...]
    cwd: Path
    environment: tuple[tuple[str, str], ...]
    target_release: str
    artifact_closure_fingerprint: str

    def __post_init__(self) -> None:
        _identity(self.owner_identity, "owner identity")
        _identity(self.action_kind, "action kind")
        _identity(self.target_release, "target release")
        _sha256(self.artifact_closure_fingerprint, "artifact closure fingerprint")
        if not isinstance(self.argv, tuple) or not self.argv:
            raise ValueError("owner action argv must be a nonempty tuple")
        if not Path(self.argv[0]).is_absolute():
            raise ValueError("owner action executable must be absolute")
        if any(
            not isinstance(argument, str) or not argument or "\x00" in argument
            for argument in self.argv
        ):
            raise ValueError("owner action arguments must be nonempty strings")
        if not isinstance(self.cwd, Path) or not self.cwd.is_absolute():
            raise ValueError("owner action working directory must be absolute")
        environment = tuple(sorted(self.environment))
        if len({key for key, _value in environment}) != len(environment) or any(
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\x00" in key
            or not isinstance(value, str)
            or "\x00" in value
            for key, value in environment
        ):
            raise ValueError(
                "owner action environment must contain valid unique strings"
            )
        object.__setattr__(self, "environment", environment)

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "owner_identity": self.owner_identity,
                "action_kind": self.action_kind,
                "argv": list(self.argv),
                "cwd": str(self.cwd),
                "environment": [list(item) for item in self.environment],
                "target_release": self.target_release,
                "artifact_closure_fingerprint": self.artifact_closure_fingerprint,
            }
        )


@dataclass(frozen=True, slots=True)
class OwnerUpgradePreview:
    """Exact reviewed source, target, closure, and owner-native action."""

    application_identity: str
    installation_identity: str
    plan_purpose: str
    source_observation_fingerprint: str
    source_state_fingerprint: str
    owner_identity: str
    owner_installation_identity: str
    owner_runtime_identity: str
    release_channel: str
    platform: str
    architecture: str
    source_release: str
    target_release: str
    target_artifact_digest: str
    resolved_target_fingerprint: str
    candidate_fingerprint: str
    policy_assessment_fingerprint: str
    artifact_closure_fingerprint: str
    rollback_source_release: str
    action: OwnerUpgradeAction

    def __post_init__(self) -> None:
        for field_name in (
            "application_identity",
            "installation_identity",
            "plan_purpose",
            "owner_identity",
            "owner_installation_identity",
            "owner_runtime_identity",
            "release_channel",
            "platform",
            "architecture",
            "source_release",
            "target_release",
            "rollback_source_release",
        ):
            _identity(getattr(self, field_name), field_name.replace("_", " "))
        for field_name in (
            "source_observation_fingerprint",
            "source_state_fingerprint",
            "resolved_target_fingerprint",
            "candidate_fingerprint",
            "policy_assessment_fingerprint",
            "artifact_closure_fingerprint",
        ):
            _sha256(getattr(self, field_name), field_name.replace("_", " "))
        _artifact_digest(self.target_artifact_digest, "target artifact digest")
        if not isinstance(self.action, OwnerUpgradeAction):
            raise ValueError("owner action is required")
        if (
            self.plan_purpose != "reconciliation"
            or self.action.owner_identity != self.owner_identity
            or self.action.target_release != self.target_release
            or self.action.artifact_closure_fingerprint
            != self.artifact_closure_fingerprint
        ):
            raise ValueError("owner action does not bind the reviewed target")
        if self.rollback_source_release != self.source_release:
            raise ValueError(
                "rollback source must preserve the observed source release"
            )

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "application_identity": self.application_identity,
                "installation_identity": self.installation_identity,
                "plan_purpose": self.plan_purpose,
                "source_observation_fingerprint": self.source_observation_fingerprint,
                "source_state_fingerprint": self.source_state_fingerprint,
                "owner_identity": self.owner_identity,
                "owner_installation_identity": self.owner_installation_identity,
                "owner_runtime_identity": self.owner_runtime_identity,
                "release_channel": self.release_channel,
                "platform": self.platform,
                "architecture": self.architecture,
                "source_release": self.source_release,
                "target_release": self.target_release,
                "target_artifact_digest": self.target_artifact_digest,
                "resolved_target_fingerprint": self.resolved_target_fingerprint,
                "candidate_fingerprint": self.candidate_fingerprint,
                "policy_assessment_fingerprint": self.policy_assessment_fingerprint,
                "artifact_closure_fingerprint": self.artifact_closure_fingerprint,
                "rollback_source_release": self.rollback_source_release,
                "action_fingerprint": self.action.fingerprint,
            }
        )


@dataclass(frozen=True, slots=True)
class AuthorizedOwnerUpgrade:
    """Untrusted Plan/Approval references requiring authoritative verification."""

    plan_identity: str
    approval_identity: str
    assessment_identity: str
    preview_fingerprint: str

    def __post_init__(self) -> None:
        for field_name in (
            "plan_identity",
            "approval_identity",
            "assessment_identity",
            "preview_fingerprint",
        ):
            _sha256(getattr(self, field_name), field_name.replace("_", " "))

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "plan_identity": self.plan_identity,
                "approval_identity": self.approval_identity,
                "assessment_identity": self.assessment_identity,
                "preview_fingerprint": self.preview_fingerprint,
            }
        )


@dataclass(frozen=True, slots=True)
class OwnerUpgradeRequest:
    """Exact mutation request carrying expected-current through the owner port."""

    installation_identity: str
    release_channel: str
    expected_owner_installation_identity: str
    expected_owner_runtime_identity: str
    expected_state_fingerprint: str
    preview_fingerprint: str
    action: OwnerUpgradeAction
    artifact_closure: VerifiedArtifactClosure

    def __post_init__(self) -> None:
        for field_name in (
            "installation_identity",
            "release_channel",
            "expected_owner_installation_identity",
            "expected_owner_runtime_identity",
        ):
            _identity(getattr(self, field_name), field_name.replace("_", " "))
        _sha256(self.expected_state_fingerprint, "expected state fingerprint")
        _sha256(self.preview_fingerprint, "preview fingerprint")
        if (
            self.action.artifact_closure_fingerprint
            != self.artifact_closure.fingerprint
        ):
            raise ValueError("owner request action and artifact closure do not match")

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(
            {
                "installation_identity": self.installation_identity,
                "release_channel": self.release_channel,
                "expected_owner_installation_identity": (
                    self.expected_owner_installation_identity
                ),
                "expected_owner_runtime_identity": self.expected_owner_runtime_identity,
                "expected_state_fingerprint": self.expected_state_fingerprint,
                "preview_fingerprint": self.preview_fingerprint,
                "action_fingerprint": self.action.fingerprint,
                "artifact_closure_fingerprint": self.artifact_closure.fingerprint,
            }
        )


@dataclass(frozen=True, slots=True)
class OwnerUpgradeExecutionEvidence:
    """Content-free proof returned by an owner adapter after byte verification."""

    request_fingerprint: str
    artifact_closure_fingerprint: str
    verified_post_state_fingerprint: str

    def __post_init__(self) -> None:
        _sha256(self.request_fingerprint, "owner request fingerprint")
        _sha256(self.artifact_closure_fingerprint, "artifact closure fingerprint")
        _sha256(
            self.verified_post_state_fingerprint,
            "verified post state fingerprint",
        )


class MutationOutcome(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    VERIFIED = "verified"
    UNKNOWN = "unknown"


class PlanFollowUp(StrEnum):
    NONE = "none"
    SUCCESSOR_REQUIRED = "successor_required"


class ArtifactCleanupOutcome(StrEnum):
    VERIFIED = "verified"
    REQUIRED = "required"


@dataclass(frozen=True, slots=True)
class OwnerMutationResult:
    mutation_outcome: MutationOutcome
    plan_follow_up: PlanFollowUp
    reason_code: str
    preview_fingerprint: str
    authorization_fingerprint: str
    action_fingerprint: str
    artifact_cleanup_outcome: ArtifactCleanupOutcome = ArtifactCleanupOutcome.VERIFIED
    pre_observation_fingerprint: str | None = None
    pre_resolution_fingerprint: str | None = None
    post_observation_fingerprint: str | None = None
    post_resolution_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mutation_outcome, MutationOutcome):
            raise ValueError("mutation outcome is required")
        if not isinstance(self.plan_follow_up, PlanFollowUp):
            raise ValueError("Plan follow-up is required")
        if not isinstance(self.artifact_cleanup_outcome, ArtifactCleanupOutcome):
            raise ValueError("artifact cleanup outcome is required")
        _identity(self.reason_code, "reason code")
        for field_name in (
            "preview_fingerprint",
            "authorization_fingerprint",
            "action_fingerprint",
        ):
            _sha256(getattr(self, field_name), field_name.replace("_", " "))
        for field_name in (
            "pre_observation_fingerprint",
            "pre_resolution_fingerprint",
            "post_observation_fingerprint",
            "post_resolution_fingerprint",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _sha256(value, field_name.replace("_", " "))


class InstallationDiscovery(Protocol):
    def discover(
        self,
        *,
        selected_installation_identity: str,
        selected_release_channel: str,
    ) -> InstallationObservation: ...


class CurrentResolver(Protocol):
    def resolve(
        self,
        selected: ExternalApplicationInstallation,
        observed: InstallationObservation,
    ) -> CurrentReleaseResolution: ...


class OwnerUpgradeAuthorizationVerifier(Protocol):
    def hold_authorization(
        self,
        selected: ExternalApplicationInstallation,
        preview: OwnerUpgradePreview,
        authorization: AuthorizedOwnerUpgrade,
        artifact_closure: VerifiedArtifactClosure,
    ) -> AbstractContextManager[bool]: ...


class OwnerUpgradeMaterialVerifier(Protocol):
    """Verify owner-specific closure and action shape without mutation."""

    def verify_authorization_material(
        self,
        selected: ExternalApplicationInstallation,
        preview: OwnerUpgradePreview,
        artifact_closure: VerifiedArtifactClosure,
    ) -> bool: ...


class OwnerUpgradeExecutor(Protocol):
    def apply_exact(
        self, request: OwnerUpgradeRequest
    ) -> OwnerUpgradeExecutionEvidence: ...


class ArtifactClosureReleaser(Protocol):
    def release(self, closure: VerifiedArtifactClosure) -> None: ...


Transition = Callable[[str], AbstractContextManager[None]]


@contextmanager
def _verified_authorization(
    verifier: OwnerUpgradeAuthorizationVerifier,
    selected: ExternalApplicationInstallation,
    preview: OwnerUpgradePreview,
    authorization: AuthorizedOwnerUpgrade,
    artifact_closure: VerifiedArtifactClosure,
) -> Iterator[None]:
    with verifier.hold_authorization(
        selected, preview, authorization, artifact_closure
    ) as verified:
        if not verified:
            raise ValueError("owner upgrade authorization was not verified")
        yield


def build_owner_upgrade_preview(
    candidate: UpgradeCandidate,
    assessment: UpgradePolicyAssessment,
    resolution: CurrentReleaseResolution,
    source_observation: InstallationObservation,
    artifact_closure: VerifiedArtifactClosure,
    action: OwnerUpgradeAction,
) -> OwnerUpgradePreview:
    if (
        assessment.disposition
        is not UpgradePolicyAssessmentDisposition.APPROVAL_REQUIRED
        or assessment.candidate_fingerprint != candidate.fingerprint
        or source_observation.fingerprint
        != candidate.installation_observation_fingerprint
        or resolution.fingerprint != candidate.current_resolution_fingerprint
    ):
        raise ValueError(
            "owner upgrade preview requires exact approval-required evidence"
        )
    if (
        artifact_closure.application_identity != candidate.application_identity
        or artifact_closure.exact_release != candidate.target_release
        or artifact_closure.artifact("primary").archive_digest
        != candidate.target_artifact_digest
        or action.owner_identity != candidate.owner_identity
        or action.target_release != candidate.target_release
        or action.artifact_closure_fingerprint != artifact_closure.fingerprint
    ):
        raise ValueError("owner action or artifact closure does not match candidate")
    return OwnerUpgradePreview(
        application_identity=candidate.application_identity,
        installation_identity=candidate.installation_identity,
        plan_purpose="reconciliation",
        source_observation_fingerprint=source_observation.fingerprint,
        source_state_fingerprint=source_observation.state_fingerprint,
        owner_identity=candidate.owner_identity,
        owner_installation_identity=candidate.owner_installation_identity,
        owner_runtime_identity=candidate.owner_runtime_identity,
        release_channel=candidate.release_channel,
        platform=candidate.platform,
        architecture=candidate.architecture,
        source_release=candidate.source_release,
        target_release=candidate.target_release,
        target_artifact_digest=candidate.target_artifact_digest,
        resolved_target_fingerprint=resolution.resolved_target_fingerprint,
        candidate_fingerprint=candidate.fingerprint,
        policy_assessment_fingerprint=assessment.fingerprint,
        artifact_closure_fingerprint=artifact_closure.fingerprint,
        rollback_source_release=candidate.source_release,
        action=action,
    )


def apply_owner_upgrade(
    selected: ExternalApplicationInstallation,
    preview: OwnerUpgradePreview,
    authorization: AuthorizedOwnerUpgrade,
    artifact_closure: VerifiedArtifactClosure,
    *,
    authorization_verifier: OwnerUpgradeAuthorizationVerifier,
    discovery: InstallationDiscovery,
    current_resolver: CurrentResolver,
    executor: OwnerUpgradeExecutor,
    artifact_releaser: ArtifactClosureReleaser,
    transition: Transition,
    clock: Callable[[], datetime],
) -> OwnerMutationResult:
    try:
        result = _apply_owner_upgrade_retained(
            selected,
            preview,
            authorization,
            artifact_closure,
            authorization_verifier=authorization_verifier,
            discovery=discovery,
            current_resolver=current_resolver,
            executor=executor,
            transition=transition,
            clock=clock,
        )
    except BaseException as primary_error:
        try:
            artifact_releaser.release(artifact_closure)
        except BaseException as cleanup_error:
            primary_error.add_note(
                f"artifact closure cleanup also failed: {type(cleanup_error).__name__}"
            )
        raise
    try:
        artifact_releaser.release(artifact_closure)
    except OwnerUpgradeCommandError:
        return replace(
            result,
            plan_follow_up=PlanFollowUp.SUCCESSOR_REQUIRED,
            artifact_cleanup_outcome=ArtifactCleanupOutcome.REQUIRED,
        )
    return result


def _apply_owner_upgrade_retained(
    selected: ExternalApplicationInstallation,
    preview: OwnerUpgradePreview,
    authorization: AuthorizedOwnerUpgrade,
    artifact_closure: VerifiedArtifactClosure,
    *,
    authorization_verifier: OwnerUpgradeAuthorizationVerifier,
    discovery: InstallationDiscovery,
    current_resolver: CurrentResolver,
    executor: OwnerUpgradeExecutor,
    transition: Transition,
    clock: Callable[[], datetime],
) -> OwnerMutationResult:
    _validate_selected(selected, preview, authorization, artifact_closure)
    with (
        _verified_authorization(
            authorization_verifier,
            selected,
            preview,
            authorization,
            artifact_closure,
        ),
        transition(selected.installation_identity),
    ):
        try:
            source = _discover(selected, discovery)
        except InstallationDiscoveryError:
            return _result_not_attempted(
                "source_discovery_changed", preview, authorization
            )
        if source.state_fingerprint != preview.source_state_fingerprint:
            return _result_not_attempted(
                "source_observation_changed", preview, authorization, source
            )
        try:
            current = current_resolver.resolve(selected, source)
        except CurrentReleaseResolutionError:
            return _result_not_attempted(
                "current_resolution_unavailable", preview, authorization, source
            )
        now = _aware(clock(), "upgrade execution time")
        if (
            current.resolved_target_fingerprint != preview.resolved_target_fingerprint
            or now >= current.expires_at
        ):
            return _result_not_attempted(
                "current_resolution_changed", preview, authorization, source, current
            )
        try:
            fenced = _discover(selected, discovery)
        except InstallationDiscoveryError:
            return _result_not_attempted(
                "owner_fence_unavailable", preview, authorization, source, current
            )
        if fenced.state_fingerprint != source.state_fingerprint:
            return _result_not_attempted(
                "owner_fence_changed", preview, authorization, source, current
            )
        request = OwnerUpgradeRequest(
            installation_identity=selected.installation_identity,
            release_channel=selected.release_intent.channel,
            expected_owner_installation_identity=fenced.owner_installation_identity,
            expected_owner_runtime_identity=fenced.owner_runtime_identity,
            expected_state_fingerprint=fenced.state_fingerprint,
            preview_fingerprint=preview.fingerprint,
            action=preview.action,
            artifact_closure=artifact_closure,
        )
        try:
            execution = executor.apply_exact(request)
        except OwnerUpgradeNotAttemptedError as error:
            return _result_not_attempted(
                error.reason_code,
                preview,
                authorization,
                source,
                current,
            )
        except OwnerUpgradeCommandError:
            return _result_unknown(
                "owner_command_outcome_unknown", preview, authorization, source, current
            )
        if (
            execution.request_fingerprint != request.fingerprint
            or execution.artifact_closure_fingerprint != artifact_closure.fingerprint
        ):
            return _result_unknown(
                "artifact_closure_not_verified", preview, authorization, source, current
            )
        try:
            after = _discover(selected, discovery)
        except InstallationDiscoveryError:
            return _result_verified_successor(
                "post_mutation_discovery_unavailable",
                preview,
                authorization,
                source,
                current,
            )
        if not _target_installation_verified(preview, source, after):
            return _result_verified_successor(
                "target_installation_changed_after_verification",
                preview,
                authorization,
                source,
                current,
                after,
            )
        if after.state_fingerprint != execution.verified_post_state_fingerprint:
            return _result_verified_successor(
                "verified_artifact_state_changed",
                preview,
                authorization,
                source,
                current,
                after,
            )
        try:
            after_current = current_resolver.resolve(selected, after)
        except CurrentReleaseResolutionError:
            return _result_verified_successor(
                "post_mutation_current_unavailable",
                preview,
                authorization,
                source,
                current,
                after,
            )
        if (
            after_current.exact_release != preview.target_release
            or after_current.artifact_digest != preview.target_artifact_digest
        ):
            return _result(
                MutationOutcome.VERIFIED,
                PlanFollowUp.SUCCESSOR_REQUIRED,
                "current_release_advanced",
                preview,
                authorization,
                source,
                current,
                after,
                after_current,
            )
        return _result(
            MutationOutcome.VERIFIED,
            PlanFollowUp.NONE,
            "verified",
            preview,
            authorization,
            source,
            current,
            after,
            after_current,
        )


def _validate_selected(selected, preview, authorization, artifact_closure) -> None:
    if (
        authorization.preview_fingerprint != preview.fingerprint
        or artifact_closure.fingerprint != preview.artifact_closure_fingerprint
    ):
        raise ValueError("owner upgrade authorization does not match preview")
    if (
        selected.application_identity != preview.application_identity
        or selected.installation_identity != preview.installation_identity
        or selected.owner_identity != preview.owner_identity
        or selected.release_intent.channel != preview.release_channel
        or selected.platform != preview.platform
        or selected.architecture != preview.architecture
    ):
        raise ValueError("selected installation does not match authorization preview")


def _discover(selected, discovery) -> InstallationObservation:
    return discovery.discover(
        selected_installation_identity=selected.installation_identity,
        selected_release_channel=selected.release_intent.channel,
    )


def _target_installation_verified(preview, source, after) -> bool:
    return (
        after.application_identity == preview.application_identity
        and after.installation_identity == preview.installation_identity
        and after.owner_identity == preview.owner_identity
        and after.owner_runtime_identity == preview.owner_runtime_identity
        and after.release_channel == preview.release_channel
        and after.platform == preview.platform
        and after.architecture == preview.architecture
        and after.installed_release == preview.target_release
        and after.active_invocation == source.active_invocation
        and after.reachable_invocations == source.reachable_invocations
    )


def _result_not_attempted(reason, preview, authorization, source=None, current=None):
    return _result(
        MutationOutcome.NOT_ATTEMPTED,
        PlanFollowUp.SUCCESSOR_REQUIRED,
        reason,
        preview,
        authorization,
        source,
        current,
    )


def _result_unknown(reason, preview, authorization, source, current, after=None):
    return _result(
        MutationOutcome.UNKNOWN,
        PlanFollowUp.SUCCESSOR_REQUIRED,
        reason,
        preview,
        authorization,
        source,
        current,
        after,
    )


def _result_verified_successor(
    reason, preview, authorization, source, current, after=None
):
    return _result(
        MutationOutcome.VERIFIED,
        PlanFollowUp.SUCCESSOR_REQUIRED,
        reason,
        preview,
        authorization,
        source,
        current,
        after,
    )


def _result(
    outcome,
    follow_up,
    reason,
    preview,
    authorization,
    source=None,
    current=None,
    after=None,
    after_current=None,
):
    return OwnerMutationResult(
        mutation_outcome=outcome,
        plan_follow_up=follow_up,
        reason_code=reason,
        preview_fingerprint=preview.fingerprint,
        authorization_fingerprint=authorization.fingerprint,
        action_fingerprint=preview.action.fingerprint,
        pre_observation_fingerprint=(source.fingerprint if source else None),
        pre_resolution_fingerprint=(current.fingerprint if current else None),
        post_observation_fingerprint=(after.fingerprint if after else None),
        post_resolution_fingerprint=(
            after_current.fingerprint if after_current else None
        ),
    )
