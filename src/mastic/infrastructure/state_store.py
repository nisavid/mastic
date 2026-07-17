"""SQLite WAL storage for mastic operational state."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping


_SCHEMA_LOCK = threading.Lock()
_SCHEMA_VERSION = 1
_SENSITIVE_KEYS = frozenset(
    {
        "messages",
        "prompt",
        "prompt_text",
        "prompts",
        "request_body",
        "response",
        "response_body",
        "response_text",
        "responses",
    }
)
_CREDENTIAL_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "api_token",
        "authorization",
        "bearer_token",
        "client_secret",
        "cookie",
        "credentials",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "set-cookie",
        "x-api-key",
        "token",
    }
)


class SensitiveContentError(ValueError):
    """Operational state attempted to persist inference content."""


class OperationalStateStore:
    """Persist deterministic operational DTOs in a private SQLite database."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._prepare_path()
        with _SCHEMA_LOCK, self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, _SCHEMA_VERSION):
                raise RuntimeError(f"unsupported operational schema version {version}")
            if version == 0:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS operations (
                        operation_id TEXT PRIMARY KEY,
                        dto_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                        kind TEXT NOT NULL,
                        dto_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS events_by_operation
                        ON events(operation_id, sequence);
                    CREATE TABLE IF NOT EXISTS snapshots (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        kind TEXT NOT NULL,
                        resource_id TEXT NOT NULL,
                        version TEXT NOT NULL,
                        dto_json TEXT NOT NULL,
                        UNIQUE(kind, resource_id, version)
                    );
                    CREATE TABLE IF NOT EXISTS metrics (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        kind TEXT NOT NULL,
                        dto_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS metrics_by_kind
                        ON metrics(kind, sequence);
                    PRAGMA user_version = 1;
                    COMMIT;
                    """
                )
        self._secure_files()

    def metadata(self) -> dict[str, object]:
        """Return stable schema and journaling metadata."""
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        return {"journal_mode": str(mode).lower(), "schema_version": int(version)}

    def put_operation(self, operation: Mapping[str, object]) -> dict[str, object]:
        """Create or replace one durable operation DTO by identity."""
        dto = _record(operation)
        operation_id = _identity(dto, "id", "operation")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO operations(operation_id, dto_json) VALUES (?, ?)
                ON CONFLICT(operation_id) DO UPDATE SET dto_json = excluded.dto_json
                """,
                (operation_id, _encode(dto)),
            )
        return dto

    def operation(self, operation_id: str) -> dict[str, object] | None:
        """Return one operation DTO, or ``None`` when it is unknown."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT dto_json FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return _decode(row[0]) if row else None

    def operations(self) -> tuple[dict[str, object], ...]:
        """Return operation DTOs ordered by identity."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT dto_json FROM operations ORDER BY operation_id"
            ).fetchall()
        return tuple(_decode(row[0]) for row in rows)

    def append_progress(
        self, operation_id: str, progress: Mapping[str, object]
    ) -> dict[str, object]:
        """Append a durable progress event for an operation."""
        dto = _record(progress)
        reserved = {"kind", "operation_id", "sequence"} & set(dto)
        if reserved:
            raise ValueError(
                f"progress uses reserved keys: {', '.join(sorted(reserved))}"
            )
        return self.append_event(
            {"kind": "progress", "operation_id": operation_id, "progress": dto}
        )

    def append_event(self, event: Mapping[str, object]) -> dict[str, object]:
        """Append an immutable event and assign its database sequence."""
        dto = _record(event)
        if "sequence" in dto:
            raise ValueError("event sequence is assigned by the state store")
        operation_id = _identity(dto, "operation_id", "event")
        kind = _identity(dto, "kind", "event")
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT INTO events(operation_id, kind, dto_json) VALUES (?, ?, ?)",
                (operation_id, kind, _encode(dto)),
            )
            sequence = int(cursor.lastrowid)
        return _record({**dto, "sequence": sequence})

    def events(self, operation_id: str | None = None) -> tuple[dict[str, object], ...]:
        """Return immutable events in sequence order."""
        if operation_id is None:
            sql = "SELECT sequence, dto_json FROM events ORDER BY sequence"
            parameters: tuple[object, ...] = ()
        else:
            sql = "SELECT sequence, dto_json FROM events WHERE operation_id = ? ORDER BY sequence"
            parameters = (operation_id,)
        with self._connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return tuple(_record({**_decode(row[1]), "sequence": row[0]}) for row in rows)

    def progress(self, operation_id: str) -> tuple[dict[str, object], ...]:
        """Return only progress events for an operation."""
        return tuple(
            event for event in self.events(operation_id) if event["kind"] == "progress"
        )

    def put_snapshot(self, snapshot: Mapping[str, object]) -> dict[str, object]:
        """Create or replace the latest versioned resource snapshot."""
        dto = _record(snapshot)
        kind = _identity(dto, "kind", "snapshot")
        resource_id = _identity(dto, "id", "snapshot")
        version = _snapshot_version(dto)
        encoded = _encode(dto)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO snapshots(kind, resource_id, version, dto_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(kind, resource_id, version)
                DO NOTHING
                """,
                (kind, resource_id, version, encoded),
            )
            if cursor.rowcount == 0:
                stored = connection.execute(
                    """
                    SELECT dto_json FROM snapshots
                    WHERE kind = ? AND resource_id = ? AND version = ?
                    """,
                    (kind, resource_id, version),
                ).fetchone()[0]
                if stored != encoded:
                    raise ValueError(
                        f"snapshot {kind}/{resource_id} version {dto['version']!r} "
                        "is immutable"
                    )
        return dto

    def snapshot(
        self, kind: str, resource_id: str, *, version: str | int | None = None
    ) -> dict[str, object] | None:
        """Return the latest or one exact versioned resource snapshot."""
        with self._connection() as connection:
            if version is None:
                row = connection.execute(
                    """
                    SELECT dto_json FROM snapshots
                    WHERE kind = ? AND resource_id = ?
                    ORDER BY sequence DESC LIMIT 1
                    """,
                    (kind, resource_id),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT dto_json FROM snapshots
                    WHERE kind = ? AND resource_id = ? AND version = ?
                    """,
                    (kind, resource_id, _encode_version(version)),
                ).fetchone()
        return _decode(row[0]) if row else None

    def snapshots(self, kind: str | None = None) -> tuple[dict[str, object], ...]:
        """Return latest snapshots in deterministic resource order."""
        if kind is None:
            sql = "SELECT dto_json FROM snapshots ORDER BY sequence"
            parameters: tuple[object, ...] = ()
        else:
            sql = "SELECT dto_json FROM snapshots WHERE kind = ? ORDER BY sequence"
            parameters = (kind,)
        with self._connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return tuple(_decode(row[0]) for row in rows)

    def record_metric(self, metric: Mapping[str, object]) -> dict[str, object]:
        """Append one content-free metric DTO."""
        dto = _record(metric)
        if "sequence" in dto:
            raise ValueError("metric sequence is assigned by the state store")
        kind = _identity(dto, "kind", "metric")
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT INTO metrics(kind, dto_json) VALUES (?, ?)",
                (kind, _encode(dto)),
            )
            sequence = int(cursor.lastrowid)
        return _record({**dto, "sequence": sequence})

    def metrics(self, kind: str | None = None) -> tuple[dict[str, object], ...]:
        """Return metrics in sequence order, optionally filtered by kind."""
        if kind is None:
            sql = "SELECT sequence, dto_json FROM metrics ORDER BY sequence"
            parameters: tuple[object, ...] = ()
        else:
            sql = "SELECT sequence, dto_json FROM metrics WHERE kind = ? ORDER BY sequence"
            parameters = (kind,)
        with self._connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return tuple(_record({**_decode(row[1]), "sequence": row[0]}) for row in rows)

    def _prepare_path(self) -> None:
        _prepare_private_directory(self._path.parent)
        descriptor = _open_private_file(self._path, os.O_RDWR | os.O_CREAT)
        os.close(descriptor)

    def _connect(self) -> sqlite3.Connection:
        self._validate_database_files()
        connection = sqlite3.connect(self._path, timeout=10)
        try:
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            return connection
        except Exception:
            connection.close()
            raise

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()
            self._secure_files()

    def _secure_files(self) -> None:
        self._validate_database_files(chmod=True)

    def _validate_database_files(self, *, chmod: bool = False) -> None:
        for path in (
            self._path,
            Path(f"{self._path}-wal"),
            Path(f"{self._path}-shm"),
            Path(f"{self._path}-journal"),
        ):
            try:
                descriptor = os.open(
                    path,
                    os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                )
            except FileNotFoundError:
                continue
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise OSError(
                        f"operational state target is not a regular file: {path}"
                    )
                if metadata.st_uid != os.getuid():
                    raise PermissionError(
                        f"operational state target is not user-owned: {path}"
                    )
                if chmod:
                    os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)


