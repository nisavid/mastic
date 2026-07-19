import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from mastic.application.config_schema import validate_config
from mastic.infrastructure.config_store import ConfigStore
from mastic.infrastructure.control_protocol import MAX_FRAME_BYTES
from mastic.infrastructure.model_supply import (
    CacheDeletionPreview,
    CacheInventory,
    CachedRevision,
    ModelAlias,
    ModelInstallResult,
    ModelInstallation,
    ModelProvenance,
    ModelRevision,
    VerificationResult,
)
from mastic.infrastructure.model_intelligence import (
    EvidenceState,
    RepositoryFile,
    RuntimeCompatibility,
    TrustSignal,
)
from mastic.infrastructure.state_store import OperationalStateStore
from mastic.infrastructure.runtime_supply import (
    RuntimeCatalogue,
    RuntimeInstallation,
)
from mastic.infrastructure.supply_ports import (
    CacheMovePreview,
    ExactRevisionModelSecurity,
    ModelSecurityPolicyError,
    ModelSupplyPort,
    RuntimeSupplyPort,
    SupplyPortError,
    VerifiedCacheMover,
    inspect_adopted_snapshot,
    verify_adopted_snapshot,
)


_SHA_A = "a" * 40
_SHA_B = "b" * 40


class FakeRuntimeManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[object, ...]] = []

    def install_tested(
        self, bundle_id: str, installation_root: Path
    ) -> RuntimeInstallation:
        self.calls.append(("install_tested", bundle_id, installation_root))
        runtime = bundle_id.split("-", 1)[0]
        if bundle_id.startswith("mlx_lm"):
            runtime = "mlx_lm"
        elif bundle_id.startswith("mlx_vlm"):
            runtime = "mlx_vlm"
        version = bundle_id.removeprefix(f"{runtime}-").split("-py", 1)[0]
        installation = self._installation(
            bundle_id,
            runtime,
            version,
            "tested",
            bundle_id=bundle_id,
        )
        installation.root.mkdir(parents=True)
        return installation

    def install_custom(
        self,
        runtime: str,
        version: str,
        *,
        python: str,
        installation_root: Path,
    ) -> RuntimeInstallation:
        self.calls.append(
            ("install_custom", runtime, version, python, installation_root)
        )
        installation = self._installation(
            f"{runtime}-{version}-custom", runtime, version, "custom"
        )
        installation.root.mkdir(parents=True)
        return installation

    def adopt_custom(self, runtime: str, root: Path) -> RuntimeInstallation:
        self.calls.append(("adopt_custom", runtime, root))
        return self._installation(
            f"{runtime}-9.9-adopted", runtime, "9.9", "adopted", root=root
        )

    def _installation(
        self,
        installation_id: str,
        runtime: str,
        version: str,
        provenance: str,
        *,
        root: Path | None = None,
        bundle_id: str | None = None,
    ) -> RuntimeInstallation:
        path = root or self.root / installation_id
        return RuntimeInstallation(
            installation_id=installation_id,
            runtime=runtime,
            version=version,
            provenance=provenance,
            root=path.resolve(),
            launcher=(str(path.resolve() / "bin" / runtime), "serve"),
            capabilities=frozenset({"model", "host", "port"}),
            bundle_id=bundle_id,
        )


class FakeRuntimeFiles:
    def __init__(self) -> None:
        self.removed: list[Path] = []

    def remove(self, root: Path) -> None:
        self.removed.append(root)


@dataclass
class FakeDeletionStrategy:
    expected_freed_size: int = 1024
    executed: bool = False

    def execute(self) -> None:
        self.executed = True


class FakeModelSupply:
    def __init__(self, cache_root: Path) -> None:
        self.cache_root = cache_root
        self.calls: list[tuple[object, ...]] = []
        self.strategies: list[FakeDeletionStrategy] = []
        self.revisions: tuple[CachedRevision, ...] = ()

    def install(
        self,
        *,
        alias: str,
        repo_id: str,
        revision: str,
        offline: bool = False,
    ) -> ModelInstallResult:
        self.calls.append(("install", alias, repo_id, revision, offline))
        sha = _SHA_A if revision in {"main", _SHA_A} else _SHA_B
        model_revision = ModelRevision(repo_id, sha, revision, "hub-observed")
        snapshot = self.cache_root / sha
        snapshot.mkdir(parents=True, exist_ok=True)
        cached = CachedRevision(
            revision_id=model_revision.revision_id,
            repo_id=repo_id,
            commit_sha=sha,
            snapshot_path=snapshot,
            size_on_disk=17,
            evidence="downloaded-exact",
            complete=True,
        )
        installation = ModelInstallation(
            installation_id=model_revision.revision_id,
            revision=model_revision,
            cached_revision_id=cached.revision_id,
            snapshot_path=snapshot,
            provenance=ModelProvenance(revision, sha, "hugging-face-cache"),
        )
        self.revisions = tuple(
            item for item in self.revisions if item.commit_sha != sha
        ) + (cached,)
        return ModelInstallResult(
            model_revision,
            cached,
            installation,
            ModelAlias(alias, installation.installation_id),
            VerificationResult("complete", "cache-completeness", ()),
        )

    def resolve(
        self, repo_id: str, revision: str, *, offline: bool = False
    ) -> ModelRevision:
        sha = _SHA_A if revision in {"main", _SHA_A} else _SHA_B
        self.calls.append(("resolve", repo_id, revision, offline))
        return ModelRevision(repo_id, sha, revision, "test-resolution")

    def repair(self, installation: ModelInstallation) -> VerificationResult:
        self.calls.append(("repair", installation))
        return VerificationResult("complete", "cache-completeness", ())

    def verify(self, installation: ModelInstallation) -> VerificationResult:
        self.calls.append(("verify", installation))
        return VerificationResult("complete", "cache-completeness", ())

    def search(self, query: str, *, mode: str = "curated", limit: int = 20):
        self.calls.append(("search", query, mode, limit))
        return (query, mode, limit)

    def inventory(self) -> CacheInventory:
        self.calls.append(("inventory",))
        return CacheInventory(self.revisions, "local-observed", ())

    def preview_cache_deletion(
        self,
        commit_hashes: tuple[str, ...],
        *,
        installations: tuple[ModelInstallation, ...] = (),
    ) -> CacheDeletionPreview:
        self.calls.append(("preview_cache_deletion", commit_hashes, installations))
        blocked = tuple(
            installation.installation_id
            for installation in installations
            if installation.revision.commit_sha in commit_hashes
        )
        if blocked:
            return CacheDeletionPreview(False, commit_hashes, blocked, 0)
        strategy = FakeDeletionStrategy()
        self.strategies.append(strategy)
        return CacheDeletionPreview(
            True,
            commit_hashes,
            (),
            strategy.expected_freed_size,
            strategy,
        )


