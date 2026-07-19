"""Private, round-trip-safe desired-state storage."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Generic, Iterator, Mapping, Sequence, TypeVar

import tomlkit
from tomlkit.toml_document import TOMLDocument


ValidatedConfig = TypeVar("ValidatedConfig")
ConfigValidator = Callable[[Mapping[str, object]], ValidatedConfig]
_JOURNAL_SCHEMA_VERSION = 1
_JOURNAL_KEYS = frozenset(
    {"action", "previous_revision", "revision", "saved_at", "schema_version"}
)


@dataclass(frozen=True, slots=True)
class ConfigSnapshot(Generic[ValidatedConfig]):
    """One validated desired-state document and its semantic value."""

    document: TOMLDocument
    value: ValidatedConfig
    revision: str


@dataclass(frozen=True, slots=True)
class ConfigChange:
    """One semantic difference between current and candidate desired state."""

    path: tuple[str | int, ...]
    before: object
    after: object


@dataclass(frozen=True, slots=True)
class ConfigRevision:
    """One durable desired-state journal entry."""

    schema_version: int
    revision: str
    previous_revision: str | None
    saved_at: str
    action: str


class ConfigStore(Generic[ValidatedConfig]):
    """Load and replace one private TOML desired-state document."""

    def __init__(
        self, path: str | Path, validator: ConfigValidator[ValidatedConfig]
    ) -> None:
        self._path = Path(path)
        self._validator = validator
        self._history_path = self._path.parent / f".{self._path.name}.history"
        self._journal_path = self._path.parent / f".{self._path.name}.journal.jsonl"
        self._prepare_directory()

    def load(self) -> ConfigSnapshot[ValidatedConfig]:
        """Load and validate the current document without changing it."""
        with self._locked():
            self._reconcile_journal_locked()
            return self._snapshot(_read_private_file(self._path).decode("utf-8"))

    @property
    def exists(self) -> bool:
        """Return whether desired state has been initialized, without creating it."""

        return _private_file_exists(self._path)

    def save(
        self, document: TOMLDocument, *, action: str = "save"
    ) -> ConfigSnapshot[ValidatedConfig]:
        """Validate and atomically replace the desired-state document."""
        text = document.as_string()
        snapshot = self._snapshot(text)
        with self._locked():
            self._save_locked(snapshot, action)
        return snapshot

    def edit(
        self, mutation: Callable[[TOMLDocument], object]
    ) -> ConfigSnapshot[ValidatedConfig]:
        """Apply one locked semantic edit to the latest desired state."""
        with self._locked():
            snapshot = self._snapshot(_read_private_file(self._path).decode("utf-8"))
            mutation(snapshot.document)
            edited = self._snapshot(snapshot.document.as_string())
            self._save_locked(edited, "edit")
            return edited

    def import_text(self, text: str) -> ConfigSnapshot[ValidatedConfig]:
        """Validate and atomically import a TOML document."""
        snapshot = self._snapshot(text)
        return self.save(snapshot.document)

    def export_text(self) -> str:
        """Export the current document exactly as stored."""
        with self._locked():
            return _read_private_file(self._path).decode("utf-8")

    def diff(self, candidate: TOMLDocument) -> tuple[ConfigChange, ...]:
        """Compare a candidate with current desired state, ignoring TOML trivia."""
        current = self.load().document.unwrap()
        return tuple(_semantic_diff(current, candidate.unwrap()))

    def history(self) -> tuple[ConfigRevision, ...]:
        """Return committed revision entries in journal order."""
        with self._locked():
            return self._reconcile_journal_locked()

    def restore(self, revision: str) -> ConfigSnapshot[ValidatedConfig]:
        """Restore an exact archived desired-state revision."""
        archive_path = self._archive_path(revision)
        try:
            text = _read_private_file(archive_path).decode("utf-8")
        except FileNotFoundError as error:
            raise KeyError(f"unknown config revision {revision}") from error
        if _revision(text.encode()) != revision:
            raise RuntimeError(f"config revision {revision} failed integrity check")
        snapshot = self._snapshot(text)
        return self.save(snapshot.document, action="restore")

    def _snapshot(self, text: str) -> ConfigSnapshot[ValidatedConfig]:
        document = tomlkit.parse(text)
        value = self._validator(document)
        return ConfigSnapshot(
            document=document, value=value, revision=_revision(text.encode())
        )

    def _prepare_directory(self) -> None:
        _prepare_private_directory(self._path.parent)
        _prepare_private_directory(self._history_path)
        _private_file_exists(self._path)
        _private_file_exists(self._journal_path)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self._path.with_suffix(f"{self._path.suffix}.lock")
        with private_file_lock(lock_path):
            yield

    @staticmethod
    def _atomic_replace(path: Path, payload: bytes) -> None:
        _private_file_exists(path)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
            os.chmod(path, 0o600, follow_symlinks=False)
            directory = os.open(
                path.parent,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _archive(self, revision: str, payload: bytes) -> None:
        archive_path = self._archive_path(revision)
        if _private_file_exists(archive_path):
            if _read_private_file(archive_path) != payload:
                raise RuntimeError(f"config revision collision for {revision}")
            return
        self._atomic_replace(archive_path, payload)

    def _archive_path(self, revision: str) -> Path:
        if len(revision) != 64 or any(
            character not in "0123456789abcdef" for character in revision
        ):
            raise KeyError(f"invalid config revision {revision}")
        return self._history_path / f"{revision}.toml"

    def _append_journal(self, revision: ConfigRevision) -> None:
        payload = json.dumps(
            {
                "action": revision.action,
                "previous_revision": revision.previous_revision,
                "revision": revision.revision,
                "saved_at": revision.saved_at,
                "schema_version": revision.schema_version,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        descriptor = _open_private_file(
            self._journal_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "ab", closefd=False) as stream:
                stream.write(f"{payload}\n".encode())
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)

    def _save_locked(
        self, snapshot: ConfigSnapshot[ValidatedConfig], action: str
    ) -> None:
        records = self._reconcile_journal_locked()
        payload = snapshot.document.as_string().encode()
        previous_revision = None
        if _private_file_exists(self._path):
            previous_payload = _read_private_file(self._path)
            previous_revision = _revision(previous_payload)
            if previous_revision == snapshot.revision:
                return
            self._archive(previous_revision, previous_payload)
        if records and records[-1].revision != previous_revision:
            raise RuntimeError("config journal is corrupt")
        self._atomic_replace(self._path, payload)
        self._archive(snapshot.revision, payload)
        self._append_journal(
            ConfigRevision(
                schema_version=_JOURNAL_SCHEMA_VERSION,
                revision=snapshot.revision,
                previous_revision=previous_revision,
                saved_at=datetime.now(UTC).isoformat(),
                action=action,
            )
        )

    def _reconcile_journal_locked(self) -> tuple[ConfigRevision, ...]:
        records = list(self._read_journal_locked())
        if not _private_file_exists(self._path):
            return tuple(records)
        payload = _read_private_file(self._path)
        current_revision = _revision(payload)
        if records and records[-1].revision == current_revision:
            return tuple(records)
        self._archive(current_revision, payload)
        recovered = ConfigRevision(
            schema_version=_JOURNAL_SCHEMA_VERSION,
            revision=current_revision,
            previous_revision=records[-1].revision if records else None,
            saved_at=datetime.now(UTC).isoformat(),
            action="recovered",
        )
        self._append_journal(recovered)
        records.append(recovered)
        return tuple(records)

    def _read_journal_locked(self) -> tuple[ConfigRevision, ...]:
        if not _private_file_exists(self._journal_path):
            return ()
        payload = _read_private_file(self._journal_path)
        records = []
        previous_revision: str | None = None
        valid_bytes = 0
        lines = payload.splitlines(keepends=True)
        for index, line in enumerate(lines):
            complete = line.endswith(b"\n")
            try:
                raw = json.loads(line) if complete else None
                if not isinstance(raw, dict):
                    raise ValueError("journal entry must be an object")
                record = _parse_journal_revision(raw, previous_revision)
                archive = _read_private_file(self._archive_path(record.revision))
                if _revision(archive) != record.revision:
                    raise ValueError("journal archive digest does not match")
                records.append(record)
                previous_revision = record.revision
            except (
                json.JSONDecodeError,
                KeyError,
                OSError,
                TypeError,
                ValueError,
            ) as error:
                if index != len(lines) - 1 or complete:
                    raise RuntimeError("config journal is corrupt") from error
                self._truncate_journal(valid_bytes)
                break
            valid_bytes += len(line)
        return tuple(records)

    def _truncate_journal(self, length: int) -> None:
        descriptor = _open_private_file(self._journal_path, os.O_RDWR)
        try:
            os.fchmod(descriptor, 0o600)
            os.ftruncate(descriptor, length)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def private_file_lock(path: str | Path) -> Iterator[None]:
    """Hold one user-owned, cross-process advisory file lock."""

    lock_path = Path(path)
    _prepare_private_directory(lock_path.parent)
    descriptor = _open_private_file(lock_path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _prepare_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"private config path is not a directory: {path}")
    if metadata.st_uid != os.getuid():
        raise PermissionError(f"private config path is not user-owned: {path}")
    os.chmod(path, 0o700, follow_symlinks=False)


def _private_file_exists(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"private config target is not a regular file: {path}")
    if metadata.st_uid != os.getuid():
        raise PermissionError(f"private config target is not user-owned: {path}")
    return True


def _open_private_file(path: Path, flags: int) -> int:
    descriptor = os.open(
        path,
        flags | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"private config target is not a regular file: {path}")
        if metadata.st_uid != os.getuid():
            raise PermissionError(f"private config target is not user-owned: {path}")
        os.fchmod(descriptor, 0o600)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _read_private_file(path: Path) -> bytes:
    descriptor = _open_private_file(path, os.O_RDONLY)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _revision(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _parse_journal_revision(
    raw: Mapping[str, object], previous_revision: str | None
) -> ConfigRevision:
    if set(raw) != _JOURNAL_KEYS:
        raise ValueError("journal entry has an invalid schema")
    if raw.get("schema_version") != _JOURNAL_SCHEMA_VERSION:
        raise ValueError("journal entry has an unsupported schema version")
    revision = raw.get("revision")
    if not _is_revision(revision):
        raise ValueError("journal entry revision is invalid")
    recorded_previous = raw.get("previous_revision")
    if recorded_previous != previous_revision:
        raise ValueError("journal entry lineage is invalid")
    action = raw.get("action")
    saved_at = raw.get("saved_at")
    if not isinstance(action, str) or not action:
        raise ValueError("journal entry action is invalid")
    if not isinstance(saved_at, str):
        raise ValueError("journal entry timestamp is invalid")
    timestamp = datetime.fromisoformat(saved_at)
    if timestamp.tzinfo is None:
        raise ValueError("journal entry timestamp must include a timezone")
    return ConfigRevision(
        schema_version=_JOURNAL_SCHEMA_VERSION,
        revision=revision,
        previous_revision=previous_revision,
        saved_at=saved_at,
        action=action,
    )


def _is_revision(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _semantic_diff(
    before: object, after: object, path: tuple[str | int, ...] = ()
) -> Iterator[ConfigChange]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for key in sorted(set(before) | set(after), key=str):
            if key not in before:
                yield ConfigChange((*path, str(key)), None, _plain(after[key]))
            elif key not in after:
                yield ConfigChange((*path, str(key)), _plain(before[key]), None)
            else:
                yield from _semantic_diff(before[key], after[key], (*path, str(key)))
        return
    if (
        isinstance(before, Sequence)
        and not isinstance(before, (str, bytes))
        and isinstance(after, Sequence)
        and not isinstance(after, (str, bytes))
    ):
        for index in range(max(len(before), len(after))):
            if index >= len(before):
                yield ConfigChange((*path, index), None, _plain(after[index]))
            elif index >= len(after):
                yield ConfigChange((*path, index), _plain(before[index]), None)
            else:
                yield from _semantic_diff(before[index], after[index], (*path, index))
        return
    if before != after:
        yield ConfigChange(path, _plain(before), _plain(after))


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_plain(item) for item in value]
    return value
