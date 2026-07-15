"""Precise, reversible Codex and Hindsight Gateway integrations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, TypeVar
from urllib.parse import urlsplit

import tomlkit
from tomlkit.toml_document import TOMLDocument


@dataclass(frozen=True, slots=True)
class SamplingProfile:
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.temperature is not None and self.temperature < 0:
            raise ValueError("temperature must be nonnegative")
        if self.top_p is not None and not 0 < self.top_p <= 1:
            raise ValueError("top_p must be greater than zero and at most one")
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")

    def values(self) -> Mapping[str, object]:
        values: dict[str, object] = {}
        if self.temperature is not None:
            values["temperature"] = self.temperature
        if self.top_p is not None:
            values["top_p"] = self.top_p
        if self.max_tokens is not None:
            values["max_tokens"] = self.max_tokens
        return MappingProxyType(values)


@dataclass(frozen=True, slots=True)
class ClientConfiguration:
    gateway_endpoint: str
    service_name: str
    context_window: int | None = None
    sampling_profiles: Mapping[str, SamplingProfile] = field(default_factory=dict)
    codex_provider_id: str = "mlxctl-local"
    hindsight_provider: str = "openai"
    max_concurrent: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "sampling_profiles", MappingProxyType(dict(self.sampling_profiles))
        )
        endpoint = urlsplit(self.gateway_endpoint)
        try:
            address = ip_address(endpoint.hostname or "")
            port = endpoint.port
        except ValueError as error:
            raise ValueError(
                "Gateway endpoint must be a literal HTTP loopback URL"
            ) from error
        if (
            endpoint.scheme != "http"
            or not address.is_loopback
            or port is None
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("Gateway endpoint must be a literal HTTP loopback URL")
        if not self.service_name:
            raise ValueError("service_name is required")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", self.service_name):
            raise ValueError("service_name must be a Gateway route name")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", self.codex_provider_id):
            raise ValueError("codex_provider_id must be a TOML-safe identifier")
        if self.context_window is not None and self.context_window <= 0:
            raise ValueError("context_window must be positive")
        if self.max_concurrent <= 0:
            raise ValueError("max_concurrent must be positive")
        normalized = [
            re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
            for name in self.sampling_profiles
        ]
        if any(not name for name in normalized) or len(set(normalized)) != len(
            normalized
        ):
            raise ValueError(
                "sampling profile names must be distinct after normalization"
            )


@dataclass(frozen=True, slots=True)
class SemanticChange:
    path: tuple[str, ...]
    before: object
    after: object


@dataclass(frozen=True, slots=True)
class ClientApplyResult:
    changed: bool
    changes: tuple[SemanticChange, ...]
    backup_path: Path
    manifest_path: Path


@dataclass(frozen=True, slots=True)
class ClientRemovalResult:
    changed: bool
    changes: tuple[SemanticChange, ...]
    skipped_paths: tuple[tuple[str, ...], ...] = ()


class ClientIntegrationConflict(RuntimeError):
    """A managed field or snapshot changed outside mlxctl."""


Replace = Callable[[Path, bytes], None]
TestResult = TypeVar("TestResult")
TestRequest = Callable[[str, str, Mapping[str, object]], TestResult]


class CodexClientIntegration:
    """Manage only the Codex TOML fields recorded in an ownership manifest."""

    def __init__(
        self,
        config_path: str | Path,
        manifest_path: str | Path,
        backup_path: str | Path,
        *,
        replace: Replace | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.manifest_path = Path(manifest_path)
        self.backup_path = Path(backup_path)
        self._replace = replace or _atomic_replace

    def preview(self, configuration: ClientConfiguration) -> tuple[SemanticChange, ...]:
        document = _load_toml(self.config_path)
        return tuple(_toml_changes(document, _codex_fields(configuration)))

    def apply(self, configuration: ClientConfiguration) -> ClientApplyResult:
        raw, existed = _read(self.config_path)
        document = _parse_toml(raw)
        desired = _codex_fields(configuration)
        prior_manifest = self._manifest(optional=True)
        prior_fields = {
            tuple(item["path"]): item for item in prior_manifest.get("fields", [])
        }
        changes: list[SemanticChange] = []
        owned: list[dict[str, object]] = []
        for path, previous in prior_fields.items():
            if path in desired:
                continue
            present, current = _toml_lookup(document, path)
            if not present or _plain(current) != previous.get("after"):
                owned.append(previous)
                continue
            if previous["before_present"]:
                _toml_set(document, path, previous.get("before"))
                changes.append(
                    SemanticChange(path, _plain(current), previous.get("before"))
                )
            else:
                _toml_delete(document, path)
                changes.append(SemanticChange(path, _plain(current), None))

        for path, after in desired.items():
            present, current = _toml_lookup(document, path)
            plain_current = _plain(current) if present else None
            previous = prior_fields.get(path)
            if not present or plain_current != after:
                changes.append(SemanticChange(path, plain_current, after))
                _toml_set(document, path, after)
            if previous is not None:
                before_present = bool(previous["before_present"])
                before = previous.get("before")
            elif not present or plain_current != after:
                before_present, before = present, current
            else:
                continue
            owned.append(
                {
                    "path": list(path),
                    "before_present": before_present,
                    "before": _plain(before),
                    "after": _plain(after),
                }
            )

        if not changes:
            return ClientApplyResult(False, (), self.backup_path, self.manifest_path)

        rendered = document.as_string().encode()
        manifest = {
            "schema_version": 1,
            "integration": "codex",
            "config_path": str(self.config_path),
            "config_existed": (
                bool(prior_manifest.get("config_existed"))
                if prior_manifest
                else existed
            ),
            "backup_path": str(self.backup_path),
            "before_digest": (
                str(prior_manifest.get("before_digest"))
                if prior_manifest
                else _digest(raw)
            ),
            "applied_digest": _digest(rendered),
            "fields": owned,
        }
        support = _support_snapshot(self.manifest_path, self.backup_path)
        try:
            if not prior_manifest:
                _write_private(self.backup_path, raw)
            _write_private(self.manifest_path, _json_bytes(manifest))
            self._replace(self.config_path, rendered)
        except Exception:
            _restore_support(self.manifest_path, self.backup_path, support)
            raise
        return ClientApplyResult(
            True, tuple(changes), self.backup_path, self.manifest_path
        )

    def remove(self) -> ClientRemovalResult:
        manifest = self._manifest(optional=True)
        if not manifest:
            return ClientRemovalResult(False, ())
        document = _load_toml(self.config_path)
        changes: list[SemanticChange] = []
        skipped: list[tuple[str, ...]] = []
        retained = []
        for item in manifest["fields"]:
            path = tuple(item["path"])
            present, current = _toml_lookup(document, path)
            if not present or _plain(current) != item.get("after"):
                skipped.append(path)
                retained.append(item)
                continue
            if item["before_present"]:
                _toml_set(document, path, item.get("before"))
                changes.append(
                    SemanticChange(path, _plain(current), item.get("before"))
                )
            else:
                _toml_delete(document, path)
                changes.append(SemanticChange(path, _plain(current), None))

        rendered = document.as_string().encode()
        if changes:
            if not manifest["config_existed"] and not rendered.strip() and not retained:
                self.config_path.unlink(missing_ok=True)
            else:
                self._replace(self.config_path, rendered)
        self._finish_removal(manifest, retained)
        return ClientRemovalResult(bool(changes), tuple(changes), tuple(skipped))

    def restore(self) -> None:
        manifest = self._manifest()
        current, _ = _read(self.config_path)
        if _digest(current) != manifest["applied_digest"]:
            raise ClientIntegrationConflict(
                "Codex config changed after mlxctl applied the integration"
            )
        backup = self.backup_path.read_bytes()
        if manifest["config_existed"]:
            self._replace(self.config_path, backup)
        else:
            self.config_path.unlink(missing_ok=True)
        self.manifest_path.unlink(missing_ok=True)
        self.backup_path.unlink(missing_ok=True)

    def test(
        self,
        configuration: ClientConfiguration,
        request: TestRequest[TestResult],
        *,
        profile: str = "coding",
    ) -> TestResult:
        return _test_request(configuration, request, profile)

    def _manifest(self, *, optional: bool = False) -> dict[str, object]:
        manifest = _load_manifest(self.manifest_path, "codex", optional)
        _validate_manifest_paths(manifest, self.config_path, self.backup_path)
        return manifest

    def _finish_removal(
        self, manifest: dict[str, object], retained: list[dict[str, object]]
    ) -> None:
        if retained:
            manifest["fields"] = retained
            _write_private(self.manifest_path, _json_bytes(manifest))
        else:
            self.manifest_path.unlink(missing_ok=True)
            self.backup_path.unlink(missing_ok=True)


class HindsightClientIntegration:
    """Manage a Hindsight profile env file without owning unrelated keys."""

    def __init__(
        self,
        config_path: str | Path,
        manifest_path: str | Path,
        backup_path: str | Path,
        *,
        replace: Replace | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.manifest_path = Path(manifest_path)
        self.backup_path = Path(backup_path)
        self._replace = replace or _atomic_replace

    def preview(self, configuration: ClientConfiguration) -> tuple[SemanticChange, ...]:
        raw, _ = _read(self.config_path)
        env = _EnvDocument(raw.decode())
        return tuple(env.changes(_hindsight_fields(configuration)))

    def apply(self, configuration: ClientConfiguration) -> ClientApplyResult:
        raw, existed = _read(self.config_path)
        env = _EnvDocument(raw.decode())
        desired = _hindsight_fields(configuration)
        prior_manifest = self._manifest(optional=True)
        prior_fields = {
            tuple(item["path"]): item for item in prior_manifest.get("fields", [])
        }
        changes: list[SemanticChange] = []
        owned: list[dict[str, object]] = []
        for path, previous in prior_fields.items():
            if path[0] in desired:
                continue
            present, current, _line = env.lookup(path[0])
            if not present or current != previous.get("after"):
                owned.append(previous)
                continue
            if previous["before_present"]:
                env.restore_line(path[0], str(previous["before_line"]))
                changes.append(SemanticChange(path, current, previous.get("before")))
            else:
                env.delete(path[0])
                changes.append(SemanticChange(path, current, None))

        for key, after in desired.items():
            path = (key,)
            present, current, current_line = env.lookup(key)
            previous = prior_fields.get(path)
            if not present or current != after:
                changes.append(
                    SemanticChange(path, current if present else None, after)
                )
                env.set(key, after)
            if previous is not None:
                before_present = bool(previous["before_present"])
                before = previous.get("before")
                before_line = previous.get("before_line")
            elif not present or current != after:
                before_present, before, before_line = present, current, current_line
            else:
                continue
            owned.append(
                {
                    "path": [key],
                    "before_present": before_present,
                    "before": before,
                    "before_line": before_line,
                    "after": after,
                }
            )

        if not changes:
            return ClientApplyResult(False, (), self.backup_path, self.manifest_path)

        rendered = env.render().encode()
        manifest = {
            "schema_version": 1,
            "integration": "hindsight",
            "config_path": str(self.config_path),
            "config_existed": (
                bool(prior_manifest.get("config_existed"))
                if prior_manifest
                else existed
            ),
            "backup_path": str(self.backup_path),
            "before_digest": (
                str(prior_manifest.get("before_digest"))
                if prior_manifest
                else _digest(raw)
            ),
            "applied_digest": _digest(rendered),
            "fields": owned,
        }
        support = _support_snapshot(self.manifest_path, self.backup_path)
        try:
            if not prior_manifest:
                _write_private(self.backup_path, raw)
            _write_private(self.manifest_path, _json_bytes(manifest))
            self._replace(self.config_path, rendered)
        except Exception:
            _restore_support(self.manifest_path, self.backup_path, support)
            raise
        return ClientApplyResult(
            True, tuple(changes), self.backup_path, self.manifest_path
        )

    def remove(self) -> ClientRemovalResult:
        manifest = self._manifest(optional=True)
        if not manifest:
            return ClientRemovalResult(False, ())
        raw, _ = _read(self.config_path)
        env = _EnvDocument(raw.decode())
        changes: list[SemanticChange] = []
        skipped: list[tuple[str, ...]] = []
        retained = []
        for item in manifest["fields"]:
            path = tuple(item["path"])
            present, current, _line = env.lookup(path[0])
            if not present or current != item.get("after"):
                skipped.append(path)
                retained.append(item)
                continue
            if item["before_present"]:
                env.restore_line(path[0], str(item["before_line"]))
                changes.append(SemanticChange(path, current, item.get("before")))
            else:
                env.delete(path[0])
                changes.append(SemanticChange(path, current, None))
        rendered = env.render().encode()
        if changes:
            if not manifest["config_existed"] and not rendered.strip() and not retained:
                self.config_path.unlink(missing_ok=True)
            else:
                self._replace(self.config_path, rendered)
        if retained:
            manifest["fields"] = retained
            _write_private(self.manifest_path, _json_bytes(manifest))
        else:
            self.manifest_path.unlink(missing_ok=True)
            self.backup_path.unlink(missing_ok=True)
        return ClientRemovalResult(bool(changes), tuple(changes), tuple(skipped))

    def restore(self) -> None:
        manifest = self._manifest()
        current, _ = _read(self.config_path)
        if _digest(current) != manifest["applied_digest"]:
            raise ClientIntegrationConflict(
                "Hindsight config changed after mlxctl applied the integration"
            )
        backup = self.backup_path.read_bytes()
        if manifest["config_existed"]:
            self._replace(self.config_path, backup)
        else:
            self.config_path.unlink(missing_ok=True)
        self.manifest_path.unlink(missing_ok=True)
        self.backup_path.unlink(missing_ok=True)

    def test(
        self,
        configuration: ClientConfiguration,
        request: TestRequest[TestResult],
        *,
        profile: str = "reflect",
    ) -> TestResult:
        return _test_request(configuration, request, profile)

    def _manifest(self, *, optional: bool = False) -> dict[str, object]:
        manifest = _load_manifest(self.manifest_path, "hindsight", optional)
        _validate_manifest_paths(manifest, self.config_path, self.backup_path)
        return manifest


def _codex_fields(
    configuration: ClientConfiguration,
) -> Mapping[tuple[str, ...], object]:
    provider = configuration.codex_provider_id
    fields: dict[tuple[str, ...], object] = {
        ("model",): configuration.service_name,
        ("model_provider",): provider,
        ("model_providers", provider, "name"): "Local mlxctl Gateway",
        ("model_providers", provider, "base_url"): configuration.gateway_endpoint,
        ("model_providers", provider, "wire_api"): "responses",
    }
    if configuration.context_window is not None:
        fields[("model_context_window",)] = configuration.context_window
    for name, sampling in configuration.sampling_profiles.items():
        prefix = ("profiles", name)
        fields[(*prefix, "model")] = configuration.service_name
        fields[(*prefix, "model_provider")] = provider
        if configuration.context_window is not None:
            fields[(*prefix, "model_context_window")] = configuration.context_window
        for key, value in sampling.values().items():
            fields[(*prefix, key)] = value
    return MappingProxyType(fields)


def _hindsight_fields(configuration: ClientConfiguration) -> Mapping[str, str]:
    fields = {
        "HINDSIGHT_API_LLM_PROVIDER": configuration.hindsight_provider,
        "HINDSIGHT_API_LLM_BASE_URL": configuration.gateway_endpoint,
        "HINDSIGHT_API_LLM_MODEL": configuration.service_name,
        "HINDSIGHT_API_LLM_MAX_CONCURRENT": str(configuration.max_concurrent),
    }
    for name, sampling in configuration.sampling_profiles.items():
        suffix = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
        for key, value in sampling.values().items():
            setting = "MAX_TOKENS" if key == "max_tokens" else key.upper()
            fields[f"HINDSIGHT_API_LLM_{setting}_{suffix}"] = str(value)
    return MappingProxyType(fields)


def _test_request(
    configuration: ClientConfiguration,
    request: TestRequest[TestResult],
    profile: str,
) -> TestResult:
    try:
        sampling = configuration.sampling_profiles[profile].values()
    except KeyError as error:
        raise KeyError(f"unknown sampling profile: {profile}") from error
    return request(configuration.gateway_endpoint, configuration.service_name, sampling)


def _toml_changes(document: TOMLDocument, desired: Mapping[tuple[str, ...], object]):
    for path, after in desired.items():
        present, before = _toml_lookup(document, path)
        plain_before = _plain(before) if present else None
        if not present or plain_before != after:
            yield SemanticChange(path, plain_before, after)


def _toml_lookup(document: object, path: tuple[str, ...]) -> tuple[bool, object]:
    current = document
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _toml_set(document: TOMLDocument, path: tuple[str, ...], value: object) -> None:
    current = document
    for key in path[:-1]:
        if key not in current:
            current[key] = tomlkit.table()
        child = current[key]
        if not isinstance(child, Mapping):
            raise ClientIntegrationConflict(
                f"Codex field {'.'.join(path[:-1])} is not a table"
            )
        current = child
    current[path[-1]] = value


def _toml_delete(document: TOMLDocument, path: tuple[str, ...]) -> None:
    parents: list[tuple[object, str]] = []
    current: object = document
    for key in path[:-1]:
        if not isinstance(current, Mapping) or key not in current:
            return
        parents.append((current, key))
        current = current[key]
    if isinstance(current, Mapping):
        del current[path[-1]]
    for parent, key in reversed(parents):
        child = parent[key]  # type: ignore[index]
        if isinstance(child, Mapping) and not child:
            del parent[key]  # type: ignore[index]
        else:
            break


class _EnvDocument:
    _assignment = re.compile(
        r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*?)(?P<newline>\r?\n)?$"
    )

    def __init__(self, text: str) -> None:
        self.lines = text.splitlines(keepends=True)
        self._index: dict[str, int] = {}
        for index, line in enumerate(self.lines):
            match = self._assignment.match(line)
            if not match:
                continue
            key = match.group("key")
            if key in self._index:
                raise ClientIntegrationConflict(f"duplicate Hindsight setting: {key}")
            self._index[key] = index

    def lookup(self, key: str) -> tuple[bool, str | None, str | None]:
        index = self._index.get(key)
        if index is None:
            return False, None, None
        line = self.lines[index]
        match = self._assignment.match(line)
        assert match is not None
        return True, match.group("value"), line

    def changes(self, desired: Mapping[str, str]):
        for key, after in desired.items():
            present, before, _line = self.lookup(key)
            if not present or before != after:
                yield SemanticChange((key,), before if present else None, after)

    def set(self, key: str, value: str) -> None:
        index = self._index.get(key)
        if index is None:
            if self.lines and not self.lines[-1].endswith(("\n", "\r")):
                self.lines[-1] += "\n"
            self._index[key] = len(self.lines)
            self.lines.append(f"{key}={value}\n")
            return
        line = self.lines[index]
        match = self._assignment.match(line)
        assert match is not None
        newline = match.group("newline") or ""
        self.lines[index] = f"{match.group('prefix')}{key}={value}{newline}"

    def restore_line(self, key: str, line: str) -> None:
        index = self._index[key]
        self.lines[index] = line

    def delete(self, key: str) -> None:
        index = self._index.pop(key)
        del self.lines[index]
        self._index = {
            existing: (position - 1 if position > index else position)
            for existing, position in self._index.items()
        }

    def render(self) -> str:
        return "".join(self.lines)


def _load_toml(path: Path) -> TOMLDocument:
    raw, _ = _read(path)
    return _parse_toml(raw)


def _parse_toml(raw: bytes) -> TOMLDocument:
    text = raw.decode()
    return tomlkit.parse(text) if text.strip() else tomlkit.document()


def _load_manifest(path: Path, integration: str, optional: bool) -> dict[str, object]:
    if not path.exists():
        if optional:
            return {}
        raise FileNotFoundError(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != 1
        or raw.get("integration") != integration
        or not isinstance(raw.get("fields"), list)
    ):
        raise ClientIntegrationConflict(f"invalid {integration} ownership manifest")
    return raw


def _validate_manifest_paths(
    manifest: Mapping[str, object], config_path: Path, backup_path: Path
) -> None:
    if not manifest:
        return
    if manifest.get("config_path") != str(config_path) or manifest.get(
        "backup_path"
    ) != str(backup_path):
        raise ClientIntegrationConflict("ownership manifest belongs to other paths")


def _read(path: Path) -> tuple[bytes, bool]:
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


def _atomic_replace(path: Path, payload: bytes) -> None:
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


def _plain(value: object) -> object:
    if hasattr(value, "unwrap"):
        return value.unwrap()  # type: ignore[no-any-return,union-attr]
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value
