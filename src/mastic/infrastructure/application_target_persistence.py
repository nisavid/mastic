"""Private, atomic persistence primitives for application-target ownership."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Mapping

from mastic.infrastructure.application_target_contracts import (
    ApplicationTargetIntegrationConflict,
)


def _load_manifest(path: Path, integration: str, optional: bool) -> dict[str, object]:
    payload, existed = _read(path)
    if not existed:
        if optional:
            return {}
        raise FileNotFoundError(path)
    raw = json.loads(payload.decode())
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != 1
        or raw.get("integration") != integration
        or not isinstance(raw.get("fields"), list)
    ):
        raise ApplicationTargetIntegrationConflict(
            f"invalid {integration} ownership manifest"
        )
    return raw


def _validate_manifest_paths(
    manifest: Mapping[str, object], config_path: Path, backup_path: Path
) -> None:
    if not manifest:
        return
    if manifest.get("config_path") != str(config_path) or manifest.get(
        "backup_path"
    ) != str(backup_path):
        raise ApplicationTargetIntegrationConflict(
            "ownership manifest belongs to other paths"
        )


def _read(path: Path) -> tuple[bytes, bool]:
    try:
        with _open_parent(path, create=False) as (parent, name):
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent,
            )
            try:
                _validate_open_file(descriptor, "managed application-target file")
            except Exception:
                os.close(descriptor)
                raise
            with os.fdopen(descriptor, "rb") as stream:
                return stream.read(), True
    except FileNotFoundError:
        return b"", False


def _write_private(path: Path, payload: bytes) -> None:
    _atomic_replace(path, payload)


def _snapshot_files(*paths: Path) -> tuple[tuple[Path, bytes, bool], ...]:
    return tuple((path, *_read(path)) for path in paths)


def _restore_files(snapshot: tuple[tuple[Path, bytes, bool], ...]) -> None:
    for path, payload, existed in snapshot:
        if existed:
            _atomic_replace(path, payload)
        else:
            _unlink(path)


def _atomic_replace(path: Path, payload: bytes) -> None:
    with _open_parent(path, create=True) as (parent, name):
        _safe_target_at(parent, name, "managed application-target file")
        temporary_name = f".{name}.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary_name,
                name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
            )
            os.fsync(parent)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=parent)


def _unlink(path: Path) -> None:
    try:
        with _open_parent(path, create=False) as (parent, name):
            _safe_target_at(parent, name, "managed application-target file")
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)
    except FileNotFoundError:
        return


@contextmanager
def _open_parent(path: Path, *, create: bool) -> Iterator[tuple[int, str]]:
    if create:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.parent, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("managed application-target parent must be a directory")
        if metadata.st_uid != os.getuid():
            raise PermissionError(
                "managed application-target parent must be user-owned"
            )
        yield descriptor, path.name
    finally:
        os.close(descriptor)


def _validate_open_file(descriptor: int, label: str) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be a regular file")
    if metadata.st_uid != os.getuid():
        raise PermissionError(f"{label} must be user-owned")


def _safe_target_at(parent: int, name: str, label: str) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be a regular file")
    if metadata.st_uid != os.getuid():
        raise PermissionError(f"{label} must be user-owned")


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _safe_directory(path: Path, label: str) -> None:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    if path.exists() and not path.is_dir():
        raise ValueError(f"{label} must be a directory")


def _safe_target(path: Path, label: str) -> None:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    if path.exists() and not path.is_file():
        raise ValueError(f"{label} must be a regular file")
