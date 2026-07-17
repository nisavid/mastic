import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from mastic.infrastructure.model_supply import (
    CacheInventory,
    CachedRevision,
    HuggingFaceHubClient,
    HubModelRecord,
    ModelSupply,
    VerificationResult,
)


@dataclass
class FakeDeletionStrategy:
    expected_freed_size: int
    executed: bool = False

    def execute(self) -> None:
        self.executed = True


class FakeHub:
    def __init__(self, snapshot: Path) -> None:
        self.snapshot = snapshot
        self.search_calls: list[tuple[str, str | None, int]] = []
        self.resolve_calls: list[tuple[str, str, bool]] = []
        self.download_calls: list[tuple[str, str, bool, bool]] = []
        self.verification = VerificationResult(
            status="complete",
            evidence="cache-completeness",
            issues=(),
        )
        self.inventory = CacheInventory(
            revisions=(
                CachedRevision(
                    revision_id="mlx-community/local@abc123",
                    repo_id="mlx-community/local",
                    commit_sha="abc123",
                    snapshot_path=snapshot,
                    size_on_disk=1234,
                    evidence="local-observed",
                    complete=True,
                ),
            ),
            evidence="local-observed",
            warnings=(),
        )
        self.deletion = FakeDeletionStrategy(expected_freed_size=900)
        self.deletion_calls: list[tuple[str, ...]] = []

    def search_models(
        self, query: str, *, author: str | None, limit: int
    ) -> tuple[HubModelRecord, ...]:
        self.search_calls.append((query, author, limit))
        return (
            HubModelRecord(
                repo_id="mlx-community/Qwen-test",
                reported_sha="f" * 40,
                pipeline_tag="text-generation",
                library_name="mlx",
                tags=("mlx", "4-bit"),
                private=False,
                gated=False,
            ),
        )

    def resolve_revision(
        self, repo_id: str, revision: str, *, local_files_only: bool
    ) -> str:
        self.resolve_calls.append((repo_id, revision, local_files_only))
        return "a" * 40

    def snapshot_download(
        self,
        repo_id: str,
        revision: str,
        *,
        local_files_only: bool,
        force_download: bool,
    ) -> Path:
        self.download_calls.append(
            (repo_id, revision, local_files_only, force_download)
        )
        return self.snapshot

    def verify_revision(
        self, repo_id: str, revision: str, snapshot_path: Path
    ) -> VerificationResult:
        return self.verification

    def cache_inventory(self) -> CacheInventory:
        return self.inventory

    def preview_cache_deletion(self, commit_hashes: tuple[str, ...]):
        self.deletion_calls.append(commit_hashes)
        return self.deletion


