import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from mlxctl.application.config_schema import validate_config
from mlxctl.infrastructure.config_store import ConfigStore
from mlxctl.infrastructure.model_supply import (
    CacheDeletionPlan,
    CacheInventory,
    CachedRevision,
    ModelAlias,
    ModelInstallResult,
    ModelInstallation,
    ModelProvenance,
    ModelRevision,
    VerificationResult,
)
from mlxctl.infrastructure.runtime_supply import (
    RuntimeCatalogue,
    RuntimeInstallation,
)
from mlxctl.infrastructure.supply_ports import (
    CacheMovePlan,
    ModelSupplyPort,
    RuntimeSupplyPort,
    SupplyPortError,
    VerifiedCacheMover,
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
        return self._installation(
            bundle_id,
            runtime,
            version,
            "tested",
            bundle_id=bundle_id,
        )

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
        return self._installation(
            f"{runtime}-{version}-custom", runtime, version, "custom"
        )

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

    def repair(self, installation: ModelInstallation) -> VerificationResult:
        self.calls.append(("repair", installation))
        return VerificationResult("complete", "cache-completeness", ())

    def search(self, query: str, *, mode: str = "curated", limit: int = 20):
        self.calls.append(("search", query, mode, limit))
        return (query, mode, limit)

    def inventory(self) -> CacheInventory:
        self.calls.append(("inventory",))
        return CacheInventory(self.revisions, "local-observed", ())

    def plan_cache_deletion(
        self,
        commit_hashes: tuple[str, ...],
        *,
        installations: tuple[ModelInstallation, ...] = (),
    ) -> CacheDeletionPlan:
        self.calls.append(("plan_cache_deletion", commit_hashes, installations))
        blocked = tuple(
            installation.installation_id
            for installation in installations
            if installation.revision.commit_sha in commit_hashes
        )
        if blocked:
            return CacheDeletionPlan(False, commit_hashes, blocked, 0)
        strategy = FakeDeletionStrategy()
        self.strategies.append(strategy)
        return CacheDeletionPlan(
            True,
            commit_hashes,
            (),
            strategy.expected_freed_size,
            strategy,
        )


class FakeCacheMover:
    def __init__(self) -> None:
        self.plans: list[CacheMovePlan] = []
        self.executed: list[CacheMovePlan] = []

    def plan(self, revision: CachedRevision, destination: Path) -> CacheMovePlan:
        plan = CacheMovePlan(
            revision_id=revision.revision_id,
            source=revision.snapshot_path,
            destination=destination,
            bytes_to_copy=revision.size_on_disk,
            steps=("copy", "verify", "publish"),
        )
        self.plans.append(plan)
        return plan

    def execute(self, plan: CacheMovePlan) -> Path:
        self.executed.append(plan)
        return plan.destination


class SupplyPortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ConfigStore(self.root / "config.toml", validate_config)
        self.store.import_text(
            """schema_version = 1

[gateway]
host = "127.0.0.1"
port = 8766

[runtimes]
[models]
[aliases]
[services]
[clients]
"""
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

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
        self.assertEqual(result["plan"]["operation"], "install")

    def test_runtime_install_initializes_supported_v1_desired_state(self) -> None:
        store = ConfigStore(self.root / "fresh.toml", validate_config)
        manager = FakeRuntimeManager(self.root / "runtimes")
        port = RuntimeSupplyPort(manager, store, self.root / "runtimes")

        result = port.execute("runtime.install", {"runtime": "mlx_lm"})

        self.assertIn(result["installation_id"], store.load().value.runtimes)

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
[clients]
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
        self.assertEqual(result["plan"]["referenced_services"], ["coding"])

    def test_runtime_remove_is_reference_gated_and_requires_confirmation(self) -> None:
        manager = FakeRuntimeManager(self.root / "runtimes")
        files = FakeRuntimeFiles()
        installation = manager._installation("optiq-old", "optiq", "0.2", "tested")
        RuntimeSupplyPort.persist_runtime(self.store, installation)
        port = RuntimeSupplyPort(
            manager, self.store, self.root / "runtimes", filesystem=files
        )

        with self.assertRaisesRegex(PermissionError, "confirmation"):
            port.execute("runtime.remove", {"resource": "optiq-old"})

        result = port.execute(
            "runtime.remove", {"resource": "optiq-old", "confirmed": True}
        )

        self.assertTrue(result["plan"]["allowed"])
        self.assertEqual(files.removed, [installation.root])
        self.assertNotIn("optiq-old", self.store.load().value.runtimes)

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
[clients]
"""
        )
        port = RuntimeSupplyPort(
            manager, self.store, self.root / "runtimes", filesystem=files
        )

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

        result = port.execute("runtime.prune", {"confirmed": True})

        self.assertEqual(result["removed"], ["optiq-1"])
        self.assertEqual(files.removed, [installations[0].root])
        self.assertEqual(set(self.store.load().value.runtimes), {"optiq-2", "optiq-3"})

    def test_model_install_update_and_rollback_preserve_exact_installations(
        self,
    ) -> None:
        supply = FakeModelSupply(self.root / "cache")
        port = ModelSupplyPort(supply, self.store)

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

    def test_model_repair_delegates_the_exact_pinned_revision(self) -> None:
        supply = FakeModelSupply(self.root / "cache")
        port = ModelSupplyPort(supply, self.store)
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

    def test_cache_eviction_is_reference_aware_and_requires_confirmation(self) -> None:
        supply = FakeModelSupply(self.root / "cache")
        port = ModelSupplyPort(supply, self.store)
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
        self.assertTrue(result["plan"]["allowed"])
        self.assertTrue(supply.strategies[-1].executed)

    def test_cache_prune_deletes_only_unreferenced_revisions(self) -> None:
        supply = FakeModelSupply(self.root / "cache")
        port = ModelSupplyPort(supply, self.store)
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

        self.assertEqual(result["plan"]["revision_hashes"], [_SHA_B])
        self.assertTrue(supply.strategies[-1].executed)

    def test_cache_move_exposes_plan_and_confirms_source_cleanup(self) -> None:
        supply = FakeModelSupply(self.root / "cache")
        mover = FakeCacheMover()
        port = ModelSupplyPort(supply, self.store, cache_mover=mover)
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

        result = port.execute(
            "model.cache.move",
            {
                "resource": _SHA_A,
                "destination": str(self.root / "new-cache"),
                "cleanup_source": True,
                "confirmed": True,
            },
        )
        self.assertEqual(result["plan"]["bytes_to_copy"], 17)
        self.assertEqual(len(mover.executed), 1)
        self.assertTrue(mover.executed[0].cleanup_source)

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

        published = mover.execute(mover.plan(revision, destination))

        self.assertEqual(published, destination.resolve())
        self.assertEqual((published / "weights.bin").read_bytes(), b"exact model bytes")
        self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
