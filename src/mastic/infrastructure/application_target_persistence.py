"""Private, atomic persistence primitives for application-target ownership."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
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
    _safe_target(path, "managed application-target file")
    try:
        return path.read_bytes(), True
    except FileNotFoundError:
        return b"", False


def _support_snapshot(
    manifest_path: Path, backup_path: Path
) -> tuple[tuple[bytes, bool], tuple[bytes, bool]]:
    return _read(manifest_path), _read(backup_path)


def _restore_support(
    manifest_path: Path,
    backup_path: Path,
    snapshot: tuple[tuple[bytes, bool], tuple[bytes, bool]],
) -> None:
    for path, (payload, existed) in zip(
        (manifest_path, backup_path), snapshot, strict=True
    ):
        if existed:
            _atomic_replace(path, payload)
        else:
            path.unlink(missing_ok=True)


def _write_private(path: Path, payload: bytes) -> None:
    _atomic_replace(path, payload)


def _snapshot_files(*paths: Path) -> tuple[tuple[Path, bytes, bool], ...]:
    return tuple((path, *_read(path)) for path in paths)


def _restore_files(snapshot: tuple[tuple[Path, bytes, bool], ...]) -> None:
    for path, payload, existed in snapshot:
        if existed:
            _atomic_replace(path, payload)
        else:
            path.unlink(missing_ok=True)


def _atomic_replace(path: Path, payload: bytes) -> None:
    _safe_target(path, "managed application-target file")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


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


def _plain(value: object) -> object:
    if hasattr(value, "unwrap"):
        return value.unwrap()  # type: ignore[no-any-return,union-attr]
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value
