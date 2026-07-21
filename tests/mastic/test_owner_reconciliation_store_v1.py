import hashlib
import json
import sqlite3
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from mastic.application.application_upgrade_policy import (
    assess_unattended_upgrade,
    build_upgrade_candidate,
)
from mastic.application.external_application_lifecycle import (
    AuthorizedOwnerUpgrade,
    OwnerUpgradeAction,
    VerifiedArtifact,
    VerifiedArtifactClosure,
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
from mastic.infrastructure.owner_reconciliation_store import (
    OwnerReconciliationStore,
    PreparedOwnerReconciliation,
    PreparedOwnerReconciliationError,
)
from mastic.infrastructure.state_store import OperationalStateStore


NOW = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64
SHA_E = "sha256:" + "e" * 64
SHA_F = "sha256:" + "f" * 64


def _digest(algorithm: str, payload: bytes) -> str:
    return f"{algorithm}:" + hashlib.new(algorithm, payload).hexdigest()


def _prepared(stage_root: Path) -> PreparedOwnerReconciliation:
    staging = stage_root / "codex-owner-upgrade"
    cache = staging / "npm-cache"
    cache.mkdir(parents=True)
    primary_path = staging / "codex.tgz"
    platform_path = staging / "codex-darwin-arm64.tgz"
    primary_bytes = b"verified primary archive"
    platform_bytes = b"verified platform archive"
    primary_path.write_bytes(primary_bytes)
    platform_path.write_bytes(platform_bytes)

    selected = ExternalApplicationInstallation(
        application_identity="external-application:codex",
        installation_identity="application-installation:codex:vite",
        owner_identity="vite-plus/npm-global",
        release_intent=ReleaseIntent.current(channel="npm:latest"),
        platform="darwin",
        architecture="arm64",
    )
    active = "/Users/test/.vite-plus/bin/codex"
    observed = InstallationObservation(
        application_identity=selected.application_identity,
        installation_identity=selected.installation_identity,
        owner_identity=selected.owner_identity,
        owner_installation_identity=SHA_A,
        owner_runtime_identity="node:24.18.0",
        release_channel=selected.release_intent.channel,
        platform=selected.platform,
        architecture=selected.architecture,
        installed_release="0.144.5",
        installed_artifact_digest=SHA_B,
        active_invocation=active,
        reachable_invocations=(active,),
        observed_at=NOW,
    )
    current = CurrentReleaseResolution(
        installation_identity=selected.installation_identity,
        installation_observation_fingerprint=observed.fingerprint,
        owner_identity=selected.owner_identity,
        release_channel=selected.release_intent.channel,
        platform=selected.platform,
        architecture=selected.architecture,
        exact_release="0.144.6",
        artifact_coordinate="npm:@openai/codex@0.144.6",
        artifact_digest=_digest("sha512", primary_bytes),
        authority_identity="release-authority:npmjs:@openai/codex:latest",
        authority_response_digest=SHA_C,
        observed_at=NOW + timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=5),
        resolver_policy_identity="current-online:v1",
        validation_profile_identity="codex-current:v1",
    )
    candidate = build_upgrade_candidate(
        selected, observed, current, transition=ReleaseTransitionKind.UPGRADE
    )
    policy = UnattendedUpgradePolicy(
        policy_identity="unattended-upgrade:codex:v1",
        application_identity=selected.application_identity,
        owner_identity=selected.owner_identity,
        release_channel=selected.release_intent.channel,
        validation_profile_identity="codex-upgrade:v1",
        data_bearing=False,
        maximum_backup_age=timedelta(minutes=5),
    )
    assessment = assess_unattended_upgrade(
        policy, candidate, now=NOW + timedelta(seconds=2)
    )
    closure = VerifiedArtifactClosure(
        application_identity=selected.application_identity,
        exact_release=current.exact_release,
        artifacts=(
            VerifiedArtifact(
                role="primary",
                package_identity="@openai/codex",
                exact_release=current.exact_release,
                coordinate=current.artifact_coordinate,
                archive_digest=current.artifact_digest,
                installed_payload_digest=SHA_D,
                staged_path=primary_path,
            ),
            VerifiedArtifact(
                role="platform",
                package_identity="@openai/codex-darwin-arm64",
                exact_release="0.144.6-darwin-arm64",
                coordinate="npm:@openai/codex-darwin-arm64@0.144.6",
                archive_digest=_digest("sha512", platform_bytes),
                installed_payload_digest=SHA_E,
                staged_path=platform_path,
            ),
        ),
        staging_directory=staging,
        cache_directory=cache,
    )
    action = OwnerUpgradeAction(
        owner_identity=selected.owner_identity,
        action_kind="npm-global-install-verified-closure",
        argv=("/Users/test/.vite-plus/bin/vp", "env", "exec"),
        cwd=staging,
        environment=(("NO_COLOR", "1"),),
        target_release=current.exact_release,
        artifact_closure_fingerprint=closure.fingerprint,
    )
    preview = build_owner_upgrade_preview(
        candidate, assessment, current, observed, closure, action
    )
    references = AuthorizedOwnerUpgrade(
        plan_identity=SHA_D,
        approval_identity=SHA_E,
        assessment_identity=SHA_F,
        preview_fingerprint=preview.fingerprint,
    )
    return PreparedOwnerReconciliation(
        selected=selected,
        observation=observed,
        resolution=current,
        closure=closure,
        preview=preview,
        authorization=references,
    )