class ModelDiscoveryTests(unittest.TestCase):
    def test_curated_search_defaults_to_mlx_community_without_claiming_compatibility(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hub = FakeHub(Path(directory))
            supply = ModelSupply(hub)

            candidates = supply.search("Qwen", mode="curated", limit=8)

            self.assertEqual(hub.search_calls, [("Qwen", "mlx-community", 8)])
            self.assertEqual(candidates[0].repo_id, "mlx-community/Qwen-test")
            self.assertEqual(candidates[0].evidence, "hub-declared")
            self.assertIsNone(candidates[0].compatibility)

    def test_broad_and_local_search_have_distinct_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hub = FakeHub(Path(directory))
            supply = ModelSupply(hub)

            broad = supply.search("Qwen", mode="broad")
            local = supply.search("local", mode="local")

            self.assertEqual(hub.search_calls[-1], ("Qwen", None, 20))
            self.assertEqual(broad[0].source, "hub")
            self.assertEqual(local[0].source, "cache")
            self.assertEqual(local[0].reported_sha, "abc123")
            self.assertEqual(local[0].evidence, "local-observed")


class ModelInstallTests(unittest.TestCase):
    def test_install_resolves_a_mutable_reference_then_pins_exact_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            hub = FakeHub(snapshot)
            supply = ModelSupply(hub)

            result = supply.install(
                alias="coding",
                repo_id="mlx-community/Qwen-test",
                revision="main",
            )

            self.assertEqual(
                hub.resolve_calls,
                [("mlx-community/Qwen-test", "main", False)],
            )
            self.assertEqual(
                hub.download_calls,
                [("mlx-community/Qwen-test", "a" * 40, False, False)],
            )
            self.assertEqual(result.revision.commit_sha, "a" * 40)
            self.assertTrue(result.cached.complete)
            self.assertEqual(result.installation.revision, result.revision)
            self.assertEqual(
                result.installation.cached_revision_id, result.cached.revision_id
            )
            self.assertEqual(result.alias.name, "coding")
            self.assertEqual(
                result.alias.installation_id, result.installation.installation_id
            )
            self.assertNotEqual(result.installation, result.cached)

    def test_offline_install_labels_exact_local_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hub = FakeHub(Path(directory))
            result = ModelSupply(hub).install(
                alias="offline",
                repo_id="mlx-community/Qwen-test",
                revision="a" * 40,
                offline=True,
            )

            self.assertEqual(
                hub.resolve_calls[-1],
                (
                    "mlx-community/Qwen-test",
                    "a" * 40,
                    True,
                ),
            )
            self.assertEqual(result.revision.evidence, "offline-cached")
            self.assertEqual(result.cached.evidence, "offline-cached")

    def test_verify_and_repair_use_the_exact_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hub = FakeHub(Path(directory))
            supply = ModelSupply(hub)
            installed = supply.install(
                alias="coding",
                repo_id="mlx-community/Qwen-test",
                revision="main",
            ).installation
            hub.verification = VerificationResult(
                status="incomplete",
                evidence="cache-completeness",
                issues=("missing shard",),
            )

            before = supply.verify(installed)
            repaired = supply.repair(installed)

            self.assertEqual(before.status, "incomplete")
            self.assertEqual(
                hub.download_calls[-1],
                ("mlx-community/Qwen-test", "a" * 40, False, False),
            )
            self.assertEqual(repaired.status, "incomplete")


class ModelCacheTests(unittest.TestCase):
    def test_cache_deletion_is_blocked_while_an_installation_references_revision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hub = FakeHub(Path(directory))
            supply = ModelSupply(hub)
            installation = supply.install(
                alias="coding",
                repo_id="mlx-community/Qwen-test",
                revision="main",
            ).installation

            preview = supply.preview_cache_deletion(
                (installation.revision.commit_sha,), installations=(installation,)
            )

            self.assertFalse(preview.allowed)
            self.assertEqual(preview.blocked_by, (installation.installation_id,))
            self.assertEqual(hub.deletion_calls, [])

    def test_official_cache_deletion_preview_requires_explicit_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hub = FakeHub(Path(directory))
            preview = ModelSupply(hub).preview_cache_deletion(("abc123",))

            self.assertTrue(preview.allowed)
            self.assertEqual(preview.expected_freed_size, 900)
            self.assertEqual(hub.deletion_calls, [("abc123",)])
            with self.assertRaisesRegex(PermissionError, "explicit approval"):
                preview.execute()
            preview.execute(approved=True)
            self.assertTrue(hub.deletion.executed)


class HuggingFaceHubClientTests(unittest.TestCase):
    def test_official_api_objects_are_normalized_behind_the_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / ("b" * 40)
            snapshot.mkdir()
            calls: list[tuple[str, object]] = []
            deletion = FakeDeletionStrategy(456)

            class FakeApi:
                def list_models(self, **kwargs):
                    calls.append(("list_models", kwargs))
                    return (
                        SimpleNamespace(
                            id="mlx-community/Qwen",
                            sha="a" * 40,
                            pipeline_tag="text-generation",
                            library_name="mlx",
                            tags=["mlx"],
                            private=False,
                            gated="manual",
                        ),
                    )

                def model_info(self, repo_id, **kwargs):
                    calls.append(("model_info", (repo_id, kwargs)))
                    return SimpleNamespace(sha="b" * 40)

            def snapshot_download(**kwargs):
                calls.append(("snapshot_download", kwargs))
                return str(snapshot)

            cache_info = SimpleNamespace(
                warnings=(),
                repos=(
                    SimpleNamespace(
                        repo_id="mlx-community/Qwen",
                        revisions=(
                            SimpleNamespace(
                                commit_hash="b" * 40,
                                snapshot_path=snapshot,
                                size_on_disk=321,
                            ),
                        ),
                    ),
                ),
                delete_revisions=lambda *hashes: (
                    calls.append(("delete_revisions", hashes)) or deletion
                ),
            )
            module = ModuleType("huggingface_hub")
            module.HfApi = FakeApi
            module.snapshot_download = snapshot_download
            module.scan_cache_dir = lambda: cache_info

            with patch.dict("sys.modules", {"huggingface_hub": module}):
                client = HuggingFaceHubClient()
                records = client.search_models("Qwen", author="mlx-community", limit=3)
                resolved = client.resolve_revision(
                    "mlx-community/Qwen", "main", local_files_only=False
                )
                inventory = client.cache_inventory()
                preview = client.preview_cache_deletion(("b" * 40,))

            self.assertEqual(records[0].repo_id, "mlx-community/Qwen")
            self.assertEqual(records[0].gated, "manual")
            self.assertEqual(resolved, "b" * 40)
            self.assertEqual(inventory.revisions[0].size_on_disk, 321)
            self.assertIsNone(inventory.revisions[0].complete)
            self.assertIs(preview, deletion)
            self.assertIn(("delete_revisions", ("b" * 40,)), calls)


if __name__ == "__main__":
    unittest.main()
