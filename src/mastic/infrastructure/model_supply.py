"""Hugging Face backed model discovery, installation, and cache supply."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class ModelSupplyError(ValueError):
    """A model supply operation cannot satisfy its contract."""


@dataclass(frozen=True)
class HubModelRecord:
    """Selected publisher and repository fields returned by the Hub."""

    repo_id: str
    reported_sha: str | None
    pipeline_tag: str | None
    library_name: str | None
    tags: tuple[str, ...]
    private: bool
    gated: bool | str


@dataclass(frozen=True)
class CatalogCandidate:
    """A discoverable model source, without an implied compatibility claim."""

    repo_id: str
    source: str
    evidence: str
    reported_sha: str | None
    pipeline_tag: str | None = None
    library_name: str | None = None
    tags: tuple[str, ...] = ()
    private: bool | None = None
    gated: bool | str | None = None
    compatibility: None = None


@dataclass(frozen=True)
class ModelRevision:
    """An immutable repository revision resolved from a requested reference."""

    repo_id: str
    commit_sha: str
    requested_revision: str
    evidence: str

    @property
    def revision_id(self) -> str:
        return f"{self.repo_id}@{self.commit_sha}"


@dataclass(frozen=True)
class CachedRevision:
    """Physical local bytes for one exact revision in the shared cache."""

    revision_id: str
    repo_id: str
    commit_sha: str
    snapshot_path: Path
    size_on_disk: int
    evidence: str
    complete: bool | None


@dataclass(frozen=True)
class CacheInventory:
    """Read-only local cache observations and scan warnings."""

    revisions: tuple[CachedRevision, ...]
    evidence: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ModelProvenance:
    """The source reference and exact identity captured at installation."""

    requested_revision: str
    resolved_sha: str
    source: str


@dataclass(frozen=True)
class ModelInstallation:
    """Durable mastic intent for one exact model revision."""

    installation_id: str
    revision: ModelRevision
    cached_revision_id: str
    snapshot_path: Path
    provenance: ModelProvenance


@dataclass(frozen=True)
class ModelAlias:
    """A stable user-facing name selecting one Model Installation."""

    name: str
    installation_id: str


@dataclass(frozen=True)
class VerificationResult:
    """Exact-revision verification evidence, without overstating integrity."""

    status: str
    evidence: str
    issues: tuple[str, ...]


@dataclass(frozen=True)
class ModelInstallResult:
    revision: ModelRevision
    cached: CachedRevision
    installation: ModelInstallation
    alias: ModelAlias
    verification: VerificationResult


class HubDeletionStrategy(Protocol):
    expected_freed_size: int

    def execute(self) -> None: ...


class ModelHub(Protocol):
    """Injectable boundary around official Hugging Face Hub APIs."""

    def search_models(
        self, query: str, *, author: str | None, limit: int
    ) -> tuple[HubModelRecord, ...]: ...

    def resolve_revision(
        self, repo_id: str, revision: str, *, local_files_only: bool
    ) -> str: ...

    def snapshot_download(
        self,
        repo_id: str,
        revision: str,
        *,
        local_files_only: bool,
        force_download: bool,
    ) -> Path: ...

    def verify_revision(
        self, repo_id: str, revision: str, snapshot_path: Path
    ) -> VerificationResult: ...

    def cache_inventory(self) -> CacheInventory: ...

    def plan_cache_deletion(
        self, commit_hashes: tuple[str, ...]
    ) -> HubDeletionStrategy: ...


@dataclass(frozen=True)
class CacheDeletionPlan:
    """Reference-aware wrapper around the Hub's official deletion strategy."""

    allowed: bool
    revision_hashes: tuple[str, ...]
    blocked_by: tuple[str, ...]
    expected_freed_size: int
    _strategy: HubDeletionStrategy | None = field(
        default=None, repr=False, compare=False
    )

    def execute(self, *, approved: bool = False) -> None:
        if not self.allowed or self._strategy is None:
            raise ModelSupplyError("cache deletion plan is blocked by references")
        if not approved:
            raise PermissionError("cache deletion requires explicit approval")
        self._strategy.execute()