def _tamper(database: Path, update) -> None:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT sequence, dto_json FROM snapshots WHERE kind = ?",
            ("prepared_owner_reconciliation",),
        ).fetchone()
        assert row is not None
        dto = json.loads(row[1])
        update(dto)
        connection.execute(
            "UPDATE snapshots SET dto_json = ? WHERE sequence = ?",
            (json.dumps(dto, separators=(",", ":"), sort_keys=True), row[0]),
        )
        connection.commit()
    finally:
        connection.close()


class OwnerReconciliationStoreTests(unittest.TestCase):
    def test_round_trips_exact_preparation_across_restart(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            stage_root = root / "prepared"
            prepared = _prepared(stage_root)

            identity = OwnerReconciliationStore(
                OperationalStateStore(database), staging_root=stage_root
            ).put(prepared)
            rehydrated = OwnerReconciliationStore(
                OperationalStateStore(database), staging_root=stage_root
            ).load(identity)

            self.assertEqual(identity, prepared.identity)
            self.assertEqual(rehydrated, prepared)
            self.assertEqual(rehydrated.identity, identity)

    def test_put_is_idempotent_for_the_same_exact_preparation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            stage_root = root / "prepared"
            prepared = _prepared(stage_root)
            store = OwnerReconciliationStore(
                OperationalStateStore(database), staging_root=stage_root
            )

            self.assertEqual(store.put(prepared), store.put(prepared))
            self.assertEqual(
                len(
                    OperationalStateStore(database).snapshot_history(
                        "prepared_owner_reconciliation"
                    )
                ),
                1,
            )

    def test_round_trips_preconfirmation_preparation_without_authorization(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            stage_root = root / "prepared"
            prepared = replace(_prepared(stage_root), authorization=None)
            store = OwnerReconciliationStore(
                OperationalStateStore(database), staging_root=stage_root
            )

            identity = store.put(prepared)

            self.assertEqual(store.load(identity), prepared)
            self.assertIsNone(store.load(identity).authorization)

    def test_unknown_prepared_identity_is_absent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = OwnerReconciliationStore(
                OperationalStateStore(root / "state.sqlite3"),
                staging_root=root / "prepared",
            )

            self.assertIsNone(store.load(SHA_A))

    def test_rehydrate_fails_closed_when_a_derived_fingerprint_is_tampered(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            stage_root = root / "prepared"
            prepared = _prepared(stage_root)
            OwnerReconciliationStore(
                OperationalStateStore(database), staging_root=stage_root
            ).put(prepared)
            _tamper(
                database,
                lambda dto: dto["record"]["observation"].__setitem__(
                    "installed_release", "0.1.0"
                ),
            )

            with self.assertRaisesRegex(
                PreparedOwnerReconciliationError, "prepared_state_invalid"
            ):
                OwnerReconciliationStore(
                    OperationalStateStore(database), staging_root=stage_root
                ).load(prepared.identity)

    def test_rehydrate_fails_closed_when_staged_material_is_missing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            stage_root = root / "prepared"
            prepared = _prepared(stage_root)
            OwnerReconciliationStore(
                OperationalStateStore(database), staging_root=stage_root
            ).put(prepared)
            prepared.closure.artifacts[0].staged_path.unlink()

            with self.assertRaisesRegex(
                PreparedOwnerReconciliationError, "prepared_material_missing"
            ):
                OwnerReconciliationStore(
                    OperationalStateStore(database), staging_root=stage_root
                ).load(prepared.identity)

    def test_rehydrate_fails_closed_when_staged_archive_bytes_change(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            stage_root = root / "prepared"
            prepared = _prepared(stage_root)
            OwnerReconciliationStore(
                OperationalStateStore(database), staging_root=stage_root
            ).put(prepared)
            prepared.closure.artifacts[0].staged_path.write_bytes(b"changed")

            with self.assertRaisesRegex(
                PreparedOwnerReconciliationError, "prepared_material_changed"
            ):
                OwnerReconciliationStore(
                    OperationalStateStore(database), staging_root=stage_root
                ).load(prepared.identity)

    def test_rehydrate_fails_closed_when_persisted_paths_escape_stage_root(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            stage_root = root / "prepared"
            prepared = _prepared(stage_root)
            OwnerReconciliationStore(
                OperationalStateStore(database), staging_root=stage_root
            ).put(prepared)
            escaped = root / "escaped"
            escaped_cache = escaped / "cache"
            escaped_cache.mkdir(parents=True)
            escaped_archive = escaped / "codex.tgz"
            escaped_archive.write_bytes(b"verified primary archive")

            def escape(dto):
                closure = dto["record"]["artifact_closure"]
                closure["staging_directory"] = str(escaped)
                closure["cache_directory"] = str(escaped_cache)
                closure["artifacts"][0]["staged_path"] = str(escaped_archive)
                closure["artifacts"][1]["staged_path"] = str(
                    escaped / "codex-darwin-arm64.tgz"
                )

            _tamper(database, escape)

            with self.assertRaisesRegex(
                PreparedOwnerReconciliationError, "prepared_path_invalid"
            ):
                OwnerReconciliationStore(
                    OperationalStateStore(database), staging_root=stage_root
                ).load(prepared.identity)


if __name__ == "__main__":
    unittest.main()