def _identity(dto: Mapping[str, object], key: str, noun: str) -> str:
    value = dto.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{noun} requires non-empty string '{key}'")
    return value


def _snapshot_version(dto: Mapping[str, object]) -> str:
    if "version" not in dto:
        raise ValueError("snapshot requires 'version'")
    return _encode_version(dto["version"])


def _encode_version(value: object) -> str:
    if not isinstance(value, str) and type(value) is not int:
        raise ValueError("snapshot version must be a string or integer")
    if value == "":
        raise ValueError("snapshot version must not be empty")
    return json.dumps(value, separators=(",", ":"))


def _record(value: Mapping[str, object]) -> dict[str, object]:
    normalized = _normalize(value, ())
    if not isinstance(normalized, dict):
        raise TypeError("operational DTO must be a mapping")
    return normalized


def _normalize(value: object, path: tuple[str, ...]) -> object:
    if isinstance(value, Mapping):
        result = {}
        for raw_key in sorted(value, key=str):
            if not isinstance(raw_key, str):
                raise TypeError("operational DTO keys must be strings")
            if raw_key.casefold() in _SENSITIVE_KEYS:
                location = ".".join((*path, raw_key))
                raise SensitiveContentError(
                    f"operational state cannot persist inference content at {location}"
                )
            normalized_key = raw_key.casefold()
            if normalized_key in _CREDENTIAL_KEYS or (
                normalized_key.endswith("_token") and normalized_key != "birth_token"
            ):
                location = ".".join((*path, raw_key))
                raise SensitiveContentError(
                    "operational state cannot persist credential material at "
                    f"{location}"
                )
            result[raw_key] = _normalize(value[raw_key], (*path, raw_key))
        return result
    if isinstance(value, (list, tuple)):
        return [_normalize(item, path) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise TypeError(f"operational DTO contains unsupported value {value!r}")


def _prepare_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"private state path is not a directory: {path}")
    if metadata.st_uid != os.getuid():
        raise PermissionError(f"private state path is not user-owned: {path}")
    os.chmod(path, 0o700, follow_symlinks=False)


def _open_private_file(path: Path, flags: int) -> int:
    descriptor = os.open(
        path,
        flags | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"operational state target is not a regular file: {path}")
        if metadata.st_uid != os.getuid():
            raise PermissionError(f"operational state target is not user-owned: {path}")
        os.fchmod(descriptor, 0o600)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _encode(dto: Mapping[str, object]) -> str:
    return json.dumps(dto, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _decode(payload: str) -> dict[str, object]:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise RuntimeError("stored operational DTO is not an object")
    return _record(value)