class ModelSupply:
    """Supply models while preserving catalog, revision, cache, and intent."""

    def __init__(self, hub: ModelHub) -> None:
        self._hub = hub

    def search(
        self, query: str, *, mode: str = "curated", limit: int = 20
    ) -> tuple[CatalogCandidate, ...]:
        if mode == "local":
            needle = query.casefold()
            return tuple(
                CatalogCandidate(
                    repo_id=revision.repo_id,
                    source="cache",
                    evidence=revision.evidence,
                    reported_sha=revision.commit_sha,
                )
                for revision in self._hub.cache_inventory().revisions
                if needle in revision.repo_id.casefold()
            )
        if mode not in {"curated", "broad"}:
            raise ModelSupplyError(f"unknown model search mode: {mode}")
        author = "mlx-community" if mode == "curated" else None
        return tuple(
            CatalogCandidate(
                repo_id=record.repo_id,
                source="hub",
                evidence="hub-declared",
                reported_sha=record.reported_sha,
                pipeline_tag=record.pipeline_tag,
                library_name=record.library_name,
                tags=record.tags,
                private=record.private,
                gated=record.gated,
            )
            for record in self._hub.search_models(query, author=author, limit=limit)
        )

    def resolve(
        self,
        repo_id: str,
        revision: str,
        *,
        offline: bool = False,
    ) -> ModelRevision:
        commit_sha = self._hub.resolve_revision(
            repo_id, revision, local_files_only=offline
        )
        if not _COMMIT_SHA.fullmatch(commit_sha):
            raise ModelSupplyError(
                f"Hub did not resolve {repo_id}@{revision} to an exact commit SHA"
            )
        return ModelRevision(
            repo_id=repo_id,
            commit_sha=commit_sha,
            requested_revision=revision,
            evidence="offline-cached" if offline else "hub-observed",
        )

    def install(
        self,
        *,
        alias: str,
        repo_id: str,
        revision: str,
        offline: bool = False,
    ) -> ModelInstallResult:
        _validate_alias(alias)
        resolved = self.resolve(repo_id, revision, offline=offline)
        snapshot = (
            self._hub.snapshot_download(
                repo_id,
                resolved.commit_sha,
                local_files_only=offline,
                force_download=False,
            )
            .expanduser()
            .resolve()
        )
        evidence = "offline-cached" if offline else "downloaded-exact"
        verification = self._hub.verify_revision(repo_id, resolved.commit_sha, snapshot)
        if verification.status not in {"complete", "verified"}:
            details = "; ".join(verification.issues) or verification.status
            raise ModelSupplyError(
                f"exact revision is not ready for installation: {details}"
            )
        cached = CachedRevision(
            revision_id=resolved.revision_id,
            repo_id=repo_id,
            commit_sha=resolved.commit_sha,
            snapshot_path=snapshot,
            size_on_disk=_tree_size(snapshot),
            evidence=evidence,
            complete=True,
        )
        provenance = ModelProvenance(
            requested_revision=revision,
            resolved_sha=resolved.commit_sha,
            source="hugging-face-cache",
        )
        installation = ModelInstallation(
            installation_id=resolved.revision_id,
            revision=resolved,
            cached_revision_id=cached.revision_id,
            snapshot_path=snapshot,
            provenance=provenance,
        )
        return ModelInstallResult(
            revision=resolved,
            cached=cached,
            installation=installation,
            alias=ModelAlias(alias, installation.installation_id),
            verification=verification,
        )

    def verify(self, installation: ModelInstallation) -> VerificationResult:
        return self._hub.verify_revision(
            installation.revision.repo_id,
            installation.revision.commit_sha,
            installation.snapshot_path,
        )

    def repair(self, installation: ModelInstallation) -> VerificationResult:
        snapshot = (
            self._hub.snapshot_download(
                installation.revision.repo_id,
                installation.revision.commit_sha,
                local_files_only=False,
                force_download=False,
            )
            .expanduser()
            .resolve()
        )
        return self._hub.verify_revision(
            installation.revision.repo_id,
            installation.revision.commit_sha,
            snapshot,
        )

    def inventory(self) -> CacheInventory:
        return self._hub.cache_inventory()

    def plan_cache_deletion(
        self,
        commit_hashes: tuple[str, ...],
        *,
        installations: tuple[ModelInstallation, ...] = (),
    ) -> CacheDeletionPlan:
        requested = tuple(dict.fromkeys(commit_hashes))
        if not requested:
            raise ModelSupplyError("cache deletion requires at least one revision")
        blocked_by = tuple(
            sorted(
                installation.installation_id
                for installation in installations
                if installation.revision.commit_sha in requested
            )
        )
        if blocked_by:
            return CacheDeletionPlan(
                allowed=False,
                revision_hashes=requested,
                blocked_by=blocked_by,
                expected_freed_size=0,
            )
        strategy = self._hub.plan_cache_deletion(requested)
        return CacheDeletionPlan(
            allowed=True,
            revision_hashes=requested,
            blocked_by=(),
            expected_freed_size=strategy.expected_freed_size,
            _strategy=strategy,
        )


