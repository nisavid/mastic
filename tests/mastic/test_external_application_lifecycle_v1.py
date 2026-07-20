import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mastic.application.application_upgrade_policy import (
    assess_unattended_upgrade,
    build_upgrade_candidate,
)
from mastic.application.external_application_lifecycle import (
    ArtifactCleanupOutcome,
    AuthorizedOwnerUpgrade,
    InstallationDiscoveryError,
    MutationOutcome,
    OwnerUpgradeAction,
    OwnerUpgradeCommandError,
    OwnerUpgradeExecutionEvidence,
    PlanFollowUp,
    VerifiedArtifact,
    VerifiedArtifactClosure,
    apply_owner_upgrade,
    build_owner_upgrade_preview,
)
from mastic.domain.application_lifecycle import (
    ReleaseTransitionKind,
    UnattendedUpgradePolicy,
)
from mastic.domain.external_applications import (
    CurrentReleaseResolution,
    ExternalApplicationInstallation,
    InstallationObservation,
    ReleaseIntent,
)


NOW = datetime(2026, 7, 20, 21, 0, tzinfo=UTC)
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64
SHA_E = "sha256:" + "e" * 64
SHA_F = "sha256:" + "f" * 64
SHA512_A = "sha512:" + "a" * 128
SHA512_B = "sha512:" + "b" * 128


def installation() -> ExternalApplicationInstallation:
    return ExternalApplicationInstallation(
        application_identity="external-application:codex",
        installation_identity="application-installation:codex:vite",
        owner_identity="vite-plus/npm-global",
        release_intent=ReleaseIntent.current(channel="npm:latest"),
        platform="darwin",
        architecture="arm64",
    )


def observation(
    *,
    release="0.144.5",
    owner_installation=SHA_A,
    owner_runtime="node:24.18.0",
    installed_digest=SHA_B,
    observed_at=NOW,
) -> InstallationObservation:
    active = "/Users/test/.vite-plus/bin/codex"
    return InstallationObservation(
        application_identity="external-application:codex",
        installation_identity="application-installation:codex:vite",
        owner_identity="vite-plus/npm-global",
        owner_installation_identity=owner_installation,
        owner_runtime_identity=owner_runtime,
        release_channel="npm:latest",
        platform="darwin",
        architecture="arm64",
        installed_release=release,
        installed_artifact_digest=installed_digest,
        active_invocation=active,
        reachable_invocations=(active,),
        observed_at=observed_at,
    )


def resolution(
    observed,
    *,
    release="0.144.6",
    digest=SHA512_A,
    observed_at=NOW + timedelta(seconds=1),
) -> CurrentReleaseResolution:
    return CurrentReleaseResolution(
        installation_identity=observed.installation_identity,
        installation_observation_fingerprint=observed.fingerprint,
        owner_identity=observed.owner_identity,
        release_channel=observed.release_channel,
        platform=observed.platform,
        architecture=observed.architecture,
        exact_release=release,
        artifact_coordinate=f"npm:@openai/codex@{release}",
        artifact_digest=digest,
        authority_identity="release-authority:npmjs:@openai/codex:latest",
        authority_response_digest=SHA_C,
        observed_at=observed_at,
        expires_at=observed_at + timedelta(minutes=5),
        resolver_policy_identity="current-online:v1",
        validation_profile_identity="codex-current:v1",
    )


def closure() -> VerifiedArtifactClosure:
    return VerifiedArtifactClosure(
        application_identity="external-application:codex",
        exact_release="0.144.6",
        artifacts=(
            VerifiedArtifact(
                role="primary",
                package_identity="@openai/codex",
                exact_release="0.144.6",
                coordinate="npm:@openai/codex@0.144.6",
                archive_digest=SHA512_A,
                installed_payload_digest=SHA_D,
                staged_path=Path("/private/tmp/staged/codex-0.144.6.tgz"),
            ),
            VerifiedArtifact(
                role="platform",
                package_identity="@openai/codex-darwin-arm64",
                exact_release="0.144.6-darwin-arm64",
                coordinate="npm:@openai/codex@0.144.6-darwin-arm64",
                archive_digest=SHA512_B,
                installed_payload_digest=SHA_E,
                staged_path=Path("/private/tmp/staged/codex-darwin-arm64.tgz"),
            ),
        ),
        staging_directory=Path("/private/tmp/staged"),
        cache_directory=Path("/private/tmp/staged/npm-cache"),
    )