class FakeCacheMover:
    def __init__(self) -> None:
        self.previews: list[CacheMovePreview] = []
        self.executed: list[CacheMovePreview] = []
        self.before_cleanup_action = None

    def preview(self, revision: CachedRevision, destination: Path) -> CacheMovePreview:
        preview = CacheMovePreview(
            revision_id=revision.revision_id,
            source=revision.snapshot_path,
            destination=destination,
            bytes_to_copy=revision.size_on_disk,
            steps=("copy", "verify", "publish"),
        )
        self.previews.append(preview)
        return preview

    def execute(self, preview: CacheMovePreview, *, before_cleanup=None) -> Path:
        if self.before_cleanup_action is not None:
            self.before_cleanup_action()
        if preview.cleanup_source and before_cleanup is not None:
            before_cleanup()
        self.executed.append(preview)
        return preview.destination


class FakeModelIntelligence:
    def __init__(
        self,
        *signals: TrustSignal,
        compatibility: tuple[RuntimeCompatibility, ...] = (),
        repository_files: tuple[RepositoryFile, ...] = (),
    ) -> None:
        self.signals = signals or (
            TrustSignal(
                "hub_security_scan",
                "info",
                EvidenceState.OBSERVED,
                "Hub security status@test",
                "Hub scans completed with no reported file issues",
            ),
        )
        self.compatibility = compatibility
        self.repository_files = repository_files

    def inspect(self, repository: str, revision: str, **_options):
        return SimpleNamespace(
            identity=SimpleNamespace(repo_id=repository, commit_sha=revision),
            trust_signals=self.signals,
            compatibility=self.compatibility,
            repository_files=self.repository_files,
        )


class SupplyPortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ConfigStore(self.root / "config.toml", validate_config)
        self.security_state = OperationalStateStore(self.root / "state.sqlite3")
        self.security = ExactRevisionModelSecurity(
            FakeModelIntelligence(), self.security_state
        )
        self.store.import_text(
            """schema_version = 1

[gateway]
host = "127.0.0.1"
port = 8766

[runtimes]
[models]
[aliases]
[services]
[application_targets]
"""
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _mark_owned(
        self, port: RuntimeSupplyPort, installation: RuntimeInstallation
    ) -> None:
        installation.root.mkdir(parents=True, exist_ok=True)
        port.record_managed_installation(installation)

    def test_runtime_install_uses_tested_bundle_and_persists_exact_probe(self) -> None:
        manager = FakeRuntimeManager(self.root / "runtimes")
        catalogue = RuntimeCatalogue.load_builtin()
        bundle = next(
            item for item in catalogue.tested_bundles if item.runtime == "optiq"
        )
        port = RuntimeSupplyPort(
            manager,
            self.store,
            self.root / "runtimes",
            catalogue=catalogue,
        )

        result = port.execute(
            "runtime.install",
            {
                "name": "optiq",
                "channel": "tested",
                "expected_version": bundle.version,
                "expected_lock_digest": bundle.lock_sha256,
            },
        )

        installed = self.store.load().value.runtimes[result["installation_id"]]
        self.assertEqual(manager.calls[0][0], "install_tested")
        self.assertEqual(installed.definition, "optiq")
        self.assertEqual(installed.root, result["root"])
        self.assertEqual(installed.launcher, tuple(result["launcher"]))
        self.assertEqual(installed.capabilities, frozenset(result["capabilities"]))
        self.assertEqual(installed.bundle_id, result["bundle_id"])
        self.assertEqual(result["lock_sha256"], bundle.lock_sha256)
        self.assertEqual(result["preview"]["operation"], "install")

    def test_runtime_install_initializes_supported_v1_desired_state(self) -> None:
        store = ConfigStore(self.root / "fresh.toml", validate_config)
        manager = FakeRuntimeManager(self.root / "runtimes")
        port = RuntimeSupplyPort(manager, store, self.root / "runtimes")

        result = port.execute("runtime.install", {"runtime": "mlx_lm"})

        self.assertIn(result["installation_id"], store.load().value.runtimes)

    def test_runtime_install_resumes_after_config_publication_fails(self) -> None:
        manager = FakeRuntimeManager(self.root / "runtimes")
        port = RuntimeSupplyPort(manager, self.store, self.root / "runtimes")

        with (
            patch.object(
                RuntimeSupplyPort,
                "persist_runtime",
                side_effect=OSError("config unavailable"),
            ),
            self.assertRaisesRegex(OSError, "config unavailable"),
        ):
            port.execute(
                "runtime.install",
                {"runtime": "optiq", "version": "0.3", "channel": "custom"},
            )

        result = port.execute(
            "runtime.install",
            {"runtime": "optiq", "version": "0.3", "channel": "custom"},
        )

        self.assertEqual(len(manager.calls), 1)
        self.assertIn(result["installation_id"], self.store.load().value.runtimes)

    def test_runtime_update_is_side_by_side_and_switches_service_references(
        self,
    ) -> None:
        manager = FakeRuntimeManager(self.root / "runtimes")
        old = manager._installation("optiq-old", "optiq", "0.2", "tested")
        RuntimeSupplyPort.persist_runtime(self.store, old)
        # Build valid dependent desired state in one atomic import.
        self.store.import_text(
            f"""schema_version = 1
[gateway]
host = "127.0.0.1"
port = 8766
[runtimes.optiq-old]
definition = "optiq"
version = "0.2"
provenance = "tested"
root = "{old.root}"
launcher = ["{old.launcher[0]}", "serve"]
capabilities = ["host", "model", "port"]
[models.qwen]
repository = "mlx-community/Qwen"
revision = "{_SHA_A}"
[aliases.coding]
installation = "qwen"
[services.coding]
model_alias = "coding"
runtime = "optiq-old"
route = "coding"
[application_targets]
"""
        )
        port = RuntimeSupplyPort(manager, self.store, self.root / "runtimes")

        result = port.execute(
            "runtime.update",
            {"resource": "optiq-old", "version": "0.3", "python": "3.13"},
        )

        config = self.store.load().value
        self.assertIn("optiq-old", config.runtimes)
        self.assertIn("optiq-0.3-custom", config.runtimes)
        self.assertEqual(
            config.services["coding"].runtime_installation, "optiq-0.3-custom"
        )
        self.assertEqual(result["preview"]["referenced_services"], ["coding"])

    def test_runtime_update_honors_explicit_channel_contract(self) -> None:
        manager = FakeRuntimeManager(self.root / "runtimes")
        old = manager._installation("optiq-old", "optiq", "0.2", "tested")
        RuntimeSupplyPort.persist_runtime(self.store, old)
        port = RuntimeSupplyPort(manager, self.store, self.root / "runtimes")

        with self.assertRaisesRegex(SupplyPortError, "does not accept"):
            port.execute(
                "runtime.update",
                {
                    "resource": "optiq-old",
                    "channel": "tested",
                    "version": "0.3",
                },
            )
        with self.assertRaisesRegex(SupplyPortError, "requires an exact version"):
            port.execute(
                "runtime.update",
                {"resource": "optiq-old", "channel": "custom"},
            )

    def test_runtime_rollback_requires_an_explicit_target(self) -> None:
        manager = FakeRuntimeManager(self.root / "runtimes")
        current = manager._installation("optiq-current", "optiq", "0.3", "tested")
        previous = manager._installation("optiq-previous", "optiq", "0.2", "tested")
        RuntimeSupplyPort.persist_runtime(self.store, current)
        RuntimeSupplyPort.persist_runtime(self.store, previous)
        port = RuntimeSupplyPort(manager, self.store, self.root / "runtimes")

        with self.assertRaisesRegex(SupplyPortError, "target is required"):
            port.execute("runtime.rollback", {"resource": "optiq-current"})

    def test_runtime_update_and_rollback_reject_incompatible_service_options(self):
        manager = FakeRuntimeManager(self.root / "runtimes")
        current = RuntimeInstallation(
            "optiq-current",
            "optiq",
            "0.2",
            "tested",
            self.root / "runtimes/optiq-current",
            (str(self.root / "runtimes/optiq-current/bin/optiq"), "serve"),
            frozenset({"model", "host", "port", "mtp"}),
        )
        target = RuntimeInstallation(
            "optiq-target",
            "optiq",
            "0.3",
            "tested",
            self.root / "runtimes/optiq-target",
            (str(self.root / "runtimes/optiq-target/bin/optiq"), "serve"),
            frozenset({"model", "host", "port"}),
        )
        self.store.import_text(
            f'''schema_version = 1
[gateway]
[runtimes.optiq-current]
definition = "optiq"
version = "0.2"
provenance = "tested"
root = "{current.root}"
launcher = ["{current.launcher[0]}", "serve"]
capabilities = ["host", "model", "mtp", "port"]
[runtimes.optiq-target]
definition = "optiq"
version = "0.3"
provenance = "tested"
root = "{target.root}"
launcher = ["{target.launcher[0]}", "serve"]
capabilities = ["host", "model", "port"]
[models.qwen]
repository = "mlx-community/Qwen"
revision = "{_SHA_A}"
[aliases.coding]
installation = "qwen"
[services.coding]
model_alias = "coding"
runtime = "optiq-current"
route = "coding"
[services.coding.options]
mtp = true
[application_targets]
'''
        )
        port = RuntimeSupplyPort(manager, self.store, self.root / "runtimes")

        for operation in ("runtime.update", "runtime.rollback"):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(
                    SupplyPortError, "missing exact capabilities"
                ):
                    port.execute(
                        operation,
                        {"resource": "optiq-current", "target": "optiq-target"},
                    )
                self.assertEqual(
                    self.store.load().value.services["coding"].runtime_installation,
                    "optiq-current",
                )

    def test_runtime_remove_is_reference_gated_and_requires_confirmation(self) -> None:
        manager = FakeRuntimeManager(self.root / "runtimes")
        files = FakeRuntimeFiles()
        installation = manager._installation("optiq-old", "optiq", "0.2", "tested")
        RuntimeSupplyPort.persist_runtime(self.store, installation)
        port = RuntimeSupplyPort(
            manager, self.store, self.root / "runtimes", filesystem=files
        )
        self._mark_owned(port, installation)

        with self.assertRaisesRegex(PermissionError, "confirmation"):
            port.execute("runtime.remove", {"resource": "optiq-old"})

        result = port.execute(
            "runtime.remove", {"resource": "optiq-old", "confirmed": True}
        )

        self.assertTrue(result["preview"]["allowed"])
        self.assertEqual(
            files.removed,
            [installation.root.parent / ".optiq-old.removing"],
        )
        self.assertNotIn("optiq-old", self.store.load().value.runtimes)

    def test_runtime_remove_restores_environment_when_config_update_fails(
        self,
    ) -> None:
        manager = FakeRuntimeManager(self.root / "runtimes")
        files = FakeRuntimeFiles()
        installation = manager._installation("optiq-old", "optiq", "0.2", "tested")
        RuntimeSupplyPort.persist_runtime(self.store, installation)
        port = RuntimeSupplyPort(
            manager, self.store, self.root / "runtimes", filesystem=files
        )
        self._mark_owned(port, installation)

        with (
            patch.object(
                port,
                "_remove_runtime_record",
                side_effect=OSError("config unavailable"),
            ),
            self.assertRaisesRegex(OSError, "config unavailable"),
        ):
            port.execute("runtime.remove", {"resource": "optiq-old", "confirmed": True})

        self.assertTrue(installation.root.is_dir())
        self.assertFalse((installation.root.parent / ".optiq-old.removing").exists())
        self.assertIn("optiq-old", self.store.load().value.runtimes)
        self.assertEqual(files.removed, [])

    def test_runtime_remove_recovers_each_interrupted_boundary(self) -> None:
        manager = FakeRuntimeManager(self.root / "runtimes")
        installation = manager._installation("optiq-old", "optiq", "0.2", "tested")
        RuntimeSupplyPort.persist_runtime(self.store, installation)
        port = RuntimeSupplyPort(manager, self.store, self.root / "runtimes")
        self._mark_owned(port, installation)
        tombstone = installation.root.parent / ".optiq-old.removing"

        installation.root.replace(tombstone)
        RuntimeSupplyPort(manager, self.store, self.root / "runtimes")
        self.assertTrue(installation.root.is_dir())
        self.assertFalse(tombstone.exists())

        installation.root.replace(tombstone)
        port._remove_runtime_record("optiq-old")
        RuntimeSupplyPort(manager, self.store, self.root / "runtimes")
        self.assertFalse(installation.root.exists())
        self.assertFalse(tombstone.exists())

    def test_runtime_remove_recovery_never_deletes_an_unowned_tombstone(self) -> None:
        manager = FakeRuntimeManager(self.root / "runtimes")
        tombstone = self.root / "runtimes" / ".unowned.removing"
        tombstone.mkdir(parents=True)

        with self.assertRaisesRegex(SupplyPortError, "ownership marker"):
            RuntimeSupplyPort(manager, self.store, self.root / "runtimes")

        self.assertTrue(tombstone.is_dir())

    def test_runtime_remove_refuses_a_referenced_installation(self) -> None:
        manager = FakeRuntimeManager(self.root / "runtimes")
        files = FakeRuntimeFiles()
        installation = manager._installation("optiq-old", "optiq", "0.2", "tested")
        self.store.import_text(
            f"""schema_version = 1
[gateway]
[runtimes.optiq-old]
definition = "optiq"
version = "0.2"
provenance = "tested"
root = "{installation.root}"
launcher = ["{installation.launcher[0]}", "serve"]
capabilities = []
[models.qwen]
repository = "mlx-community/Qwen"
revision = "{_SHA_A}"
[aliases.coding]
installation = "qwen"
[services.coding]
model_alias = "coding"
runtime = "optiq-old"
route = "coding"
[application_targets]
"""
        )
        port = RuntimeSupplyPort(
            manager, self.store, self.root / "runtimes", filesystem=files
        )
        self._mark_owned(port, installation)

        with self.assertRaisesRegex(SupplyPortError, "coding"):
            port.execute("runtime.remove", {"resource": "optiq-old", "confirmed": True})

        self.assertEqual(files.removed, [])
        self.assertIn("optiq-old", self.store.load().value.runtimes)

    def test_adopted_runtime_removal_unregisters_without_deleting_external_root(
        self,
    ) -> None:
        manager = FakeRuntimeManager(self.root / "runtimes")
        files = FakeRuntimeFiles()
        external = self.root / "external"
        external.mkdir()
        port = RuntimeSupplyPort(
            manager, self.store, self.root / "runtimes", filesystem=files
        )
        adopted = port.execute(
            "runtime.adopt", {"runtime": "optiq", "path": str(external)}
        )

        port.execute(
            "runtime.remove",
            {"resource": adopted["installation_id"], "confirmed": True},
        )

        self.assertEqual(files.removed, [])

    def test_runtime_remove_requires_a_direct_owned_child_and_exact_marker(
        self,
    ) -> None:
        manager = FakeRuntimeManager(self.root / "runtimes")
        files = FakeRuntimeFiles()
        port = RuntimeSupplyPort(
            manager, self.store, self.root / "runtimes", filesystem=files
        )
        outside = manager._installation(
            "optiq-outside", "optiq", "0.2", "tested", root=self.root / "outside"
        )
        outside.root.mkdir()
        RuntimeSupplyPort.persist_runtime(self.store, outside)

        with self.assertRaisesRegex(SupplyPortError, "direct managed child"):
            port.execute(
                "runtime.remove",
                {"resource": "optiq-outside", "confirmed": True},
            )

        self.assertEqual(files.removed, [])

        direct = manager._installation("optiq-direct", "optiq", "0.3", "tested")
        direct.root.mkdir(parents=True)
        RuntimeSupplyPort.persist_runtime(self.store, direct)
        with self.assertRaisesRegex(SupplyPortError, "ownership marker"):
            port.execute(
                "runtime.remove",
                {"resource": "optiq-direct", "confirmed": True},
            )

        self.assertEqual(files.removed, [])

    def test_runtime_prune_retains_two_rollback_candidates_per_definition(self) -> None:
        manager = FakeRuntimeManager(self.root / "runtimes")
        files = FakeRuntimeFiles()
        port = RuntimeSupplyPort(
            manager, self.store, self.root / "runtimes", filesystem=files
        )
        installations = [
            manager._installation(f"optiq-{index}", "optiq", f"0.{index}", "custom")
            for index in range(1, 4)
        ]
        for installation in installations:
            RuntimeSupplyPort.persist_runtime(self.store, installation)
            self._mark_owned(port, installation)

        result = port.execute("runtime.prune", {"confirmed": True})

        self.assertEqual(result["removed"], ["optiq-1"])
        self.assertEqual(
            files.removed,
            [installations[0].root.parent / ".optiq-1.removing"],
        )
        self.assertEqual(set(self.store.load().value.runtimes), {"optiq-2", "optiq-3"})

    def test_model_install_update_and_rollback_preserve_exact_installations(
        self,
    ) -> None:
        supply = FakeModelSupply(self.root / "cache")
        port = ModelSupplyPort(supply, self.store, self.security)

        installed = port.execute(
            "model.install",
            {
                "repository": "mlx-community/Qwen",
                "revision": "main",
                "alias": "coding",
            },
        )
        updated = port.execute(
            "model.update",
            {"resource": "coding", "revision": "next"},
        )

        config = self.store.load().value
        self.assertEqual(
            config.aliases["coding"].installation_name,
            updated["installation_name"],
        )
        self.assertIn(installed["installation_name"], config.models)
        self.assertIn(updated["installation_name"], config.models)
        self.assertEqual(
            config.models[updated["installation_name"]].revision.revision, _SHA_B
        )

        port.execute(
            "model.rollback",
            {
                "resource": "coding",
                "target": installed["installation_name"],
                "confirmed": True,
            },
        )
        self.assertEqual(
            self.store.load().value.aliases["coding"].installation_name,
            installed["installation_name"],
        )

        with self.assertRaisesRegex(SupplyPortError, "requires a Model Alias"):
            port.execute(
                "model.rollback",
                {
                    "resource": installed["installation_name"],
                    "target": updated["installation_name"],
                    "confirmed": True,
                },
            )

    def test_model_port_rejects_non_boolean_flags(self) -> None:
        port = ModelSupplyPort(
            FakeModelSupply(self.root / "cache"), self.store, self.security
        )

        with self.assertRaisesRegex(SupplyPortError, "offline must be a boolean"):
            port.execute(
                "model.install",
                {"repository": "mlx-community/Qwen", "offline": "false"},
            )

    def test_model_install_rejects_an_existing_name_for_another_revision(self) -> None:
        supply = FakeModelSupply(self.root / "cache")
        port = ModelSupplyPort(supply, self.store, self.security)
        installed = port.execute(
            "model.install",
            {
                "repository": "mlx-community/Qwen",
                "revision": "main",
                "alias": "coding",
            },
        )

        with self.assertRaisesRegex(SupplyPortError, "installation name collision"):
            port.execute(
                "model.install",
                {
                    "repository": "mlx-community/Qwen",
                    "revision": "next",
                    "alias": "coding",
                    "installation": installed["installation_name"],
                },
            )

        desired = self.store.load().value.models[installed["installation_name"]]
        self.assertEqual(desired.revision.revision, _SHA_A)

    def test_model_adopt_verifies_exact_external_bytes_and_never_owns_them(
        self,
    ) -> None:
        snapshot = self.root / "external-snapshot"
        snapshot.mkdir()
        config = b'{"model_type":"qwen"}'
        weights = b"exact external weights"
        (snapshot / "config.json").write_bytes(config)
        (snapshot / "weights.safetensors").write_bytes(weights)
        (snapshot / ".cache/huggingface").mkdir(parents=True)
        (snapshot / ".cache/huggingface/download.json").write_text("{}")
        files = (
            RepositoryFile(
                "config.json",
                len(config),
                lfs_sha256=hashlib.sha256(config).hexdigest(),
            ),
            RepositoryFile(
                "weights.safetensors",
                len(weights),
                lfs_sha256=hashlib.sha256(weights).hexdigest(),
            ),
        )
        security = ExactRevisionModelSecurity(
            FakeModelIntelligence(repository_files=files), self.security_state
        )
        supply = FakeModelSupply(self.root / "cache")
        port = ModelSupplyPort(supply, self.store, security)
        observation = inspect_adopted_snapshot(snapshot)

        result = port.execute(
            "model.adopt",
            {
                "repository": "owner/external-model",
                "revision": _SHA_A,
                "path": str(snapshot),
                "alias": "external",
                "snapshot_fingerprint": observation.fingerprint,
            },
        )

        desired = self.store.load().value.models[result["installation_name"]]
        self.assertEqual(desired.provenance, "adopted")
        self.assertEqual(desired.path, str(snapshot.resolve()))
        self.assertEqual(result["verification"]["status"], "verified")
        self.assertEqual(result["provenance"], "external-adopted")
        supplied = port._supplied_installation(result["installation_name"])
        self.assertEqual(supplied.snapshot_path, snapshot.resolve())
        self.assertEqual(supplied.provenance.source, "external-adopted")
        with self.assertRaisesRegex(SupplyPortError, "externally owned"):
            port.execute("model.repair", {"resource": "external"})
        self.assertTrue(snapshot.exists())
        self.assertEqual(
            port.execute("model.cache.prune", {"confirmed": True})["preview"][
                "revision_hashes"
            ],
            [],
        )
        self.assertTrue(snapshot.exists())

    def test_adopted_model_persists_and_revalidates_snapshot_identity(self) -> None:
        snapshot = self.root / "external-snapshot"
        snapshot.mkdir()
        config_path = snapshot / "config.json"
        payload = b'{"model_type":"qwen"}'
        config_path.write_bytes(payload)
        files = (
            RepositoryFile(
                "config.json",
                len(payload),
                lfs_sha256=hashlib.sha256(payload).hexdigest(),
            ),
        )
        security = ExactRevisionModelSecurity(
            FakeModelIntelligence(repository_files=files), self.security_state
        )
        port = ModelSupplyPort(
            FakeModelSupply(self.root / "cache"), self.store, security
        )
        observation = inspect_adopted_snapshot(snapshot)
        adopted = port.execute(
            "model.adopt",
            {
                "repository": "owner/external-model",
                "revision": _SHA_A,
                "path": str(snapshot),
                "alias": "external",
                "snapshot_fingerprint": observation.fingerprint,
            },
        )

        assessment = security.require("owner/external-model", _SHA_A)
        self.assertEqual(
            assessment["adopted_snapshot"]["fingerprint"], observation.fingerprint
        )
        metadata = config_path.stat()
        os.utime(
            config_path,
            ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000),
        )

        installation = port._supplied_installation(adopted["installation_name"])
        with self.assertRaisesRegex(SupplyPortError, "identity changed"):
            port.verify_installation(installation)

    def test_adopted_model_rollback_preserves_snapshot_identity_evidence(self) -> None:
        payload = b"same exact model"
        files = (
            RepositoryFile(
                "weights.bin",
                len(payload),
                lfs_sha256=hashlib.sha256(payload).hexdigest(),
            ),
        )
        security = ExactRevisionModelSecurity(
            FakeModelIntelligence(repository_files=files), self.security_state
        )
        port = ModelSupplyPort(
            FakeModelSupply(self.root / "cache"), self.store, security
        )
        installed = []
        for revision in (_SHA_A, _SHA_B):
            snapshot = self.root / revision
            snapshot.mkdir()
            (snapshot / "weights.bin").write_bytes(payload)
            observation = inspect_adopted_snapshot(snapshot)
            installed.append(
                port.execute(
                    "model.adopt",
                    {
                        "repository": "owner/external-model",
                        "revision": revision,
                        "path": str(snapshot),
                        "alias": "external",
                        "snapshot_fingerprint": observation.fingerprint,
                    },
                )
            )

        port.execute(
            "model.rollback",
            {
                "resource": "external",
                "target": installed[0]["installation_name"],
                "confirmed": True,
            },
        )

        assessment = security.require("owner/external-model", _SHA_A)
        self.assertIn("adopted_snapshot", assessment)

    def test_model_adopt_rejects_changed_missing_and_unsafe_snapshots(self) -> None:
        snapshot = self.root / "external-snapshot"
        snapshot.mkdir()
        payload = b"safe"
        file = snapshot / "config.json"
        file.write_bytes(payload)
        files = (
            RepositoryFile(
                "config.json",
                len(payload),
                lfs_sha256=hashlib.sha256(payload).hexdigest(),
            ),
        )
        security = ExactRevisionModelSecurity(
            FakeModelIntelligence(repository_files=files), self.security_state
        )
        port = ModelSupplyPort(
            FakeModelSupply(self.root / "cache"), self.store, security
        )
        fingerprint = inspect_adopted_snapshot(snapshot).fingerprint
        file.write_bytes(b"changed")
        with self.assertRaisesRegex(SupplyPortError, "identity changed"):
            port.execute(
                "model.adopt",
                {
                    "repository": "owner/model",
                    "revision": _SHA_A,
                    "path": str(snapshot),
                    "snapshot_fingerprint": fingerprint,
                },
            )
        file.unlink()
        file.symlink_to(snapshot / "missing")
        with self.assertRaisesRegex(SupplyPortError, "symlinks"):
            inspect_adopted_snapshot(snapshot)
        file.unlink()
        file.write_bytes(payload)
        with patch("mastic.infrastructure.supply_ports.os.getuid", return_value=999999):
            with self.assertRaisesRegex(SupplyPortError, "owned"):
                inspect_adopted_snapshot(snapshot)
        (snapshot / "unexpected.txt").write_text("not in the exact manifest")
        fingerprint = inspect_adopted_snapshot(snapshot).fingerprint
        with self.assertRaisesRegex(SupplyPortError, "integrity_mismatch"):
            port.execute(
                "model.adopt",
                {
                    "repository": "owner/model",
                    "revision": _SHA_A,
                    "path": str(snapshot),
                    "snapshot_fingerprint": fingerprint,
                },
            )

    def test_adopted_snapshot_requires_valid_content_digest_for_every_file(
        self,
    ) -> None:
        snapshot = self.root / "external-snapshot"
        snapshot.mkdir()
        (snapshot / "weights.bin").write_bytes(b"same")

        for evidence in (
            {"path": "weights.bin", "size": 4},
            {"path": "weights.bin", "size": 4, "lfs_sha256": "not-a-digest"},
            {"path": "weights.bin", "size": 4, "blob_id": "not-a-digest"},
        ):
            with self.subTest(evidence=evidence):
                with self.assertRaisesRegex(ModelSecurityPolicyError, "digest"):
                    verify_adopted_snapshot(snapshot, {"repository_files": [evidence]})

    def test_model_adopt_rejects_mastic_owned_and_cache_overlapping_paths(
        self,
    ) -> None:
        owned_root = self.root / "mastic-data"
        owned_snapshot = owned_root / "models" / "snapshot"
        owned_snapshot.mkdir(parents=True)
        (owned_snapshot / "weights.bin").write_bytes(b"owned")
        with self.assertRaisesRegex(SupplyPortError, "mastic-owned"):
            inspect_adopted_snapshot(owned_snapshot, forbidden_roots=(owned_root,))

        cache_snapshot = self.root / "cache" / _SHA_A
        cache_snapshot.mkdir(parents=True)
        (cache_snapshot / "weights.bin").write_bytes(b"cached")
        supply = FakeModelSupply(self.root / "cache")
        supply.revisions = (
            CachedRevision(
                "owner/model@" + _SHA_A,
                "owner/model",
                _SHA_A,
                cache_snapshot,
                6,
                "local-observed",
                True,
            ),
        )
        port = ModelSupplyPort(supply, self.store, self.security)
        with self.assertRaisesRegex(SupplyPortError, "managed Hugging Face cache"):
            port.inspect_adoption(str(cache_snapshot))

    def test_optiq_safetensors_install_persists_launchable_exact_security_evidence(
        self,
    ) -> None:
        supply = FakeModelSupply(self.root / "cache")
        port = ModelSupplyPort(supply, self.store, self.security)

        result = port.execute(
            "model.install",
            {
                "repository": "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit",
                "revision": _SHA_A,
                "alias": "qwen-optiq",
            },
        )

        self.assertEqual(result["security"]["hard_blockers"], [])
        self.assertEqual(result["security"]["verification"]["status"], "complete")
        persisted = self.security.require(
            "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit", _SHA_A
        )
        self.assertEqual(persisted["revision"], _SHA_A)

    def test_model_mutation_returns_bounded_security_summary(self) -> None:
        files = tuple(
            RepositoryFile(
                f"{'nested-' * 30}{index:05}.bin",
                1,
                blob_id=hashlib.sha1(str(index).encode()).hexdigest(),
            )
            for index in range(5_000)
        )
        security = ExactRevisionModelSecurity(
            FakeModelIntelligence(repository_files=files), self.security_state
        )
        port = ModelSupplyPort(
            FakeModelSupply(self.root / "cache"), self.store, security
        )

        result = port.execute(
            "model.install",
            {
                "repository": "owner/large-manifest",
                "revision": _SHA_A,
                "alias": "large",
            },
        )

        encoded = json.dumps(result, separators=(",", ":")).encode()
        self.assertLess(len(encoded), MAX_FRAME_BYTES)
        self.assertEqual(result["security"]["repository_file_count"], 5_000)
        self.assertNotIn("repository_files", result["security"])

    def test_model_install_hard_blocks_findings_unsafe_serialization_and_unknown_scan(
        self,
    ) -> None:
        scenarios = (
            TrustSignal(
                "hub_security_scan",
                "danger",
                EvidenceState.CONFLICTING,
                "Hub security status@test",
                "infected pickle",
            ),
            TrustSignal(
                "unsafe_serialization",
                "warning",
                EvidenceState.OBSERVED,
                "Hub repository inventory@test",
                "weights.bin",
            ),
            TrustSignal(
                "hub_security_scan",
                "unknown",
                EvidenceState.UNKNOWN,
                "Hub security status@test",
                "scan unavailable",
            ),
        )
        for signal in scenarios:
            with self.subTest(signal=signal.name, severity=signal.severity):
                supply = FakeModelSupply(self.root / f"cache-{signal.severity}")
                security = ExactRevisionModelSecurity(
                    FakeModelIntelligence(signal), self.security_state
                )
                port = ModelSupplyPort(supply, self.store, security)

                with self.assertRaisesRegex(SupplyPortError, "security policy"):
                    port.execute(
                        "model.install",
                        {
                            "repository": "owner/unsafe-model",
                            "revision": _SHA_A,
                        },
                    )

                self.assertFalse(any(call[0] == "install" for call in supply.calls))

    def test_integrity_mismatch_is_persisted_and_cannot_be_granted(self) -> None:
        assessment = self.security.inspect("owner/model", _SHA_A)

        with self.assertRaisesRegex(SupplyPortError, "integrity_mismatch"):
            self.security.record_verification(
                assessment,
                VerificationResult("incomplete", "cache-check", ("hash mismatch",)),
            )

        with self.assertRaisesRegex(SupplyPortError, "integrity_mismatch"):
            self.security.require("owner/model", _SHA_A)

    def test_model_update_and_rollback_block_explicit_runtime_incompatibility(self):
        supply = FakeModelSupply(self.root / "cache")
        supply.install(alias="coding", repo_id="mlx-community/Qwen", revision=_SHA_A)
        supply.install(alias="coding", repo_id="mlx-community/Qwen", revision=_SHA_B)
        self.store.import_text(
            f'''schema_version = 1
[gateway]
[runtimes.optiq]
definition = "optiq"
version = "0.3"
provenance = "tested"
root = "{self.root / "runtimes/optiq"}"
launcher = ["{self.root / "runtimes/optiq/bin/optiq"}", "serve"]
capabilities = ["host", "model", "port"]
[models.qwen-old]
repository = "mlx-community/Qwen"
revision = "{_SHA_A}"
[models.qwen-target]
repository = "mlx-community/Qwen"
revision = "{_SHA_B}"
[aliases.coding]
installation = "qwen-old"
[services.coding]
model_alias = "coding"
runtime = "optiq"
route = "coding"
[application_targets]
'''
        )
        compatibility = RuntimeCompatibility(
            "optiq",
            "optiq",
            "0.3",
            "unsupported",
            frozenset({"model", "host", "port"}),
            "exact runtime and model metadata",
            "explicit architecture contradiction",
        )
        security = ExactRevisionModelSecurity(
            FakeModelIntelligence(compatibility=(compatibility,)),
            self.security_state,
        )
        port = ModelSupplyPort(supply, self.store, security)

        requests = (
            ("model.update", {"resource": "coding", "revision": _SHA_B}),
            (
                "model.rollback",
                {
                    "resource": "coding",
                    "target": "qwen-target",
                    "confirmed": True,
                },
            ),
        )
        for operation, parameters in requests:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(SupplyPortError, "explicitly unsupported"):
                    port.execute(operation, parameters)
                self.assertEqual(
                    self.store.load().value.aliases["coding"].installation_name,
                    "qwen-old",
                )

    def test_model_repair_delegates_the_exact_pinned_revision(self) -> None:
        supply = FakeModelSupply(self.root / "cache")
        port = ModelSupplyPort(supply, self.store, self.security)
        installed = port.execute(
            "model.install",
            {
                "repository": "mlx-community/Qwen",
                "revision": _SHA_A,
                "alias": "coding",
            },
        )

        result = port.execute(
            "model.repair", {"resource": installed["installation_name"]}
        )

        repaired = supply.calls[-1][1]
        self.assertEqual(repaired.revision.commit_sha, _SHA_A)
        self.assertEqual(result["verification"]["status"], "complete")

    def test_model_repair_rejects_missing_cached_revision(self) -> None:
        supply = FakeModelSupply(self.root / "cache")
        port = ModelSupplyPort(supply, self.store, self.security)
        installed = port.execute(
            "model.install",
            {"repository": "mlx-community/Qwen", "revision": _SHA_A},
        )
        supply.revisions = ()

        with self.assertRaisesRegex(
            SupplyPortError, "cached Model Revision is missing"
        ):
            port.execute("model.repair", {"resource": installed["installation_name"]})

    def test_cache_eviction_is_reference_aware_and_requires_confirmation(self) -> None:
        supply = FakeModelSupply(self.root / "cache")
        port = ModelSupplyPort(supply, self.store, self.security)
        installed = port.execute(
            "model.install",
            {
                "repository": "mlx-community/Qwen",
                "revision": _SHA_A,
                "alias": "coding",
            },
        )

        with self.assertRaisesRegex(SupplyPortError, "referenced"):
            port.execute(
                "model.cache.evict",
                {"resource": _SHA_A, "confirmed": True},
            )

        self.store.edit(
            lambda document: (
                document["aliases"].pop("coding"),
                document["models"].pop(installed["installation_name"]),
            )
        )
        with self.assertRaisesRegex(PermissionError, "confirmation"):
            port.execute("model.cache.evict", {"resource": _SHA_A})

        result = port.execute(
            "model.cache.evict", {"resource": _SHA_A, "confirmed": True}
        )
        self.assertTrue(result["preview"]["allowed"])
        self.assertTrue(supply.strategies[-1].executed)

    def test_cache_prune_deletes_only_unreferenced_revisions(self) -> None:
        supply = FakeModelSupply(self.root / "cache")
        port = ModelSupplyPort(supply, self.store, self.security)
        port.execute(
            "model.install",
            {
                "repository": "mlx-community/Qwen",
                "revision": _SHA_A,
                "alias": "coding",
            },
        )
        unreferenced = CachedRevision(
            "other/model@" + _SHA_B,
            "other/model",
            _SHA_B,
            self.root / "cache" / _SHA_B,
            33,
            "local-observed",
            True,
        )
        supply.revisions += (unreferenced,)

        result = port.execute("model.cache.prune", {"confirmed": True})

        self.assertEqual(result["preview"]["revision_hashes"], [_SHA_B])
        self.assertTrue(supply.strategies[-1].executed)

    def test_cache_move_exposes_preview_and_confirms_source_cleanup(self) -> None:
        supply = FakeModelSupply(self.root / "cache")
        mover = FakeCacheMover()
        port = ModelSupplyPort(supply, self.store, self.security, cache_mover=mover)
        port.execute(
            "model.install",
            {
                "repository": "mlx-community/Qwen",
                "revision": _SHA_A,
                "alias": "coding",
            },
        )

        with self.assertRaisesRegex(PermissionError, "confirmation"):
            port.execute(
                "model.cache.move",
                {
                    "resource": _SHA_A,
                    "destination": str(self.root / "new-cache"),
                    "cleanup_source": True,
                },
            )

        with self.assertRaisesRegex(SupplyPortError, "referenced"):
            port.execute(
                "model.cache.move",
                {
                    "resource": _SHA_A,
                    "destination": str(self.root / "new-cache"),
                    "cleanup_source": True,
                    "confirmed": True,
                },
            )

        unreferenced = CachedRevision(
            "other/model@" + _SHA_B,
            "other/model",
            _SHA_B,
            self.root / "cache" / _SHA_B,
            33,
            "local-observed",
            True,
        )
        supply.revisions += (unreferenced,)
        result = port.execute(
            "model.cache.move",
            {
                "resource": _SHA_B,
                "destination": str(self.root / "new-cache"),
                "cleanup_source": True,
                "confirmed": True,
            },
        )
        self.assertEqual(result["preview"]["bytes_to_copy"], 33)
        self.assertEqual(len(mover.executed), 1)
        self.assertTrue(mover.executed[0].cleanup_source)

    def test_cache_move_rechecks_references_immediately_before_cleanup(self) -> None:
        supply = FakeModelSupply(self.root / "cache")
        mover = FakeCacheMover()
        port = ModelSupplyPort(supply, self.store, self.security, cache_mover=mover)
        revision = CachedRevision(
            "other/model@" + _SHA_B,
            "other/model",
            _SHA_B,
            self.root / "cache" / _SHA_B,
            33,
            "local-observed",
            True,
        )
        supply.revisions = (revision,)
        mover.before_cleanup_action = lambda: port.execute(
            "model.install",
            {
                "repository": "other/model",
                "revision": _SHA_B,
                "alias": "late-reference",
            },
        )

        with self.assertRaisesRegex(SupplyPortError, "referenced"):
            port.execute(
                "model.cache.move",
                {
                    "resource": _SHA_B,
                    "destination": str(self.root / "new-cache"),
                    "cleanup_source": True,
                    "confirmed": True,
                },
            )

        self.assertEqual(mover.executed, [])

    def test_default_cache_mover_content_verifies_before_atomic_publish(self) -> None:
        source = self.root / "source"
        source.mkdir()
        (source / "weights.bin").write_bytes(b"exact model bytes")
        revision = CachedRevision(
            "mlx-community/Qwen@" + _SHA_A,
            "mlx-community/Qwen",
            _SHA_A,
            source,
            17,
            "local-observed",
            True,
        )
        destination = self.root / "destination"
        mover = VerifiedCacheMover()

        published = mover.execute(mover.preview(revision, destination))

        self.assertEqual(published, destination.resolve())
        self.assertEqual((published / "weights.bin").read_bytes(), b"exact model bytes")
        self.assertTrue(source.exists())

    def test_default_cache_mover_retries_past_an_interrupted_staging_directory(
        self,
    ) -> None:
        source = self.root / "source"
        source.mkdir()
        (source / "weights.bin").write_bytes(b"exact model bytes")
        revision = CachedRevision(
            "mlx-community/Qwen@" + _SHA_A,
            "mlx-community/Qwen",
            _SHA_A,
            source,
            17,
            "local-observed",
            True,
        )
        destination = self.root / "destination"
        stage = self.root / (
            ".destination.mastic-staging-"
            + hashlib.sha256(revision.revision_id.encode()).hexdigest()[:12]
        )
        stage.mkdir()
        (stage / "untrusted").write_text("do not merge")

        published = VerifiedCacheMover().execute(
            VerifiedCacheMover().preview(revision, destination)
        )

        self.assertEqual((published / "weights.bin").read_bytes(), b"exact model bytes")
        self.assertTrue((stage / "untrusted").exists())


if __name__ == "__main__":
    unittest.main()
