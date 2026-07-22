import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path

from mastic.application.external_application_lifecycle import (
    OwnerUpgradeAction,
    OwnerUpgradePreview,
    VerifiedArtifact,
    VerifiedArtifactClosure,
)
from mastic.application.dispatch import ApplicationError
from mastic.application.owner_reconciliation import (
    authorize_owner_reconciliation,
    classify_release_transition,
)
from mastic.domain.application_lifecycle import ReleaseTransitionKind
from mastic.domain.external_applications import (
    ExternalApplicationInstallation,
    ReleaseIntent,
)
from mastic.infrastructure.planning_owner_upgrade_authorization import (
    TrustedPlanningPolicy,
)
from mastic.infrastructure.planning_record_authority import (
    LocalGrantReceiptIssuer,
    LocalGrantReceiptVerifier,
)
from mastic.infrastructure.planning_record_repository import PlanningRecordRepository
from mastic.infrastructure.state_store import OperationalStateStore
from mastic.infrastructure.codex_owner_reconciliation import (
    DaemonCodexOwnerReconciliation,
    LocalCodexOwnerReconciliation,
)
from mastic.infrastructure.owner_reconciliation_store import (
    OwnerReconciliationStore,
)
from mastic.infrastructure.owner_command_tracker import OwnerCommandStatus

_prepared = import_module("tests.mastic.test_owner_reconciliation_store_v1")._prepared


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64