def preview_bundle():
    selected = installation()
    source = observation()
    current = resolution(source)
    candidate = build_upgrade_candidate(
        selected, source, current, transition=ReleaseTransitionKind.UPGRADE
    )
    policy = UnattendedUpgradePolicy(
        policy_identity="unattended-upgrade:codex:v1",
        application_identity=selected.application_identity,
        owner_identity=selected.owner_identity,
        release_channel="npm:latest",
        validation_profile_identity="codex-upgrade:v1",
        data_bearing=False,
        maximum_backup_age=timedelta(minutes=5),
    )
    assessment = assess_unattended_upgrade(
        policy, candidate, now=NOW + timedelta(seconds=2)
    )
    artifacts = closure()
    action = OwnerUpgradeAction(
        owner_identity=selected.owner_identity,
        action_kind="npm-global-install-verified-closure",
        argv=("/Users/test/.vite-plus/bin/vp", "env", "exec"),
        cwd=artifacts.staging_directory,
        environment=(("NO_COLOR", "1"),),
        target_release="0.144.6",
        artifact_closure_fingerprint=artifacts.fingerprint,
    )
    preview = build_owner_upgrade_preview(
        candidate, assessment, current, source, artifacts, action
    )
    return preview, artifacts


def authorization(preview):
    return AuthorizedOwnerUpgrade(SHA_D, SHA_E, preview.fingerprint)


class Verifier:
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.calls = []

    def verify(self, selected, preview, authorization, artifact_closure):
        self.calls.append((selected, preview, authorization, artifact_closure))
        return self.accepted


class Discovery:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = []

    def discover(self, *, selected_installation_identity, selected_release_channel):
        self.calls.append((selected_installation_identity, selected_release_channel))
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


class Resolver:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = []

    def resolve(self, selected, observed):
        self.calls.append((selected, observed))
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


