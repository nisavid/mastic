"""Hugging Face backed model discovery, installation, and cache supply."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from hashlib import sha1, sha256
from pathlib import Path
from typing import Protocol

from huggingface_hub.errors import CacheNotFound


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
        self,
        repo_id: str,
        revision: str,
        snapshot_path: Path,
        *,
        local_files_only: bool = False,
    ) -> VerificationResult: ...

    def cache_inventory(self) -> CacheInventory: ...

    def preview_cache_deletion(
        self, commit_hashes: tuple[str, ...]
    ) -> HubDeletionStrategy: ...


@dataclass(frozen=True)
class CacheDeletionPreview:
    """Reference-aware wrapper around the Hub's official deletion strategy."""

    allowed: bool
    revision_hashes: tuple[str, ...]
    blocked_by: tuple[str, ...]
    expected_freed_size: int
    _strategy: HubDeletionStrategy | None = field(
        default=None, repr=False, compare=False
    )

    def execute(
        self,
        *,
        installations: tuple[ModelInstallation, ...],
        approved: bool = False,
    ) -> None:
        if not self.allowed or self._strategy is None:
            raise ModelSupplyError("cache deletion preview is blocked by references")
        if not approved:
            raise PermissionError("cache deletion requires explicit approval")
        if any(
            installation.revision.commit_sha in self.revision_hashes
            for installation in installations
        ):
            raise ModelSupplyError("cache deletion is blocked by references")
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
            candidates = tuple(
                CatalogCandidate(
                    repo_id=revision.repo_id,
                    source="cache",
                    evidence=revision.evidence,
                    reported_sha=revision.commit_sha,
                )
                for revision in self._hub.cache_inventory().revisions
                if needle in revision.repo_id.casefold()
            )
            return candidates[:limit]
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
        verification = self._hub.verify_revision(
            repo_id,
            resolved.commit_sha,
            snapshot,
            local_files_only=offline,
        )
        if verification.status != "verified":
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
                force_download=True,
            )
            .expanduser()
            .resolve()
        )
        verification = self._hub.verify_revision(
            installation.revision.repo_id,
            installation.revision.commit_sha,
            snapshot,
        )
        if verification.status != "verified":
            details = "; ".join(verification.issues) or verification.status
            raise ModelSupplyError(f"exact revision repair failed: {details}")
        return verification

    def inventory(self) -> CacheInventory:
        return self._hub.cache_inventory()

    def preview_cache_deletion(
        self,
        commit_hashes: tuple[str, ...],
        *,
        installations: tuple[ModelInstallation, ...] = (),
    ) -> CacheDeletionPreview:
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
            return CacheDeletionPreview(
                allowed=False,
                revision_hashes=requested,
                blocked_by=blocked_by,
                expected_freed_size=0,
            )
        strategy = self._hub.preview_cache_deletion(requested)
        return CacheDeletionPreview(
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
        self,
        repo_id: str,
        revision: str,
        snapshot_path: Path,
        *,
        local_files_only: bool = False,
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
        if local_files_only:
            try:
                manifest = _load_verification_manifest(
                    expected, repo_id=repo_id, revision=revision
                )
            except (OSError, ValueError, json.JSONDecodeError) as error:
                return VerificationResult(
                    status="incomplete",
                    evidence="persisted-exact-manifest",
                    issues=(str(error),),
                )
            evidence = "persisted-exact-manifest"
        else:
            try:
                from huggingface_hub import HfApi

                info = HfApi().model_info(
                    repo_id,
                    revision=revision,
                    files_metadata=True,
                    timeout=10.0,
                )
                manifest_revision = getattr(info, "sha", None)
                if (
                    not isinstance(manifest_revision, str)
                    or manifest_revision.casefold() != revision.casefold()
                ):
                    return VerificationResult(
                        status="conflicting",
                        evidence="hub-exact-manifest",
                        issues=(
                            "Hub manifest identity did not match the exact revision",
                        ),
                    )
                manifest = _hub_manifest(tuple(getattr(info, "siblings", ())))
                _persist_verification_manifest(
                    expected, repo_id=repo_id, revision=revision, manifest=manifest
                )
            except Exception as error:
                return VerificationResult(
                    status="incomplete",
                    evidence="hub-exact-manifest",
                    issues=(str(error),),
                )
            evidence = "hub-exact-manifest"
        return _verify_exact_manifest(expected, manifest, evidence=evidence)

    def cache_inventory(self) -> CacheInventory:
        from huggingface_hub import scan_cache_dir

        try:
            cache_info = scan_cache_dir()
        except CacheNotFound:
            return CacheInventory((), "local-observed", ())
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

    def preview_cache_deletion(
        self, commit_hashes: tuple[str, ...]
    ) -> HubDeletionStrategy:
        from huggingface_hub import scan_cache_dir

        return scan_cache_dir().delete_revisions(*commit_hashes)


def _verify_exact_manifest(
    expected: Path,
    manifest: dict[str, tuple[int | None, str, str]],
    *,
    evidence: str,
) -> VerificationResult:
    try:
        actual = {
            path.relative_to(expected).as_posix(): path
            for path in expected.rglob("*")
            if path.is_file()
            and not path.relative_to(expected)
            .as_posix()
            .startswith(".cache/huggingface/")
        }
        issues = [
            *(f"missing:{path}" for path in sorted(set(manifest) - set(actual))),
            *(f"unexpected:{path}" for path in sorted(set(actual) - set(manifest))),
        ]
        for relative in sorted(set(manifest) & set(actual)):
            size, algorithm, digest = manifest[relative]
            path = actual[relative]
            if size is not None and path.stat().st_size != size:
                issues.append(f"size-mismatch:{relative}")
                continue
            observed = (
                _file_sha256(path) if algorithm == "sha256" else _git_blob_sha(path)
            )
            if observed != digest:
                issues.append(f"digest-mismatch:{relative}")
    except OSError as error:
        return VerificationResult("incomplete", evidence, (str(error),))
    if issues:
        return VerificationResult("incomplete", evidence, tuple(issues))
    return VerificationResult("verified", evidence, ())


_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{40,64}\Z")
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _validate_alias(alias: str) -> None:
    if not _ALIAS.fullmatch(alias):
        raise ModelSupplyError(f"invalid model alias: {alias!r}")


def _verification_manifest_path(snapshot: Path) -> Path:
    cache_root = (
        snapshot.parent.parent
        if snapshot.parent.name == "snapshots"
        else snapshot.parent
    )
    return cache_root / ".mastic-manifests" / f"{snapshot.name}.json"


def _persist_verification_manifest(
    snapshot: Path,
    *,
    repo_id: str,
    revision: str,
    manifest: dict[str, tuple[int | None, str, str]],
) -> None:
    path = _verification_manifest_path(snapshot)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise OSError("model verification manifest path is unsafe")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "schema_version": 1,
        "repository": repo_id,
        "revision": revision,
        "files": {
            name: {"size": size, "algorithm": algorithm, "digest": digest}
            for name, (size, algorithm, digest) in sorted(manifest.items())
        },
    }
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_verification_manifest(
    snapshot: Path, *, repo_id: str, revision: str
) -> dict[str, tuple[int | None, str, str]]:
    path = _verification_manifest_path(snapshot)
    if path.is_symlink() or not path.is_file():
        raise OSError("persisted exact-revision manifest is unavailable")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("repository") != repo_id
        or str(value.get("revision", "")).casefold() != revision.casefold()
        or not isinstance(value.get("files"), dict)
    ):
        raise ValueError("persisted exact-revision manifest is invalid")
    result: dict[str, tuple[int | None, str, str]] = {}
    for name, item in value["files"].items():
        if (
            not isinstance(name, str)
            or not isinstance(item, dict)
            or not isinstance(item.get("algorithm"), str)
            or not isinstance(item.get("digest"), str)
            or (
                item.get("size") is not None
                and (not isinstance(item.get("size"), int) or item["size"] < 0)
            )
        ):
            raise ValueError("persisted exact-revision manifest is invalid")
        result[name] = (item.get("size"), item["algorithm"], item["digest"])
    if not result:
        raise ValueError("persisted exact-revision manifest is empty")
    return result


