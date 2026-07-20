import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from mastic.application.application_upgrade_policy import (
    UpgradePolicyAssessmentDisposition,
    assess_unattended_upgrade,
    build_upgrade_candidate,
)
from mastic.domain.application_lifecycle import (
    ExactRemovalTarget,
    IsolatedRestoreObservation,
    RecoveryQualification,
    ReleaseTransitionKind,
    RepresentativeRecallObservation,
    RemovalPlan,
    UnattendedUpgradePolicy,
    ValidatedBackup,
)
from mastic.domain.external_applications import (
    CurrentReleaseResolution,
    ExternalApplicationInstallation,
    InstallationObservation,
    ReleaseIntent,
)


NOW = datetime(2026, 7, 20, 20, 0, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def installation(*, application: str = "codex") -> ExternalApplicationInstallation:
    return ExternalApplicationInstallation(
        application_identity=f"external-application:{application}",
        installation_identity=f"application-installation:{application}:owner",
        owner_identity=f"installation-owner:{application}:native",
        release_intent=ReleaseIntent.current(channel="stable"),
        platform="darwin",
        architecture="arm64",
    )


def observation(*, application: str = "codex") -> InstallationObservation:
    selected = installation(application=application)
    return InstallationObservation(
        application_identity=selected.application_identity,
        installation_identity=selected.installation_identity,
        owner_identity=selected.owner_identity,
        owner_installation_identity=f"owner-installation:{application}:one",
        owner_runtime_identity="runtime:one",
        release_channel="stable",
        platform="darwin",
        architecture="arm64",
        installed_release="1.0.0",
        installed_artifact_digest=DIGEST_A,
        active_invocation=f"/Users/test/bin/{application}",
        reachable_invocations=(f"/Users/test/bin/{application}",),
        observed_at=NOW,
    )


def resolution(*, application: str = "codex") -> CurrentReleaseResolution:
    observed = observation(application=application)
    return CurrentReleaseResolution(
        installation_identity=observed.installation_identity,
        installation_observation_fingerprint=observed.fingerprint,
        owner_identity=observed.owner_identity,
        release_channel="stable",
        platform="darwin",
        architecture="arm64",
        exact_release="1.1.0",
        artifact_coordinate=f"owner:{application}@1.1.0",
        artifact_digest=DIGEST_B,
        authority_identity=f"release-authority:{application}:stable",
        authority_response_digest=DIGEST_C,
        observed_at=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=16),
        resolver_policy_identity="current-online:v1",
        validation_profile_identity=f"{application}-current:v1",
    )


def policy(*, application: str = "codex", data_bearing: bool = False):
    selected = installation(application=application)
    return UnattendedUpgradePolicy(
        policy_identity=f"unattended-upgrade:{application}:v1",
        application_identity=selected.application_identity,
        owner_identity=selected.owner_identity,
        release_channel="stable",
        validation_profile_identity=f"{application}-upgrade:v1",
        data_bearing=data_bearing,
        maximum_backup_age=timedelta(minutes=30),
    )


def candidate(*, application: str = "codex"):
    return build_upgrade_candidate(
        installation(application=application),
        observation(application=application),
        resolution(application=application),
        transition=ReleaseTransitionKind.UPGRADE,
    )


def recovery(*, selected, stale: bool = False, backup_override=None):
    validated_at = NOW - timedelta(hours=1) if stale else NOW + timedelta(minutes=2)
    backup = backup_override or ValidatedBackup(
        installation_identity=selected.installation_identity,
        source_observation_fingerprint=(selected.installation_observation_fingerprint),
        candidate_fingerprint=selected.fingerprint,
        current_resolution_fingerprint=selected.current_resolution_fingerprint,
        backup_identity="backup:hindsight:2026-07-20T2002Z",
        artifact_digest=DIGEST_A,
        snapshot_fingerprint=DIGEST_B,
        validated_at=validated_at,
    )
    restore = IsolatedRestoreObservation(
        candidate_fingerprint=selected.fingerprint,
        validated_backup_fingerprint=backup.fingerprint,
        restored_snapshot_fingerprint=backup.snapshot_fingerprint,
        successful=True,
        observed_at=validated_at + timedelta(seconds=30),
    )
    recall = RepresentativeRecallObservation(
        candidate_fingerprint=selected.fingerprint,
        validated_backup_fingerprint=backup.fingerprint,
        isolated_restore_observation_fingerprint=restore.fingerprint,
        validation_profile_identity="hindsight-upgrade:v1",
        successful=True,
        observed_at=validated_at + timedelta(seconds=45),
    )
    return RecoveryQualification(
        candidate_fingerprint=selected.fingerprint,
        validation_profile_identity="hindsight-upgrade:v1",
        validated_backup_fingerprint=backup.fingerprint,
        isolated_restore=restore,
        representative_recall=recall,
        qualified_at=validated_at + timedelta(minutes=1),
    ), backup