class HuggingFaceHubClient:
    """Official huggingface_hub implementation of the injectable boundary."""

    def search_models(
        self, query: str, *, author: str | None, limit: int
    ) -> tuple[HubModelRecord, ...]:
        from huggingface_hub import HfApi

        records = HfApi().list_models(
            search=query,
            author=author,
            limit=limit,
            full=True,
        )
        return tuple(
            HubModelRecord(
                repo_id=record.id,
                reported_sha=getattr(record, "sha", None),
                pipeline_tag=getattr(record, "pipeline_tag", None),
                library_name=getattr(record, "library_name", None),
                tags=tuple(getattr(record, "tags", None) or ()),
                private=bool(getattr(record, "private", False)),
                gated=getattr(record, "gated", False),
            )
            for record in records
        )

    def resolve_revision(
        self, repo_id: str, revision: str, *, local_files_only: bool
    ) -> str:
        if local_files_only:
            snapshot = self.snapshot_download(
                repo_id,
                revision,
                local_files_only=True,
                force_download=False,
            )
            commit_sha = snapshot.name
        else:
            from huggingface_hub import HfApi

            commit_sha = (
                HfApi().model_info(repo_id, revision=revision, files_metadata=True).sha
            )
        if not commit_sha:
            raise ModelSupplyError(f"Hub returned no commit SHA for {repo_id}")
        return commit_sha

    def snapshot_download(
        self,
        repo_id: str,
        revision: str,
        *,
        local_files_only: bool,
        force_download: bool,
    ) -> Path:
        from huggingface_hub import snapshot_download

        return Path(
            snapshot_download(
                repo_id=repo_id,
                revision=revision,
                local_files_only=local_files_only,
                force_download=force_download,
            )
        )

    def verify_revision(
        self, repo_id: str, revision: str, snapshot_path: Path
    ) -> VerificationResult:
        try:
            resolved = self.snapshot_download(
                repo_id,
                revision,
                local_files_only=True,
                force_download=False,
            ).resolve()
        except Exception as error:
            return VerificationResult(
                status="incomplete",
                evidence="cache-completeness",
                issues=(str(error),),
            )
        expected = snapshot_path.expanduser().resolve()
        if resolved != expected:
            return VerificationResult(
                status="conflicting",
                evidence="cache-completeness",
                issues=(f"Hub resolved snapshot to {resolved}, expected {expected}",),
            )
        return VerificationResult(
            status="complete",
            evidence="cache-completeness",
            issues=(),
        )

    def cache_inventory(self) -> CacheInventory:
        from huggingface_hub import scan_cache_dir

        cache_info = scan_cache_dir()
        warnings = tuple(str(warning) for warning in cache_info.warnings)
        revisions = []
        for repo in cache_info.repos:
            for revision in repo.revisions:
                revisions.append(
                    CachedRevision(
                        revision_id=f"{repo.repo_id}@{revision.commit_hash}",
                        repo_id=repo.repo_id,
                        commit_sha=revision.commit_hash,
                        snapshot_path=Path(revision.snapshot_path),
                        size_on_disk=revision.size_on_disk,
                        evidence="local-observed",
                        complete=None,
                    )
                )
        return CacheInventory(
            revisions=tuple(
                sorted(
                    revisions,
                    key=lambda item: (item.repo_id, item.commit_sha),
                )
            ),
            evidence="local-observed",
            warnings=warnings,
        )

    def plan_cache_deletion(
        self, commit_hashes: tuple[str, ...]
    ) -> HubDeletionStrategy:
        from huggingface_hub import scan_cache_dir

        return scan_cache_dir().delete_revisions(*commit_hashes)


_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{40,64}\Z")
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _validate_alias(alias: str) -> None:
    if not _ALIAS.fullmatch(alias):
        raise ModelSupplyError(f"invalid model alias: {alias!r}")


def _tree_size(root: Path) -> int:
    if not root.is_dir():
        raise FileNotFoundError(f"model snapshot does not exist: {root}")
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