def _hub_manifest(
    siblings: tuple[object, ...],
) -> dict[str, tuple[int | None, str, str]]:
    manifest: dict[str, tuple[int | None, str, str]] = {}
    for item in siblings:
        relative = getattr(item, "rfilename", None)
        if not isinstance(relative, str):
            raise ModelSupplyError("Hub manifest file omitted its path")
        path = Path(relative)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ModelSupplyError("Hub manifest contains an unsafe path")
        if relative in manifest:
            raise ModelSupplyError("Hub manifest contains a duplicate path")
        size = getattr(item, "size", None)
        if type(size) is not int or size < 0:
            size = None
        lfs = getattr(item, "lfs", None)
        lfs_digest = (
            lfs.get("sha256") if isinstance(lfs, dict) else getattr(lfs, "sha256", None)
        )
        blob_digest = getattr(item, "blob_id", None)
        if isinstance(lfs_digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", lfs_digest):
            algorithm, digest = "sha256", lfs_digest.casefold()
        elif isinstance(blob_digest, str) and re.fullmatch(
            r"[0-9a-fA-F]{40}", blob_digest
        ):
            algorithm, digest = "git-blob-sha1", blob_digest.casefold()
        else:
            raise ModelSupplyError(
                f"Hub manifest file lacks an exact content digest: {relative}"
            )
        manifest[relative] = (size, algorithm, digest)
    if not manifest:
        raise ModelSupplyError("Hub manifest is empty")
    return manifest


def _git_blob_sha(path: Path) -> str:
    digest = sha1(usedforsecurity=False)
    digest.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_size(root: Path) -> int:
    if not root.is_dir():
        raise FileNotFoundError(f"model snapshot does not exist: {root}")
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