class OwnerReconciliationTests(unittest.TestCase):
    def test_release_transition_never_treats_same_or_older_as_upgrade(self) -> None:
        self.assertIs(
            classify_release_transition("0.144.5", "0.150.0"),
            ReleaseTransitionKind.UPGRADE,
        )
        self.assertIs(
            classify_release_transition("0.150.0", "0.150.0"),
            ReleaseTransitionKind.SAME,
        )
        self.assertIs(
            classify_release_transition("0.150.0", "0.144.5"),
            ReleaseTransitionKind.DOWNGRADE,
        )
        self.assertIs(
            classify_release_transition("1.0.0-beta.10", "1.0.0-beta.2"),
            ReleaseTransitionKind.UNKNOWN,
        )
        self.assertIs(
            classify_release_transition("1.0.0-beta.10", "1.0.0-beta.10"),
            ReleaseTransitionKind.SAME,
        )
        self.assertIs(
            classify_release_transition("1.0.0-rc.1", "1.0.0"),
            ReleaseTransitionKind.UPGRADE,
        )
        self.assertIs(
            classify_release_transition("1.0.0", "1.0.0-rc.1"),
            ReleaseTransitionKind.DOWNGRADE,
        )
        self.assertIs(
            classify_release_transition("1.0.0", "1.1.0-rc.1"),
            ReleaseTransitionKind.UPGRADE,
        )
        self.assertIs(
            classify_release_transition("1.1.0", "1.0.0-rc.1"),
            ReleaseTransitionKind.DOWNGRADE,
        )
        self.assertIs(
            classify_release_transition("1.0.0-rc.1+build.1", "1.0.0-rc.1+build.2"),
            ReleaseTransitionKind.SAME,
        )

    def test_authorization_persists_exact_authenticated_current_plan(self) -> None:
        now = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)
        instants = iter((now, now + timedelta(seconds=1), now + timedelta(seconds=2)))

        def clock() -> datetime:
            return next(instants)

        selected = ExternalApplicationInstallation(
            application_identity="external-application:codex",
            installation_identity="application-installation:codex:vite",
            owner_identity="vite-plus/npm-global",
            release_intent=ReleaseIntent.current(channel="npm:latest"),
            platform="darwin",
            architecture="arm64",
        )
        closure = VerifiedArtifactClosure(
            application_identity=selected.application_identity,
            exact_release="0.150.0",
            artifacts=(
                VerifiedArtifact(
                    role="primary",
                    package_identity="@openai/codex",
                    exact_release="0.150.0",
                    coordinate="npm:@openai/codex@0.150.0",
                    archive_digest="sha512:" + "a" * 128,
                    installed_payload_digest=SHA_C,
                    staged_path=Path("/private/tmp/codex-stage/codex.tgz"),
                ),
                VerifiedArtifact(
                    role="platform",
                    package_identity="@openai/codex-darwin-arm64",
                    exact_release="0.150.0-darwin-arm64",
                    coordinate="npm:@openai/codex-darwin-arm64@0.150.0",
                    archive_digest="sha512:" + "b" * 128,
                    installed_payload_digest=SHA_D,
                    staged_path=Path("/private/tmp/codex-stage/platform.tgz"),
                ),
            ),
            staging_directory=Path("/private/tmp/codex-stage"),
            cache_directory=Path("/private/tmp/codex-stage/cache"),
        )
        action = OwnerUpgradeAction(
            owner_identity=selected.owner_identity,
            action_kind="npm-global-install-verified-closure",
            argv=("/Users/test/.vite-plus/bin/npm", "install"),
            cwd=closure.staging_directory,
            environment=(("HOME", "/Users/test"),),
            target_release="0.150.0",
            artifact_closure_fingerprint=closure.fingerprint,
        )
        preview = OwnerUpgradePreview(
            application_identity=selected.application_identity,
            installation_identity=selected.installation_identity,
            plan_purpose="reconciliation",
            source_observation_fingerprint=SHA_A,
            source_state_fingerprint=SHA_B,
            owner_identity=selected.owner_identity,
            owner_installation_identity=SHA_C,
            owner_runtime_identity="node:24.18.0",
            release_channel="npm:latest",
            platform="darwin",
            architecture="arm64",
            source_release="0.144.5",
            target_release="0.150.0",
            target_artifact_digest="sha512:" + "a" * 128,
            resolved_target_fingerprint=SHA_C,
            candidate_fingerprint=SHA_D,
            policy_assessment_fingerprint=SHA_A,
            artifact_closure_fingerprint=closure.fingerprint,
            rollback_source_release="0.144.5",
            action=action,
        )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = OperationalStateStore(root / "state.sqlite3")
            repository = PlanningRecordRepository(
                state, LocalGrantReceiptVerifier(root / "grant.key", expected_uid=501)
            )
            issuer = LocalGrantReceiptIssuer(
                root / "grant.key", expected_uid=501, clock=clock
            )

            authorized, trusted = authorize_owner_reconciliation(
                selected,
                preview,
                closure,
                repository=repository,
                issuer=issuer,
                uid=501,
                clock=clock,
            )

            plan = repository.plan(authorized.plan_identity)
            approval = repository.approval(authorized.approval_identity)
            assessment = repository.assessment(authorized.assessment_identity)
            self.assertIsNotNone(plan)
            self.assertIsNotNone(approval)
            self.assertIsNotNone(assessment)
            self.assertEqual(plan.created_at, "2026-07-21T08:00:00Z")
            self.assertEqual(approval.granted_at, "2026-07-21T08:00:01Z")
            self.assertEqual(assessment.evaluated_at, "2026-07-21T08:00:02Z")
            self.assertEqual(
                plan.owner_upgrade_mutation.request.preview_fingerprint,
                preview.fingerprint,
            )
            self.assertEqual(
                repository.current(plan.scope.identity).plan_identity,
                plan.plan_identity,
            )
            self.assertIsInstance(trusted, TrustedPlanningPolicy)
            self.assertTrue(trusted.accepts(assessment))

    def test_authorization_rejects_invalid_assessment_clock_without_records(
        self,
    ) -> None:
        now = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)
        instants = iter((now, now.replace(tzinfo=None)))

        def clock() -> datetime:
            return next(instants)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            prepared = _prepared(root / "prepared")
            state = OperationalStateStore(root / "state.sqlite3")
            repository = PlanningRecordRepository(
                state, LocalGrantReceiptVerifier(root / "grant.key", expected_uid=501)
            )
            issuer = LocalGrantReceiptIssuer(
                root / "grant.key",
                expected_uid=501,
                clock=lambda: now + timedelta(seconds=1),
            )

            with self.assertRaisesRegex(ValueError, "clock must be timezone-aware"):
                authorize_owner_reconciliation(
                    prepared.selected,
                    prepared.preview,
                    prepared.closure,
                    repository=repository,
                    issuer=issuer,
                    uid=501,
                    clock=clock,
                )

            self.assertEqual(state.snapshots(), ())

    def test_local_confirmation_mints_authority_and_delegates_only_retained_identity(
        self,
    ) -> None:
        now = datetime(2026, 7, 21, 8, 0, 3, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = OperationalStateStore(root / "state.sqlite3")
            store = OwnerReconciliationStore(state, staging_root=root / "prepared")
            pending = replace(_prepared(root / "prepared"), authorization=None)
            pending_identity = store.put(pending)
            planning = PlanningRecordRepository(
                state, LocalGrantReceiptVerifier(root / "grant.key", expected_uid=501)
            )
            remote = RecordingOwner()
            local = LocalCodexOwnerReconciliation(
                discovery=Unused(),
                current=Unused(),
                closure_materializer=Unused(),
                lifecycle=Unused(),
                store=store,
                planning=planning,
                issuer=LocalGrantReceiptIssuer(
                    root / "grant.key", expected_uid=501, clock=lambda: now
                ),
                remote=remote,
                uid=501,
                clock=lambda: now,
            )

            result = local.execute(
                "application.upgrade",
                {
                    "application": "codex",
                    "confirmed": True,
                    "preview_fingerprint": pending_identity,
                },
            )

            authorized_identity = result["prepared_reconciliation_identity"]
            authorized = store.load(authorized_identity)
            self.assertIsNotNone(authorized.authorization)
            self.assertEqual(
                remote.calls,
                [
                    (
                        "application.upgrade",
                        {
                            "application": "codex",
                            "prepared_reconciliation": authorized_identity,
                            "operation_id": authorized_identity,
                        },
                    )
                ],
            )

    def test_daemon_restart_converges_verified_target_after_resolution_expiry(
        self,
    ) -> None:
        now = datetime(2026, 7, 21, 8, 0, 3, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = OperationalStateStore(root / "state.sqlite3")
            store = OwnerReconciliationStore(state, staging_root=root / "prepared")
            pending = replace(_prepared(root / "prepared"), authorization=None)
            planning = PlanningRecordRepository(
                state, LocalGrantReceiptVerifier(root / "grant.key", expected_uid=501)
            )
            authorization, _trusted = authorize_owner_reconciliation(
                pending.selected,
                pending.preview,
                pending.closure,
                repository=planning,
                issuer=LocalGrantReceiptIssuer(
                    root / "grant.key", expected_uid=501, clock=lambda: now
                ),
                uid=501,
                clock=lambda: now,
            )
            authorized = replace(pending, authorization=authorization)
            identity = store.put(authorized)
            target = replace(
                pending.observation,
                installed_release=pending.preview.target_release,
                installed_artifact_digest=SHA_D,
                observed_at=now,
            )
            lifecycle = VerifyingLifecycle()
            releaser = RecordingReleaser()
            daemon = DaemonCodexOwnerReconciliation(
                discovery=StaticDiscovery(target),
                current=Unused(),
                lifecycle=lifecycle,
                artifact_verifier=VerifyingArtifacts(),
                store=OwnerReconciliationStore(
                    OperationalStateStore(root / "state.sqlite3"),
                    staging_root=root / "prepared",
                ),
                planning=PlanningRecordRepository(
                    OperationalStateStore(root / "state.sqlite3"),
                    LocalGrantReceiptVerifier(root / "grant.key", expected_uid=501),
                ),
                owner_commands=StaticCommandTracker(OwnerCommandStatus("absent")),
                closure_releaser=releaser,
                uid=501,
                transition=Unused(),
                clock=lambda: authorized.resolution.expires_at,
            )

            result = daemon.execute(
                "application.upgrade",
                {"application": "codex", "prepared_reconciliation": identity},
            )

            self.assertEqual(result["reason_code"], "already_converged")
            self.assertFalse(result["owner_mutation_attempted"])
            self.assertEqual(lifecycle.apply_calls, 0)
            self.assertEqual(releaser.closures, [authorized.closure])

    def test_daemon_rejects_expired_retained_current_resolution(self) -> None:
        now = datetime(2026, 7, 21, 8, 0, 3, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = OperationalStateStore(root / "state.sqlite3")
            store = OwnerReconciliationStore(state, staging_root=root / "prepared")
            pending = replace(_prepared(root / "prepared"), authorization=None)
            planning = PlanningRecordRepository(
                state, LocalGrantReceiptVerifier(root / "grant.key", expected_uid=501)
            )
            authorization, _trusted = authorize_owner_reconciliation(
                pending.selected,
                pending.preview,
                pending.closure,
                repository=planning,
                issuer=LocalGrantReceiptIssuer(
                    root / "grant.key", expected_uid=501, clock=lambda: now
                ),
                uid=501,
                clock=lambda: now,
            )
            authorized = replace(pending, authorization=authorization)
            identity = store.put(authorized)
            releaser = RecordingReleaser()
            daemon = DaemonCodexOwnerReconciliation(
                discovery=Unused(),
                current=Unused(),
                lifecycle=Unused(),
                artifact_verifier=Unused(),
                store=store,
                planning=planning,
                owner_commands=StaticCommandTracker(OwnerCommandStatus("absent")),
                closure_releaser=releaser,
                uid=501,
                transition=Unused(),
                clock=lambda: authorized.resolution.expires_at,
            )

            with self.assertRaisesRegex(
                ApplicationError, "retained Codex current-release resolution expired"
            ) as raised:
                daemon.execute(
                    "application.upgrade",
                    {"application": "codex", "prepared_reconciliation": identity},
                )

            self.assertEqual(raised.exception.code, "stale_resolution")
            self.assertEqual(releaser.closures, [authorized.closure])

    def test_daemon_restart_blocks_duplicate_live_owner_mutation(self) -> None:
        now = datetime(2026, 7, 21, 8, 0, 3, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = OperationalStateStore(root / "state.sqlite3")
            store = OwnerReconciliationStore(state, staging_root=root / "prepared")
            pending = replace(_prepared(root / "prepared"), authorization=None)
            planning = PlanningRecordRepository(
                state, LocalGrantReceiptVerifier(root / "grant.key", expected_uid=501)
            )
            authorization, _trusted = authorize_owner_reconciliation(
                pending.selected,
                pending.preview,
                pending.closure,
                repository=planning,
                issuer=LocalGrantReceiptIssuer(
                    root / "grant.key", expected_uid=501, clock=lambda: now
                ),
                uid=501,
                clock=lambda: now,
            )
            identity = store.put(replace(pending, authorization=authorization))
            lifecycle = VerifyingLifecycle()
            releaser = RecordingReleaser()
            daemon = DaemonCodexOwnerReconciliation(
                discovery=Unused(),
                current=Unused(),
                lifecycle=lifecycle,
                artifact_verifier=Unused(),
                store=store,
                planning=planning,
                owner_commands=StaticCommandTracker(
                    OwnerCommandStatus("matching_live")
                ),
                closure_releaser=releaser,
                uid=501,
                transition=Unused(),
                clock=lambda: now,
            )

            result = daemon.execute(
                "application.upgrade",
                {"application": "codex", "prepared_reconciliation": identity},
            )

            self.assertEqual(result["mutation_outcome"], "not_attempted")
            self.assertEqual(result["reason_code"], "owner_mutation_in_progress")
            self.assertEqual(result["owner_command_state"], "matching_live")
            self.assertFalse(result["owner_mutation_attempted"])
            self.assertEqual(lifecycle.apply_calls, 0)
            self.assertEqual(releaser.closures, [])

    def test_daemon_releases_closure_after_conclusive_pre_command_failure(self) -> None:
        now = datetime(2026, 7, 21, 8, 0, 3, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = OperationalStateStore(root / "state.sqlite3")
            store = OwnerReconciliationStore(state, staging_root=root / "prepared")
            pending = replace(_prepared(root / "prepared"), authorization=None)
            planning = PlanningRecordRepository(
                state, LocalGrantReceiptVerifier(root / "grant.key", expected_uid=501)
            )
            authorization, _trusted = authorize_owner_reconciliation(
                pending.selected,
                pending.preview,
                pending.closure,
                repository=planning,
                issuer=LocalGrantReceiptIssuer(
                    root / "grant.key", expected_uid=501, clock=lambda: now
                ),
                uid=501,
                clock=lambda: now,
            )
            authorized = replace(pending, authorization=authorization)
            identity = store.put(authorized)
            releaser = RecordingReleaser()
            daemon = DaemonCodexOwnerReconciliation(
                discovery=StaticDiscovery(authorized.observation),
                current=StaticCurrent(authorized.resolution),
                lifecycle=ExceptionalLifecycle(),
                artifact_verifier=VerifyingArtifacts(),
                store=store,
                planning=planning,
                owner_commands=StaticCommandTracker(OwnerCommandStatus("absent")),
                closure_releaser=releaser,
                uid=501,
                transition=lambda _identity: nullcontext(),
                clock=lambda: now,
            )

            with self.assertRaisesRegex(RuntimeError, "unexpected lifecycle failure"):
                daemon.execute(
                    "application.upgrade",
                    {"application": "codex", "prepared_reconciliation": identity},
                )

            self.assertEqual(releaser.closures, [authorized.closure])

    def test_daemon_releases_closure_when_exceptional_command_left_source_unchanged(
        self,
    ) -> None:
        now = datetime(2026, 7, 21, 8, 0, 3, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = OperationalStateStore(root / "state.sqlite3")
            store = OwnerReconciliationStore(state, staging_root=root / "prepared")
            pending = replace(_prepared(root / "prepared"), authorization=None)
            planning = PlanningRecordRepository(
                state, LocalGrantReceiptVerifier(root / "grant.key", expected_uid=501)
            )
            authorization, _trusted = authorize_owner_reconciliation(
                pending.selected,
                pending.preview,
                pending.closure,
                repository=planning,
                issuer=LocalGrantReceiptIssuer(
                    root / "grant.key", expected_uid=501, clock=lambda: now
                ),
                uid=501,
                clock=lambda: now,
            )
            authorized = replace(pending, authorization=authorization)
            identity = store.put(authorized)
            releaser = RecordingReleaser()
            daemon = DaemonCodexOwnerReconciliation(
                discovery=StaticDiscovery(authorized.observation),
                current=StaticCurrent(authorized.resolution),
                lifecycle=ExceptionalLifecycle(),
                artifact_verifier=VerifyingArtifacts(),
                store=store,
                planning=planning,
                owner_commands=SequencedCommandTracker(
                    OwnerCommandStatus("absent"), OwnerCommandStatus("completed")
                ),
                closure_releaser=releaser,
                uid=501,
                transition=lambda _identity: nullcontext(),
                clock=lambda: now,
            )

            with self.assertRaisesRegex(RuntimeError, "unexpected lifecycle failure"):
                daemon.execute(
                    "application.upgrade",
                    {"application": "codex", "prepared_reconciliation": identity},
                )

            self.assertEqual(releaser.closures, [authorized.closure])

    def test_daemon_closes_out_exceptional_command_that_converged(self) -> None:
        now = datetime(2026, 7, 21, 8, 0, 3, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = OperationalStateStore(root / "state.sqlite3")
            store = OwnerReconciliationStore(state, staging_root=root / "prepared")
            pending = replace(_prepared(root / "prepared"), authorization=None)
            planning = PlanningRecordRepository(
                state, LocalGrantReceiptVerifier(root / "grant.key", expected_uid=501)
            )
            authorization, _trusted = authorize_owner_reconciliation(
                pending.selected,
                pending.preview,
                pending.closure,
                repository=planning,
                issuer=LocalGrantReceiptIssuer(
                    root / "grant.key", expected_uid=501, clock=lambda: now
                ),
                uid=501,
                clock=lambda: now,
            )
            authorized = replace(pending, authorization=authorization)
            identity = store.put(authorized)
            target = replace(
                authorized.observation,
                installed_release=authorized.preview.target_release,
                installed_artifact_digest=SHA_D,
                observed_at=now,
            )
            releaser = RecordingReleaser()
            daemon = DaemonCodexOwnerReconciliation(
                discovery=SequencedDiscovery(
                    authorized.observation,
                    authorized.observation,
                    authorized.observation,
                    target,
                ),
                current=StaticCurrent(authorized.resolution),
                lifecycle=ExceptionalLifecycle(),
                artifact_verifier=VerifyingArtifacts(),
                store=store,
                planning=planning,
                owner_commands=SequencedCommandTracker(
                    OwnerCommandStatus("absent"), OwnerCommandStatus("completed")
                ),
                closure_releaser=releaser,
                uid=501,
                transition=lambda _identity: nullcontext(),
                clock=lambda: now,
            )

            result = daemon.execute(
                "application.upgrade",
                {"application": "codex", "prepared_reconciliation": identity},
            )

            self.assertEqual(result["mutation_outcome"], "verified")
            self.assertEqual(result["reason_code"], "converged_after_owner_exception")
            self.assertEqual(result["artifact_cleanup_outcome"], "verified")
            self.assertEqual(result["plan_follow_up"], "none")
            self.assertTrue(result["owner_mutation_attempted"])
            self.assertEqual(releaser.closures, [authorized.closure])

    def test_daemon_retains_closure_when_exceptional_command_left_drift(self) -> None:
        now = datetime(2026, 7, 21, 8, 0, 3, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = OperationalStateStore(root / "state.sqlite3")
            store = OwnerReconciliationStore(state, staging_root=root / "prepared")
            pending = replace(_prepared(root / "prepared"), authorization=None)
            planning = PlanningRecordRepository(
                state, LocalGrantReceiptVerifier(root / "grant.key", expected_uid=501)
            )
            authorization, _trusted = authorize_owner_reconciliation(
                pending.selected,
                pending.preview,
                pending.closure,
                repository=planning,
                issuer=LocalGrantReceiptIssuer(
                    root / "grant.key", expected_uid=501, clock=lambda: now
                ),
                uid=501,
                clock=lambda: now,
            )
            authorized = replace(pending, authorization=authorization)
            identity = store.put(authorized)
            drifted = replace(
                authorized.observation,
                owner_runtime_identity="node:24.19.0",
                observed_at=now,
            )
            releaser = RecordingReleaser()
            daemon = DaemonCodexOwnerReconciliation(
                discovery=SequencedDiscovery(
                    authorized.observation,
                    authorized.observation,
                    authorized.observation,
                    drifted,
                    drifted,
                ),
                current=StaticCurrent(authorized.resolution),
                lifecycle=ExceptionalLifecycle(),
                artifact_verifier=VerifyingArtifacts(),
                store=store,
                planning=planning,
                owner_commands=SequencedCommandTracker(
                    OwnerCommandStatus("absent"), OwnerCommandStatus("completed")
                ),
                closure_releaser=releaser,
                uid=501,
                transition=lambda _identity: nullcontext(),
                clock=lambda: now,
            )

            with self.assertRaisesRegex(RuntimeError, "unexpected lifecycle failure"):
                daemon.execute(
                    "application.upgrade",
                    {"application": "codex", "prepared_reconciliation": identity},
                )

            self.assertEqual(releaser.closures, [])

    def test_ended_owner_command_with_unproven_post_state_remains_unknown(self) -> None:
        now = datetime(2026, 7, 21, 8, 0, 3, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = OperationalStateStore(root / "state.sqlite3")
            store = OwnerReconciliationStore(state, staging_root=root / "prepared")
            pending = replace(_prepared(root / "prepared"), authorization=None)
            planning = PlanningRecordRepository(
                state, LocalGrantReceiptVerifier(root / "grant.key", expected_uid=501)
            )
            authorization, _trusted = authorize_owner_reconciliation(
                pending.selected,
                pending.preview,
                pending.closure,
                repository=planning,
                issuer=LocalGrantReceiptIssuer(
                    root / "grant.key", expected_uid=501, clock=lambda: now
                ),
                uid=501,
                clock=lambda: now,
            )
            identity = store.put(replace(pending, authorization=authorization))
            drifted = replace(
                pending.observation,
                owner_runtime_identity="node:24.19.0",
                observed_at=now,
            )
            releaser = RecordingReleaser()
            lifecycle = VerifyingLifecycle()
            daemon = DaemonCodexOwnerReconciliation(
                discovery=StaticDiscovery(drifted),
                current=Unused(),
                lifecycle=lifecycle,
                artifact_verifier=VerifyingArtifacts(),
                store=store,
                planning=planning,
                owner_commands=StaticCommandTracker(OwnerCommandStatus("completed")),
                closure_releaser=releaser,
                uid=501,
                transition=Unused(),
                clock=lambda: now,
            )

            result = daemon.execute(
                "application.upgrade",
                {"application": "codex", "prepared_reconciliation": identity},
            )

            self.assertEqual(result["mutation_outcome"], "unknown")
            self.assertEqual(
                result["reason_code"], "prior_owner_command_outcome_unknown"
            )
            self.assertEqual(result["artifact_cleanup_outcome"], "required")
            self.assertEqual(result["plan_follow_up"], "successor_required")
            self.assertEqual(lifecycle.apply_calls, 0)
            self.assertEqual(releaser.closures, [])


class Unused:
    def __getattr__(self, name):
        def unexpected(*_arguments, **_parameters):
            raise AssertionError(f"unexpected dependency call: {name}")

        return unexpected


class RecordingOwner:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, operation, parameters):
        self.calls.append((operation, dict(parameters)))
        return {"state": "delegated"}


class StaticDiscovery:
    def __init__(self, observation) -> None:
        self.observation = observation

    def discover(self, **_parameters):
        return self.observation


class SequencedDiscovery:
    def __init__(self, *observations) -> None:
        self.observations = iter(observations)

    def discover(self, **_parameters):
        return next(self.observations)


class StaticCurrent:
    def __init__(self, resolution) -> None:
        self.resolution = resolution

    def resolve(self, *_arguments):
        return self.resolution


class VerifyingLifecycle:
    def __init__(self) -> None:
        self.apply_calls = 0

    def verify_authorization_material(self, *_arguments):
        return True

    def apply_exact(self, _request):
        self.apply_calls += 1
        raise AssertionError("already-converged retry must not invoke the owner")


class ExceptionalLifecycle(VerifyingLifecycle):
    def apply_exact(self, _request):
        self.apply_calls += 1
        raise RuntimeError("unexpected lifecycle failure")


class VerifyingArtifacts:
    def verify_staged(self, _closure):
        return None

    def verify_installed(self, _closure, _observation):
        return None


class StaticCommandTracker:
    def __init__(self, status: OwnerCommandStatus) -> None:
        self.status = status

    def inspect(self, *_arguments, **_parameters):
        return self.status


class SequencedCommandTracker:
    def __init__(self, *statuses: OwnerCommandStatus) -> None:
        self.statuses = iter(statuses)

    def inspect(self, *_arguments, **_parameters):
        return next(self.statuses)


class RecordingReleaser:
    def __init__(self) -> None:
        self.closures = []

    def release(self, closure):
        self.closures.append(closure)


if __name__ == "__main__":
    unittest.main()