class Executor:
    def __init__(self, *, error=None, evidence=None, verified_post_state=SHA_F):
        self.error = error
        self.evidence = evidence
        self.verified_post_state = verified_post_state
        self.requests = []

    def apply_exact(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        if self.evidence is not None:
            return self.evidence
        return OwnerUpgradeExecutionEvidence(
            request.fingerprint,
            request.artifact_closure.fingerprint,
            self.verified_post_state,
        )


class Releaser:
    def __init__(self, error=None):
        self.error = error
        self.closures = []

    def release(self, selected):
        self.closures.append(selected)
        if self.error:
            raise self.error


class Locks:
    def __init__(self):
        self.identities = []

    @contextmanager
    def hold(self, identity):
        self.identities.append(identity)
        yield


def apply(
    preview,
    artifacts,
    discovery,
    resolver,
    executor,
    *,
    verifier=None,
    releaser=None,
):
    releaser = releaser or Releaser()
    return apply_owner_upgrade(
        installation(),
        preview,
        authorization(preview),
        artifacts,
        authorization_verifier=verifier or Verifier(),
        discovery=discovery,
        current_resolver=resolver,
        executor=executor,
        artifact_releaser=releaser,
        transition=Locks().hold,
        clock=lambda: NOW + timedelta(seconds=3),
    )


class ExternalApplicationLifecycleTests(unittest.TestCase):
    def test_preview_binds_stable_state_target_closure_and_exact_action(self):
        preview, artifacts = preview_bundle()

        self.assertEqual(preview.source_release, "0.144.5")
        self.assertEqual(preview.rollback_source_release, "0.144.5")
        self.assertEqual(preview.artifact_closure_fingerprint, artifacts.fingerprint)
        self.assertEqual(preview.target_artifact_digest, SHA512_A)
        self.assertTrue(preview.fingerprint.startswith("sha256:"))

    def test_unverified_hash_claim_never_reaches_lock_discovery_or_executor(self):
        preview, artifacts = preview_bundle()
        verifier = Verifier(accepted=False)
        discovery = Discovery([])
        executor = Executor()
        locks = Locks()
        releaser = Releaser()

        with self.assertRaisesRegex(ValueError, "not verified"):
            apply_owner_upgrade(
                installation(),
                preview,
                authorization(preview),
                artifacts,
                authorization_verifier=verifier,
                discovery=discovery,
                current_resolver=Resolver([]),
                executor=executor,
                artifact_releaser=releaser,
                transition=locks.hold,
                clock=lambda: NOW,
            )

        self.assertEqual(len(verifier.calls), 1)
        self.assertEqual(locks.identities, [])
        self.assertEqual(discovery.calls, [])
        self.assertEqual(executor.requests, [])
        self.assertEqual(releaser.closures, [artifacts])

    def test_later_equivalent_evidence_crosses_both_cas_fences(self):
        preview, artifacts = preview_bundle()
        source_later = observation(observed_at=NOW + timedelta(seconds=3))
        fenced_later = observation(observed_at=NOW + timedelta(seconds=4))
        current_later = resolution(source_later, observed_at=NOW + timedelta(seconds=5))
        target = observation(
            release="0.144.6",
            owner_installation=SHA_F,
            observed_at=NOW + timedelta(seconds=6),
        )
        target_current = resolution(target, observed_at=NOW + timedelta(seconds=7))
        executor = Executor(verified_post_state=target.state_fingerprint)
        releaser = Releaser()

        result = apply(
            preview,
            artifacts,
            Discovery([source_later, fenced_later, target]),
            Resolver([current_later, target_current]),
            executor,
            releaser=releaser,
        )

        self.assertEqual(result.mutation_outcome, MutationOutcome.VERIFIED)
        self.assertEqual(result.plan_follow_up, PlanFollowUp.NONE)
        self.assertEqual(len(executor.requests), 1)
        request = executor.requests[0]
        self.assertEqual(
            request.expected_state_fingerprint, fenced_later.state_fingerprint
        )
        self.assertEqual(
            request.expected_owner_installation_identity,
            fenced_later.owner_installation_identity,
        )
        self.assertEqual(request.expected_owner_runtime_identity, "node:24.18.0")
        self.assertEqual(request.artifact_closure, artifacts)
        self.assertEqual(releaser.closures, [artifacts])

    def test_verified_mutation_survives_declared_cleanup_failure(self):
        preview, artifacts = preview_bundle()
        source = observation(observed_at=NOW + timedelta(seconds=3))
        current = resolution(source, observed_at=NOW + timedelta(seconds=4))
        target = observation(
            release="0.144.6",
            owner_installation=SHA_F,
            observed_at=NOW + timedelta(seconds=5),
        )
        target_current = resolution(target, observed_at=NOW + timedelta(seconds=6))
        releaser = Releaser(OwnerUpgradeCommandError("artifact_release_refused"))

        result = apply(
            preview,
            artifacts,
            Discovery([source, source, target]),
            Resolver([current, target_current]),
            Executor(verified_post_state=target.state_fingerprint),
            releaser=releaser,
        )

        self.assertEqual(result.mutation_outcome, MutationOutcome.VERIFIED)
        self.assertEqual(result.reason_code, "verified")
        self.assertEqual(result.plan_follow_up, PlanFollowUp.SUCCESSOR_REQUIRED)
        self.assertEqual(
            result.artifact_cleanup_outcome, ArtifactCleanupOutcome.REQUIRED
        )

    def test_verified_mutation_does_not_hide_programmer_error_during_cleanup(self):
        preview, artifacts = preview_bundle()
        source = observation(observed_at=NOW + timedelta(seconds=3))
        current = resolution(source, observed_at=NOW + timedelta(seconds=4))
        target = observation(
            release="0.144.6",
            owner_installation=SHA_F,
            observed_at=NOW + timedelta(seconds=5),
        )
        target_current = resolution(target, observed_at=NOW + timedelta(seconds=6))
        releaser = Releaser(AssertionError("programmer defect"))

        with self.assertRaisesRegex(AssertionError, "programmer defect"):
            apply(
                preview,
                artifacts,
                Discovery([source, source, target]),
                Resolver([current, target_current]),
                Executor(verified_post_state=target.state_fingerprint),
                releaser=releaser,
            )

        self.assertEqual(releaser.closures, [artifacts])

    def test_changed_owner_bytes_block_before_command(self):
        preview, artifacts = preview_bundle()
        changed = observation(installed_digest=SHA_F, observed_at=NOW)
        executor = Executor()

        result = apply(preview, artifacts, Discovery([changed]), Resolver([]), executor)

        self.assertEqual(result.mutation_outcome, MutationOutcome.NOT_ATTEMPTED)
        self.assertEqual(result.plan_follow_up, PlanFollowUp.SUCCESSOR_REQUIRED)
        self.assertEqual(result.reason_code, "source_observation_changed")
        self.assertEqual(executor.requests, [])

    def test_current_target_drift_blocks_before_command(self):
        preview, artifacts = preview_bundle()
        source = observation(observed_at=NOW + timedelta(seconds=3))
        moved = resolution(source, release="0.145.0", digest=SHA512_B)
        executor = Executor()

        result = apply(
            preview, artifacts, Discovery([source]), Resolver([moved]), executor
        )

        self.assertEqual(result.mutation_outcome, MutationOutcome.NOT_ATTEMPTED)
        self.assertEqual(result.reason_code, "current_resolution_changed")
        self.assertEqual(executor.requests, [])

    def test_mismatched_execution_closure_is_not_verified(self):
        preview, artifacts = preview_bundle()
        source = observation(observed_at=NOW + timedelta(seconds=3))
        current = resolution(source, observed_at=NOW + timedelta(seconds=4))
        executor = Executor(evidence=OwnerUpgradeExecutionEvidence(SHA_D, SHA_E, SHA_F))

        result = apply(
            preview,
            artifacts,
            Discovery([source, source]),
            Resolver([current]),
            executor,
        )

        self.assertEqual(result.mutation_outcome, MutationOutcome.UNKNOWN)
        self.assertEqual(result.reason_code, "artifact_closure_not_verified")

    def test_completed_install_and_advanced_channel_keep_separate_axes(self):
        preview, artifacts = preview_bundle()
        source = observation(observed_at=NOW + timedelta(seconds=3))
        current = resolution(source, observed_at=NOW + timedelta(seconds=4))
        target = observation(
            release="0.144.6",
            owner_installation=SHA_F,
            observed_at=NOW + timedelta(seconds=5),
        )
        advanced = resolution(
            target,
            release="0.145.0",
            digest=SHA512_B,
            observed_at=NOW + timedelta(seconds=6),
        )

        result = apply(
            preview,
            artifacts,
            Discovery([source, source, target]),
            Resolver([current, advanced]),
            Executor(verified_post_state=target.state_fingerprint),
        )

        self.assertEqual(result.mutation_outcome, MutationOutcome.VERIFIED)
        self.assertEqual(result.plan_follow_up, PlanFollowUp.SUCCESSOR_REQUIRED)
        self.assertEqual(result.reason_code, "current_release_advanced")

    def test_post_verification_runtime_drift_keeps_verified_mutation_outcome(self):
        preview, artifacts = preview_bundle()
        source = observation(observed_at=NOW + timedelta(seconds=3))
        current = resolution(source, observed_at=NOW + timedelta(seconds=4))
        byte_verified_target = observation(
            release="0.144.6",
            owner_installation=SHA_F,
            observed_at=NOW + timedelta(seconds=5),
        )
        drifted_target = observation(
            release="0.144.6",
            owner_installation=SHA_F,
            owner_runtime="node:25.0.0",
            observed_at=NOW + timedelta(seconds=6),
        )

        result = apply(
            preview,
            artifacts,
            Discovery([source, source, drifted_target]),
            Resolver([current]),
            Executor(verified_post_state=byte_verified_target.state_fingerprint),
        )

        self.assertEqual(result.mutation_outcome, MutationOutcome.VERIFIED)
        self.assertEqual(result.plan_follow_up, PlanFollowUp.SUCCESSOR_REQUIRED)
        self.assertEqual(
            result.reason_code, "target_installation_changed_after_verification"
        )

    def test_declared_command_failure_is_unknown_without_rollback(self):
        preview, artifacts = preview_bundle()
        source = observation(observed_at=NOW + timedelta(seconds=3))
        current = resolution(source, observed_at=NOW + timedelta(seconds=4))
        executor = Executor(error=OwnerUpgradeCommandError("private detail"))

        result = apply(
            preview,
            artifacts,
            Discovery([source, source]),
            Resolver([current]),
            executor,
        )

        self.assertEqual(result.mutation_outcome, MutationOutcome.UNKNOWN)
        self.assertEqual(result.reason_code, "owner_command_outcome_unknown")
        self.assertNotIn("private", repr(result))

    def test_unknown_mutation_survives_declared_cleanup_failure(self):
        preview, artifacts = preview_bundle()
        source = observation(observed_at=NOW + timedelta(seconds=3))
        current = resolution(source, observed_at=NOW + timedelta(seconds=4))
        releaser = Releaser(OwnerUpgradeCommandError("artifact_release_refused"))

        result = apply(
            preview,
            artifacts,
            Discovery([source, source]),
            Resolver([current]),
            Executor(error=OwnerUpgradeCommandError("owner_command_failed")),
            releaser=releaser,
        )

        self.assertEqual(result.mutation_outcome, MutationOutcome.UNKNOWN)
        self.assertEqual(result.reason_code, "owner_command_outcome_unknown")
        self.assertEqual(result.plan_follow_up, PlanFollowUp.SUCCESSOR_REQUIRED)
        self.assertEqual(
            result.artifact_cleanup_outcome, ArtifactCleanupOutcome.REQUIRED
        )

    def test_programmer_error_is_never_reclassified_as_domain_drift(self):
        preview, artifacts = preview_bundle()
        releaser = Releaser()

        with self.assertRaisesRegex(AssertionError, "programmer defect"):
            apply(
                preview,
                artifacts,
                Discovery([AssertionError("programmer defect")]),
                Resolver([]),
                Executor(),
                releaser=releaser,
            )
        self.assertEqual(releaser.closures, [artifacts])

    def test_declared_discovery_error_is_a_safe_no_command_successor(self):
        preview, artifacts = preview_bundle()
        result = apply(
            preview,
            artifacts,
            Discovery([InstallationDiscoveryError("unavailable")]),
            Resolver([]),
            Executor(),
        )

        self.assertEqual(result.mutation_outcome, MutationOutcome.NOT_ATTEMPTED)
        self.assertEqual(result.plan_follow_up, PlanFollowUp.SUCCESSOR_REQUIRED)


if __name__ == "__main__":
    unittest.main()