class ApplicationUpgradePolicyTests(unittest.TestCase):
    def test_matching_policy_requires_a_new_exact_plan_approval(self) -> None:
        assessment = assess_unattended_upgrade(
            policy(), candidate(), now=NOW + timedelta(minutes=2)
        )

        self.assertEqual(
            assessment.disposition,
            UpgradePolicyAssessmentDisposition.APPROVAL_REQUIRED,
        )
        self.assertEqual(assessment.reason_codes, ())
        self.assertFalse(hasattr(assessment, "approved"))
        self.assertTrue(assessment.candidate_fingerprint.startswith("sha256:"))
        self.assertEqual(assessment.policy_fingerprint, policy().fingerprint)
        self.assertEqual(assessment.assessed_at, NOW + timedelta(minutes=2))
        self.assertTrue(assessment.fingerprint.startswith("sha256:"))

    def test_policy_never_authorizes_owner_channel_or_application_drift(self) -> None:
        selected = candidate()
        mismatches = (
            replace(policy(), owner_identity="installation-owner:other"),
            replace(policy(), release_channel="preview"),
            replace(policy(), application_identity="external-application:other"),
        )

        for mismatch in mismatches:
            with self.subTest(policy=mismatch):
                assessed = assess_unattended_upgrade(
                    mismatch,
                    selected,
                    now=NOW + timedelta(minutes=2),
                )
                self.assertEqual(
                    assessed.disposition,
                    UpgradePolicyAssessmentDisposition.BLOCKED,
                )

    def test_assessment_fingerprint_detects_policy_drift_under_one_identity(
        self,
    ) -> None:
        first_policy = policy()
        changed_policy = replace(
            first_policy,
            maximum_backup_age=timedelta(minutes=45),
        )

        first = assess_unattended_upgrade(
            first_policy,
            candidate(),
            now=NOW + timedelta(minutes=2),
        )
        changed = assess_unattended_upgrade(
            changed_policy,
            candidate(),
            now=NOW + timedelta(minutes=2),
        )

        self.assertNotEqual(first.policy_fingerprint, changed.policy_fingerprint)
        self.assertNotEqual(first.fingerprint, changed.fingerprint)

    def test_same_downgrade_and_unknown_transitions_are_blocked(self) -> None:
        for transition in (
            ReleaseTransitionKind.SAME,
            ReleaseTransitionKind.DOWNGRADE,
            ReleaseTransitionKind.UNKNOWN,
        ):
            assessed = assess_unattended_upgrade(
                policy(),
                build_upgrade_candidate(
                    installation(),
                    observation(),
                    resolution(),
                    transition=transition,
                ),
                now=NOW + timedelta(minutes=2),
            )
            self.assertEqual(
                assessed.disposition,
                UpgradePolicyAssessmentDisposition.BLOCKED,
            )
            self.assertIn("transition_not_upgrade", assessed.reason_codes)

    def test_candidate_rejects_a_resolution_for_another_observation(self) -> None:
        mismatched = resolution()
        object.__setattr__(
            mismatched,
            "installation_observation_fingerprint",
            DIGEST_A,
        )

        with self.assertRaisesRegex(ValueError, "observation"):
            build_upgrade_candidate(
                installation(),
                observation(),
                mismatched,
                transition=ReleaseTransitionKind.UPGRADE,
            )

    def test_data_bearing_upgrade_requires_exact_fresh_recovery(self) -> None:
        selected = candidate(application="hindsight")

        missing = assess_unattended_upgrade(
            policy(application="hindsight", data_bearing=True),
            selected,
            now=NOW + timedelta(minutes=4),
        )
        self.assertEqual(
            missing.disposition,
            UpgradePolicyAssessmentDisposition.BLOCKED,
        )
        self.assertIn("recovery_required", missing.reason_codes)

        qualification, backup = recovery(selected=selected)
        accepted = assess_unattended_upgrade(
            policy(application="hindsight", data_bearing=True),
            selected,
            now=NOW + timedelta(minutes=4),
            recovery=qualification,
            backup=backup,
        )
        self.assertEqual(
            accepted.disposition,
            UpgradePolicyAssessmentDisposition.APPROVAL_REQUIRED,
        )

    def test_stale_or_reused_recovery_is_blocked(self) -> None:
        selected = candidate(application="hindsight")
        for recovery_candidate, stale, reason in (
            (selected, True, "backup_stale"),
            (candidate(), False, "recovery_candidate_mismatch"),
        ):
            qualification, backup = recovery(selected=recovery_candidate, stale=stale)
            assessed = assess_unattended_upgrade(
                policy(application="hindsight", data_bearing=True),
                selected,
                now=NOW + timedelta(minutes=4),
                recovery=qualification,
                backup=backup,
            )
            self.assertEqual(
                assessed.disposition,
                UpgradePolicyAssessmentDisposition.BLOCKED,
            )
            self.assertIn(reason, assessed.reason_codes)

    def test_non_data_bearing_policy_rejects_unexpected_recovery(self) -> None:
        qualification, backup = recovery(selected=candidate())

        assessed = assess_unattended_upgrade(
            policy(),
            candidate(),
            now=NOW + timedelta(minutes=4),
            recovery=qualification,
            backup=backup,
        )

        self.assertIn("recovery_not_applicable", assessed.reason_codes)

    def test_backup_cannot_be_repackaged_for_another_target_resolution(self) -> None:
        first = candidate(application="hindsight")
        second_resolution = replace(
            resolution(application="hindsight"),
            exact_release="1.2.0",
            artifact_coordinate="owner:hindsight@1.2.0",
            artifact_digest=DIGEST_C,
        )
        second = build_upgrade_candidate(
            installation(application="hindsight"),
            observation(application="hindsight"),
            second_resolution,
            transition=ReleaseTransitionKind.UPGRADE,
        )
        _first_qualification, first_backup = recovery(selected=first)
        repackaged, _same_backup = recovery(
            selected=second,
            backup_override=first_backup,
        )

        assessed = assess_unattended_upgrade(
            policy(application="hindsight", data_bearing=True),
            second,
            now=NOW + timedelta(minutes=4),
            recovery=repackaged,
            backup=first_backup,
        )

        self.assertEqual(
            assessed.disposition,
            UpgradePolicyAssessmentDisposition.BLOCKED,
        )
        self.assertIn("backup_mismatch", assessed.reason_codes)

    def test_failed_restore_or_recall_is_blocked(self) -> None:
        selected = candidate(application="hindsight")
        qualification, backup = recovery(selected=selected)
        failed_restore = replace(qualification.isolated_restore, successful=False)
        failed_recall = replace(qualification.representative_recall, successful=False)

        for changed, reason in (
            (replace(qualification, isolated_restore=failed_restore), "restore_failed"),
            (
                replace(qualification, representative_recall=failed_recall),
                "recall_failed",
            ),
        ):
            assessed = assess_unattended_upgrade(
                policy(application="hindsight", data_bearing=True),
                selected,
                now=NOW + timedelta(minutes=4),
                recovery=changed,
                backup=backup,
            )
            self.assertEqual(
                assessed.disposition,
                UpgradePolicyAssessmentDisposition.BLOCKED,
            )
            self.assertIn(reason, assessed.reason_codes)

    def test_assessment_binds_the_exact_recovery_evidence_set(self) -> None:
        selected = candidate(application="hindsight")
        first_recovery, first_backup = recovery(selected=selected)
        second_backup = replace(
            first_backup,
            backup_identity="backup:hindsight:second",
            artifact_digest=DIGEST_C,
        )
        second_recovery, _ = recovery(
            selected=selected,
            backup_override=second_backup,
        )

        first = assess_unattended_upgrade(
            policy(application="hindsight", data_bearing=True),
            selected,
            now=NOW + timedelta(minutes=4),
            recovery=first_recovery,
            backup=first_backup,
        )
        second = assess_unattended_upgrade(
            policy(application="hindsight", data_bearing=True),
            selected,
            now=NOW + timedelta(minutes=4),
            recovery=second_recovery,
            backup=second_backup,
        )

        self.assertNotEqual(
            first.validated_backup_fingerprint,
            second.validated_backup_fingerprint,
        )
        self.assertNotEqual(
            first.recovery_qualification_fingerprint,
            second.recovery_qualification_fingerprint,
        )
        self.assertNotEqual(first.fingerprint, second.fingerprint)


class RemovalPlanTests(unittest.TestCase):
    def test_removal_plan_requires_unique_exact_installation_targets(self) -> None:
        observed = observation()
        target = ExactRemovalTarget(
            application_identity=observed.application_identity,
            installation_identity=observed.installation_identity,
            owner_identity=observed.owner_identity,
            owner_installation_identity=observed.owner_installation_identity,
            installation_observation_fingerprint=observed.fingerprint,
        )
        plan = RemovalPlan(plan_identity="removal-plan:one", targets=(target,))

        self.assertTrue(plan.fingerprint.startswith("sha256:"))
        with self.assertRaisesRegex(ValueError, "unique"):
            RemovalPlan(
                plan_identity="removal-plan:duplicate",
                targets=(target, target),
            )


if __name__ == "__main__":
    unittest.main()
