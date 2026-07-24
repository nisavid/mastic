"""Secure staging and installed-byte proof for the Codex artifact closure."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from mastic.application.external_application_lifecycle import (
    OwnerUpgradeCommandError,
    VerifiedArtifact,
    VerifiedArtifactClosure,
)
from mastic.domain.canonical import canonical_fingerprint
from mastic.domain.external_applications import CurrentReleaseResolution
from mastic.infrastructure.codex_npm_authority import (
    BoundedHttpsFetcher,
    HttpFetcher,
    _RejectRedirects,
)


_PACKUMENT_URL = "https://registry.npmjs.org/@openai%2Fcodex"
_PACKUMENT_MAX_BYTES = 8 * 1024 * 1024
_MAIN_MAX_BYTES = 64 * 1024 * 1024
_PLATFORM_MAX_BYTES = 512 * 1024 * 1024
_ARCHIVE_MAX_FILES = 20_000
_ARCHIVE_MAX_UNCOMPRESSED_BYTES = 768 * 1024 * 1024
_PACKAGE_JSON_MAX_BYTES = 1024 * 1024
_NPM_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)


class ArtifactClosureMaterializationError(OwnerUpgradeCommandError):
    """Content-free artifact closure materialization failure."""


@dataclass(frozen=True, slots=True)
class DownloadedArtifact:
    status: int
    final_url: str
    size: int
    sha512_digest: str


class ArtifactDownloader(Protocol):
    def download(
        self,
        url: str,
        destination: Path,
        *,
        maximum_bytes: int,
    ) -> DownloadedArtifact: ...


class ExactCommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> int: ...


class PayloadRoots(Protocol):
    def locate(self, observation: object) -> Mapping[str, Path]: ...


class BoundedHttpsDownloader:
    """Stream one redirect-free HTTPS response into an exclusive regular file."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 60.0,
        opener=None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("download timeout must be positive")
        self._timeout = timeout_seconds
        self._opener = opener or urllib.request.build_opener(_RejectRedirects())

    def download(
        self,
        url: str,
        destination: Path,
        *,
        maximum_bytes: int,
    ) -> DownloadedArtifact:
        _validate_registry_tarball_url(url)
        if maximum_bytes <= 0:
            raise ValueError("download bound must be positive")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(destination, flags, 0o600)
        digest = hashlib.sha512()
        size = 0
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/octet-stream", "User-Agent": "mastic/1"},
            method="GET",
        )
        try:
            output = os.fdopen(descriptor, "wb")
            descriptor = -1
            with (
                output,
                self._opener.open(request, timeout=self._timeout) as response,
            ):
                content_length = response.headers.get("Content-Length")
                if content_length is not None and int(content_length) > maximum_bytes:
                    raise OSError("artifact response exceeds configured bound")
                while True:
                    chunk = response.read(min(1024 * 1024, maximum_bytes - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > maximum_bytes:
                        raise OSError("artifact response exceeds configured bound")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
                return DownloadedArtifact(
                    status=int(response.status),
                    final_url=str(response.geturl()),
                    size=size,
                    sha512_digest="sha512:" + digest.hexdigest(),
                )
        except (OSError, TimeoutError, ValueError, urllib.error.URLError):
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                # Cleanup failure must not replace the original download failure.
                pass
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)


@dataclass(frozen=True, slots=True)
class _SelectedArtifact:
    exact_release: str
    coordinate: str
    digest: str

    @property
    def stable_identity(self) -> tuple[str, str, str]:
        return self.exact_release, self.coordinate, self.digest


class NpmCodexArtifactClosureMaterializer:
    """Retain the stable wrapper and platform archives selected by npm metadata."""

    def __init__(
        self,
        *,
        stage_root: Path,
        fetcher: HttpFetcher | None = None,
        downloader: ArtifactDownloader | None = None,
    ) -> None:
        self._stage_root = Path(stage_root)
        self._fetcher = fetcher or BoundedHttpsFetcher()
        self._downloader = downloader or BoundedHttpsDownloader()

    def materialize(
        self, resolution: CurrentReleaseResolution
    ) -> VerifiedArtifactClosure:
        if (
            resolution.owner_identity
            not in {"vite-plus/npm-global", "vite-plus/global-package"}
            or resolution.platform != "darwin"
            or resolution.architecture != "arm64"
        ):
            raise ArtifactClosureMaterializationError("unsupported_closure_subject")
        before = self._packument()
        primary = _select_version(before, resolution.exact_release)
        if (
            primary.coordinate != resolution.artifact_coordinate
            or primary.digest != resolution.artifact_digest
        ):
            raise ArtifactClosureMaterializationError("primary_resolution_changed")
        _prepare_private_directory(self._stage_root)
        stage = Path(tempfile.mkdtemp(prefix="codex-closure-", dir=self._stage_root))
        os.chmod(stage, 0o700)
        cache = stage / "npm-cache"
        cache.mkdir(mode=0o700)
        try:
            primary_path = stage / "codex-primary.tgz"
            self._download(primary, primary_path, _MAIN_MAX_BYTES)
            primary_payload, package = _archive_payload(primary_path)
            if (
                package.get("name") != "@openai/codex"
                or package.get("version") != resolution.exact_release
            ):
                raise ArtifactClosureMaterializationError("primary_identity_mismatch")
            platform_release = _platform_release(package, resolution.exact_release)
            platform = _select_version(before, platform_release)
            platform_path = stage / "codex-darwin-arm64.tgz"
            self._download(platform, platform_path, _PLATFORM_MAX_BYTES)
            platform_payload, platform_package = _archive_payload(platform_path)
            if (
                platform_package.get("name") != "@openai/codex"
                or platform_package.get("version") != platform_release
            ):
                raise ArtifactClosureMaterializationError("platform_identity_mismatch")
            after = self._packument()
            after_primary = _select_version(after, resolution.exact_release)
            after_platform = _select_version(after, platform_release)
            if (
                primary.stable_identity != after_primary.stable_identity
                or platform.stable_identity != after_platform.stable_identity
            ):
                raise ArtifactClosureMaterializationError("authority_changed")
            return VerifiedArtifactClosure(
                application_identity="external-application:codex",
                exact_release=resolution.exact_release,
                artifacts=(
                    VerifiedArtifact(
                        role="primary",
                        package_identity="@openai/codex",
                        exact_release=resolution.exact_release,
                        coordinate=primary.coordinate,
                        archive_digest=primary.digest,
                        installed_payload_digest=primary_payload,
                        staged_path=primary_path,
                    ),
                    VerifiedArtifact(
                        role="platform",
                        package_identity="@openai/codex-darwin-arm64",
                        exact_release=platform_release,
                        coordinate=platform.coordinate,
                        archive_digest=platform.digest,
                        installed_payload_digest=platform_payload,
                        staged_path=platform_path,
                    ),
                ),
                staging_directory=stage,
                cache_directory=cache,
            )
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def release(self, closure: VerifiedArtifactClosure) -> None:
        """Remove one exact materializer-owned staging lease idempotently."""

        stage = closure.staging_directory
        if not stage.exists():
            return
        try:
            root = self._stage_root.resolve(strict=True)
            metadata = stage.lstat()
            if (
                stage.parent.resolve(strict=True) != root
                or not stage.name.startswith("codex-closure-")
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
            ):
                raise OSError("closure staging lease is not materializer-owned")
        except OSError as error:
            raise OwnerUpgradeCommandError("artifact_release_refused") from error
        try:
            shutil.rmtree(stage)
        except OSError as error:
            raise OwnerUpgradeCommandError("artifact_release_failed") from error

    def _packument(self) -> bytes:
        try:
            response = self._fetcher.fetch(
                _PACKUMENT_URL, maximum_bytes=_PACKUMENT_MAX_BYTES
            )
        except OSError as error:
            raise ArtifactClosureMaterializationError(
                "authority_unavailable"
            ) from error
        if response.status != 200 or response.final_url != _PACKUMENT_URL:
            raise ArtifactClosureMaterializationError("authority_invalid")
        return response.body

    def _download(
        self, selected: _SelectedArtifact, destination: Path, maximum_bytes: int
    ) -> None:
        try:
            downloaded = self._downloader.download(
                selected.coordinate, destination, maximum_bytes=maximum_bytes
            )
        except OSError as error:
            raise ArtifactClosureMaterializationError("artifact_unavailable") from error
        if (
            downloaded.status != 200
            or downloaded.final_url != selected.coordinate
            or downloaded.sha512_digest != selected.digest
        ):
            raise ArtifactClosureMaterializationError("artifact_digest_mismatch")


class CodexViteArtifactClosureVerifier:
    """Seed a private cache and prove staged and installed payload bytes."""

    def __init__(
        self,
        *,
        vp_home: Path,
        roots: PayloadRoots,
        runner: ExactCommandRunner,
        base_environment: Mapping[str, str],
        timeout_seconds: float = 5 * 60,
    ) -> None:
        self._vp_home = Path(vp_home)
        self._roots = roots
        self._runner = runner
        self._environment = {
            key: value
            for key, value in base_environment.items()
            if key in {"HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR"}
        }
        if "HOME" not in self._environment:
            raise ValueError("artifact cache preparation requires HOME")
        self._timeout = timeout_seconds

    def prepare(
        self,
        closure: VerifiedArtifactClosure,
        owner_runtime_identity: str,
    ) -> None:
        self.verify_staged(closure)
        runtime = owner_runtime_identity.removeprefix("node:")
        if (
            f"node:{runtime}" != owner_runtime_identity
            or _NPM_VERSION.fullmatch(runtime) is None
        ):
            raise OwnerUpgradeCommandError("owner_runtime_invalid")
        _require_private_directory(closure.cache_directory)
        user_config = closure.staging_directory / "npm-user.config"
        global_config = closure.staging_directory / "npm-global.config"
        user_config_identity = _create_empty_private_file(user_config)
        try:
            global_config_identity = _create_empty_private_file(global_config)
        except OwnerUpgradeCommandError:
            _unlink_created_private_file(user_config, user_config_identity)
            raise
        try:
            environment = {
                **self._environment,
                "PATH": f"{self._vp_home / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
                "VP_HOME": str(self._vp_home),
                "NO_COLOR": "1",
                "NPM_CONFIG_CACHE": str(closure.cache_directory),
                "NPM_CONFIG_OFFLINE": "true",
                "NPM_CONFIG_AUDIT": "false",
                "NPM_CONFIG_FUND": "false",
                "NPM_CONFIG_REGISTRY": "https://registry.npmjs.org/",
                "NPM_CONFIG_USERCONFIG": str(user_config),
                "NPM_CONFIG_GLOBALCONFIG": str(global_config),
                "NPM_CONFIG_STRICT_SSL": "true",
            }
            for artifact in closure.artifacts:
                returncode = self._runner.run(
                    (
                        str(self._vp_home / "bin" / "vp"),
                        "env",
                        "exec",
                        "--node",
                        runtime,
                        "--",
                        str(self._vp_home / "bin" / "npm"),
                        "cache",
                        "add",
                        "--cache",
                        str(closure.cache_directory),
                        "--offline",
                        "--",
                        str(artifact.staged_path),
                    ),
                    cwd=closure.staging_directory,
                    environment=environment,
                    timeout_seconds=self._timeout,
                )
                if returncode != 0:
                    raise OwnerUpgradeCommandError("artifact_cache_prepare_failed")
        finally:
            _unlink_created_private_file(global_config, global_config_identity)
            _unlink_created_private_file(user_config, user_config_identity)

    def verify_staged(self, closure: VerifiedArtifactClosure) -> None:
        _require_private_directory(closure.staging_directory)
        for artifact in closure.artifacts:
            try:
                metadata = artifact.staged_path.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    raise OwnerUpgradeCommandError("staged_archive_changed")
                if _file_sha512(artifact.staged_path) != artifact.archive_digest:
                    raise OwnerUpgradeCommandError("staged_archive_changed")
                payload_digest, _package = _archive_payload(artifact.staged_path)
                if payload_digest != artifact.installed_payload_digest:
                    raise OwnerUpgradeCommandError("staged_payload_changed")
            except OwnerUpgradeCommandError:
                raise
            except OSError as error:
                raise OwnerUpgradeCommandError("staged_archive_changed") from error

    def verify_installed(
        self,
        closure: VerifiedArtifactClosure,
        observation: object,
    ) -> None:
        roots = self._roots.locate(observation)
        with _open_installed_roots(self._vp_home, roots) as root_fds:
            _validate_installed_topology(roots, root_fds)
            for artifact in closure.artifacts:
                root_fd = root_fds.get(artifact.role)
                if (
                    root_fd is None
                    or _installed_payload_fd(root_fd)
                    != artifact.installed_payload_digest
                ):
                    raise OwnerUpgradeCommandError("installed_payload_mismatch")


def _select_version(payload: bytes, exact_release: str) -> _SelectedArtifact:
    try:
        data = json.loads(payload)
        versions = data["versions"]
        package = versions[exact_release]
        dist = package["dist"]
        coordinate = dist["tarball"]
        integrity = dist["integrity"]
        if (
            data.get("name") != "@openai/codex"
            or package.get("name") != "@openai/codex"
            or package.get("version") != exact_release
            or _NPM_VERSION.fullmatch(exact_release) is None
            or not isinstance(coordinate, str)
            or not isinstance(integrity, str)
        ):
            raise ValueError
        _validate_registry_tarball_url(coordinate)
        digest = _sri_digest(integrity)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ArtifactClosureMaterializationError("authority_invalid") from error
    return _SelectedArtifact(exact_release, coordinate, digest)


def _sri_digest(value: str) -> str:
    if not value.startswith("sha512-") or " " in value:
        raise ValueError
    decoded = base64.b64decode(value.removeprefix("sha512-"), validate=True)
    if len(decoded) != 64:
        raise ValueError
    return "sha512:" + decoded.hex()


def _platform_release(package: Mapping[str, object], main_release: str) -> str:
    dependencies = package.get("optionalDependencies")
    expected = f"npm:@openai/codex@{main_release}-darwin-arm64"
    if (
        not isinstance(dependencies, Mapping)
        or dependencies.get("@openai/codex-darwin-arm64") != expected
    ):
        raise ArtifactClosureMaterializationError("platform_dependency_mismatch")
    return f"{main_release}-darwin-arm64"


def _archive_payload(path: Path) -> tuple[str, Mapping[str, object]]:
    try:
        return _archive_payload_checked(path)
    except ArtifactClosureMaterializationError:
        raise
    except (OSError, EOFError, tarfile.TarError) as error:
        raise ArtifactClosureMaterializationError("archive_invalid") from error


def _archive_payload_checked(path: Path) -> tuple[str, Mapping[str, object]]:
    records: list[dict[str, object]] = []
    seen_files: set[str] = set()
    package_json: bytes | None = None
    total = 0
    with tarfile.open(path, mode="r:gz") as archive:
        for index, member in enumerate(archive, start=1):
            if index > _ARCHIVE_MAX_FILES:
                raise ArtifactClosureMaterializationError("archive_invalid")
            archive_path = PurePosixPath(member.name)
            if (
                archive_path.is_absolute()
                or ".." in archive_path.parts
                or member.issym()
                or member.islnk()
                or member.isdev()
                or member.isfifo()
            ):
                raise ArtifactClosureMaterializationError("archive_invalid")
            if not member.isfile():
                continue
            total += member.size
            if total > _ARCHIVE_MAX_UNCOMPRESSED_BYTES:
                raise ArtifactClosureMaterializationError("archive_invalid")
            if not archive_path.parts or archive_path.parts[0] != "package":
                raise ArtifactClosureMaterializationError("archive_invalid")
            relative = PurePosixPath(*archive_path.parts[1:])
            if not relative.parts or "node_modules" in relative.parts:
                continue
            relative_text = relative.as_posix()
            if relative_text in seen_files:
                raise ArtifactClosureMaterializationError("archive_invalid")
            seen_files.add(relative_text)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ArtifactClosureMaterializationError("archive_invalid")
            digest = hashlib.sha256()
            read = 0
            is_package_json = relative_text == "package.json"
            if is_package_json and member.size > _PACKAGE_JSON_MAX_BYTES:
                raise ArtifactClosureMaterializationError("archive_identity_invalid")
            chunks: list[bytes] | None = [] if is_package_json else None
            with extracted:
                while True:
                    chunk = extracted.read(1024 * 1024)
                    if not chunk:
                        break
                    read += len(chunk)
                    digest.update(chunk)
                    if chunks is not None:
                        chunks.append(chunk)
            if read != member.size:
                raise ArtifactClosureMaterializationError("archive_invalid")
            records.append(
                {
                    "path": relative_text,
                    "sha256": "sha256:" + digest.hexdigest(),
                    "size": read,
                }
            )
            if chunks is not None:
                package_json = b"".join(chunks)
    if package_json is None or len(package_json) > _PACKAGE_JSON_MAX_BYTES:
        raise ArtifactClosureMaterializationError("archive_identity_missing")
    try:
        package = json.loads(package_json)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArtifactClosureMaterializationError("archive_identity_invalid") from error
    if not isinstance(package, Mapping):
        raise ArtifactClosureMaterializationError("archive_identity_invalid")
    return canonical_fingerprint(
        sorted(records, key=lambda item: item["path"])
    ), package


@contextmanager
def _open_installed_roots(
    vp_home: Path, roots: Mapping[str, Path]
) -> Iterator[Mapping[str, int]]:
    descriptors: dict[str, int] = {}
    try:
        flags = (
            os.O_RDONLY
            | os.O_CLOEXEC
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        canonical_home = vp_home.resolve(strict=True)
        home_fd = os.open(canonical_home, flags)
        try:
            for role, root in roots.items():
                try:
                    relative = root.relative_to(canonical_home)
                except ValueError:
                    relative = root.relative_to(vp_home.absolute())
                if not relative.parts or ".." in relative.parts:
                    raise ValueError("installed payload root escapes VP_HOME")
                descriptor = os.dup(home_fd)
                try:
                    for part in relative.parts:
                        next_descriptor = os.open(part, flags, dir_fd=descriptor)
                        os.close(descriptor)
                        descriptor = next_descriptor
                except BaseException:
                    os.close(descriptor)
                    raise
                descriptors[role] = descriptor
        finally:
            os.close(home_fd)
    except (OSError, ValueError) as error:
        for descriptor in descriptors.values():
            os.close(descriptor)
        raise OwnerUpgradeCommandError("installed_topology_mismatch") from error
    try:
        yield descriptors
    finally:
        for descriptor in descriptors.values():
            os.close(descriptor)


def _installed_payload_fd(root_fd: int) -> str:
    try:
        records: list[dict[str, object]] = []
        total = 0
        for directory, directory_names, file_names, directory_fd in os.fwalk(
            ".", follow_symlinks=False, dir_fd=root_fd
        ):
            for name in directory_names:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise OSError("installed payload contains a non-directory entry")
            directory_names[:] = sorted(
                name for name in directory_names if name != "node_modules"
            )
            for name in sorted(file_names):
                if len(records) >= _ARCHIVE_MAX_FILES:
                    raise OSError("installed payload exceeds verification bounds")
                digest, size = _file_sha256_at(
                    name,
                    directory_fd=directory_fd,
                    maximum_bytes=_ARCHIVE_MAX_UNCOMPRESSED_BYTES - total,
                )
                total += size
                if total > _ARCHIVE_MAX_UNCOMPRESSED_BYTES:
                    raise OSError("installed payload exceeds verification bounds")
                records.append(
                    {
                        "path": (Path(directory) / name).relative_to(".").as_posix(),
                        "sha256": digest,
                        "size": size,
                    }
                )
    except OSError as error:
        raise OwnerUpgradeCommandError("installed_payload_unavailable") from error
    return canonical_fingerprint(sorted(records, key=lambda item: item["path"]))


def _validate_installed_topology(
    roots: Mapping[str, Path], root_fds: Mapping[str, int]
) -> None:
    if set(roots) != {"primary", "platform"}:
        raise OwnerUpgradeCommandError("installed_topology_mismatch")
    primary = roots["primary"]
    platform = roots["platform"]
    if not primary.is_absolute() or not platform.is_absolute():
        raise OwnerUpgradeCommandError("installed_topology_mismatch")
    node_modules = primary / "node_modules"
    namespace = node_modules / "@openai"
    expected_platform = namespace / "codex-darwin-arm64"
    if platform.absolute() != expected_platform.absolute():
        raise OwnerUpgradeCommandError("installed_topology_mismatch")
    try:
        primary_fd = root_fds["primary"]
        platform_fd = root_fds["platform"]
        with (
            _open_directory_at(primary_fd, "node_modules") as node_modules_fd,
            _open_directory_at(node_modules_fd, "@openai") as namespace_fd,
            _open_directory_at(
                namespace_fd, "codex-darwin-arm64"
            ) as nested_platform_fd,
        ):
            nested = os.fstat(nested_platform_fd)
            selected = os.fstat(platform_fd)
            if (nested.st_dev, nested.st_ino) != (selected.st_dev, selected.st_ino):
                raise OSError("installed platform root does not match primary topology")
            if set(os.listdir(node_modules_fd)) != {"@openai"}:
                raise OSError("installed dependency topology contains extra entries")
            if set(os.listdir(namespace_fd)) != {"codex-darwin-arm64"}:
                raise OSError("installed dependency namespace contains extra entries")
    except (KeyError, OSError) as error:
        raise OwnerUpgradeCommandError("installed_topology_mismatch") from error


@contextmanager
def _open_directory_at(parent_fd: int, name: str) -> Iterator[int]:
    descriptor = os.open(
        name,
        os.O_RDONLY
        | os.O_CLOEXEC
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _file_sha256_at(
    name: str, *, directory_fd: int, maximum_bytes: int
) -> tuple[str, int]:
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_fd,
    )
    try:
        initial = os.fstat(descriptor)
        if (
            not stat.S_ISREG(initial.st_mode)
            or maximum_bytes < 0
            or initial.st_size > maximum_bytes
        ):
            raise OSError("installed payload contains a non-regular file")
        size = 0
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > maximum_bytes:
                raise OSError("installed payload exceeds verification bounds")
            digest.update(chunk)
        final = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if size != initial.st_size or any(
            getattr(initial, field) != getattr(final, field) for field in stable_fields
        ):
            raise OSError("installed payload changed during verification")
        return "sha256:" + digest.hexdigest(), size
    finally:
        os.close(descriptor)


def _file_sha512(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha512:" + digest.hexdigest()


def _prepare_private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    _require_private_directory(path)


def _require_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise OwnerUpgradeCommandError("artifact_directory_not_private") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_mode & 0o077
        or metadata.st_uid != os.geteuid()
    ):
        raise OwnerUpgradeCommandError("artifact_directory_not_private")


def _create_empty_private_file(path: Path) -> tuple[int, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    created_identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            created_identity = metadata.st_dev, metadata.st_ino
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise OSError("private file identity is invalid")
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if created_identity is None:
            raise OSError("private file identity is unavailable")
        return created_identity
    except OSError as error:
        if created_identity is not None:
            _unlink_created_private_file(path, created_identity)
        raise OwnerUpgradeCommandError("artifact_config_prepare_failed") from error


def _unlink_created_private_file(path: Path, identity: tuple[int, int]) -> None:
    try:
        current = path.lstat()
        if (current.st_dev, current.st_ino) == identity:
            path.unlink()
    except OSError:
        # Best-effort cleanup must not mask the primary preparation failure.
        pass


def _validate_registry_tarball_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "registry.npmjs.org"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/@openai/codex/-/codex-")
        or not parsed.path.endswith(".tgz")
    ):
        raise ValueError("Codex artifact URL is outside the registry scope")
