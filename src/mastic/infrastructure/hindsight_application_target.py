"""Reversible Hindsight environment-profile adapter."""

from __future__ import annotations

import re
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from mastic.infrastructure.application_target_contracts import (
    ApplicationTargetApplyResult,
    ApplicationTargetConfiguration,
    ApplicationTargetIntegrationConflict,
    ApplicationTargetRemovalResult,
    Replace,
    SemanticChange,
    TestRequest,
    TestResult,
    _ownership_recovery_next_actions,
    _profile_endpoint,
    _test_request,
    _validate_application_target_profiles,
)
from mastic.infrastructure.application_target_persistence import (
    _atomic_replace,
    _digest,
    _json_bytes,
    _load_manifest,
    _read,
    _restore_files,
    _snapshot_files,
    _validate_manifest_paths,
    _write_private,
)
from mastic.infrastructure.gateway_credential import read_gateway_token


_HINDSIGHT_API_KEY = "HINDSIGHT_API_LLM_API_KEY"
_REDACTED = "<redacted>"


class HindsightApplicationTargetIntegration:
    """Manage a Hindsight profile env file without owning unrelated keys."""

    def __init__(
        self,
        config_path: str | Path,
        manifest_path: str | Path,
        backup_path: str | Path,
        *,
        replace: Replace | None = None,
        credential_reader: Callable[[Path], str] = read_gateway_token,
    ) -> None:
        self.config_path = Path(config_path)
        self.manifest_path = Path(manifest_path)
        self.backup_path = Path(backup_path)
        self._replace = replace or _atomic_replace
        self._credential_reader = credential_reader

    def preview(
        self, configuration: ApplicationTargetConfiguration
    ) -> tuple[SemanticChange, ...]:
        raw, _ = _read(self.config_path)
        env = _EnvDocument(raw.decode())
        desired = self._desired(configuration)
        return tuple(_redact_change(change) for change in env.changes(desired))

    def apply(
        self, configuration: ApplicationTargetConfiguration, *, takeover: bool = False
    ) -> ApplicationTargetApplyResult:
        raw, existed = _read(self.config_path)
        env = _EnvDocument(raw.decode())
        desired = self._desired(configuration)
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
            matches = (
                _secret_matches(current, previous)
                if path[0] == _HINDSIGHT_API_KEY and present
                else current == previous.get("after")
            )
            if not present or not matches:
                owned.append(previous)
                continue
            if previous["before_present"]:
                before, before_line = _backup_env_value(self.backup_path, path[0])
                env.restore_line(path[0], before_line)
                changes.append(_redact_change(SemanticChange(path, current, before)))
            else:
                env.delete(path[0])
                changes.append(_redact_change(SemanticChange(path, current, None)))

        for key, after in desired.items():
            path = (key,)
            present, current, current_line = env.lookup(key)
            previous = prior_fields.get(path)
            if not present or current != after:
                changes.append(
                    _redact_change(
                        SemanticChange(path, current if present else None, after)
                    )
                )
                env.set(key, after)
            if previous is not None:
                before_present = bool(previous["before_present"])
                before = previous.get("before")
                before_line = previous.get("before_line")
            elif not present or current != after:
                before_present, before, before_line = present, current, current_line
            elif not takeover:
                continue
            else:
                before_present, before, before_line = False, None, None
            if key == _HINDSIGHT_API_KEY:
                owned.append(
                    {
                        "path": [key],
                        "before_present": before_present,
                        "after_digest": _digest(after.encode()),
                    }
                )
            else:
                owned.append(
                    {
                        "path": [key],
                        "before_present": before_present,
                        "before": before,
                        "before_line": before_line,
                        "after": after,
                    }
                )

        ownership_changed = bool(owned) and not prior_manifest
        if not changes and not ownership_changed:
            return ApplicationTargetApplyResult(
                False, (), self.backup_path, self.manifest_path
            )

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
        snapshot = _snapshot_files(
            self.config_path,
            self.manifest_path,
            self.backup_path,
        )
        try:
            if not prior_manifest:
                _write_private(self.backup_path, raw)
            _write_private(self.manifest_path, _json_bytes(manifest))
            if changes:
                self._replace(self.config_path, rendered)
        except Exception:
            _restore_files(snapshot)
            raise
        return ApplicationTargetApplyResult(
            bool(changes) or ownership_changed,
            tuple(changes),
            self.backup_path,
            self.manifest_path,
        )

    def rollback_point(self) -> Callable[[], None]:
        snapshot = _snapshot_files(
            self.config_path,
            self.manifest_path,
            self.backup_path,
        )
        return lambda: _restore_files(snapshot)

    def remove(self) -> ApplicationTargetRemovalResult:
        manifest = self._manifest(optional=True)
        if not manifest:
            return ApplicationTargetRemovalResult(False, ())
        snapshot = _snapshot_files(
            self.config_path,
            self.manifest_path,
            self.backup_path,
        )
        raw, _ = _read(self.config_path)
        env = _EnvDocument(raw.decode())
        changes: list[SemanticChange] = []
        skipped: list[tuple[str, ...]] = []
        retained = []
        for item in manifest["fields"]:
            path = tuple(item["path"])
            present, current, _line = env.lookup(path[0])
            matches = (
                _secret_matches(current, item)
                if path[0] == _HINDSIGHT_API_KEY and present
                else current == item.get("after")
            )
            if not present or not matches:
                skipped.append(path)
                retained.append(item)
                continue
            if item["before_present"]:
                before, before_line = _backup_env_value(self.backup_path, path[0])
                env.restore_line(path[0], before_line)
                changes.append(_redact_change(SemanticChange(path, current, before)))
            else:
                env.delete(path[0])
                changes.append(_redact_change(SemanticChange(path, current, None)))
        rendered = env.render().encode()
        try:
            if changes:
                if (
                    not manifest["config_existed"]
                    and not rendered.strip()
                    and not retained
                ):
                    self.config_path.unlink(missing_ok=True)
                else:
                    self._replace(self.config_path, rendered)
            if retained:
                manifest["fields"] = retained
                _write_private(self.manifest_path, _json_bytes(manifest))
            else:
                self.manifest_path.unlink(missing_ok=True)
                self.backup_path.unlink(missing_ok=True)
        except Exception:
            _restore_files(snapshot)
            raise
        return ApplicationTargetRemovalResult(
            bool(changes), tuple(changes), tuple(skipped)
        )

    def restore(self) -> None:
        manifest = self._manifest()
        snapshot = _snapshot_files(
            self.config_path,
            self.manifest_path,
            self.backup_path,
        )
        current, _ = _read(self.config_path)
        if _digest(current) != manifest["applied_digest"]:
            raise ApplicationTargetIntegrationConflict(
                "Hindsight config changed after mastic applied the integration"
            )
        backup, _ = _read(self.backup_path)
        try:
            if manifest["config_existed"]:
                self._replace(self.config_path, backup)
            else:
                self.config_path.unlink(missing_ok=True)
            self.manifest_path.unlink(missing_ok=True)
            self.backup_path.unlink(missing_ok=True)
        except Exception:
            _restore_files(snapshot)
            raise

    def inspect(self) -> Mapping[str, object]:
        try:
            manifest = self._manifest(optional=True)
        except (
            ApplicationTargetIntegrationConflict,
            OSError,
            UnicodeError,
            ValueError,
        ):
            return self._inspection_report(
                "malformed", "Hindsight ownership manifest is malformed."
            )
        if not manifest:
            return self._inspection_report(
                "unmanaged", "Hindsight profile has no mastic ownership manifest."
            )
        try:
            backup, backup_exists = _read(self.backup_path)
        except (OSError, ValueError):
            return self._inspection_report(
                "malformed", "Hindsight ownership support path is invalid."
            )
        if not backup_exists:
            return self._inspection_report(
                "missing", "Hindsight ownership backup is missing."
            )
        if _digest(backup) != manifest.get("before_digest"):
            return self._inspection_report(
                "drifted", "Hindsight ownership backup differs from its manifest."
            )
        try:
            raw, config_exists = _read(self.config_path)
            if not config_exists:
                return self._inspection_report(
                    "missing", "Hindsight owned profile is missing."
                )
            env = _EnvDocument(raw.decode())
            for item in manifest["fields"]:
                path = tuple(item["path"])
                present, current, _line = env.lookup(path[0])
                matches = (
                    _secret_matches(current, item)
                    if path[0] == _HINDSIGHT_API_KEY and present
                    else current == item.get("after")
                )
                if not present or not matches:
                    detail = (
                        "Hindsight credential differs from mastic ownership."
                        if path[0] == _HINDSIGHT_API_KEY
                        else f"Hindsight setting {path[0]} differs from mastic ownership."
                    )
                    return self._inspection_report("drifted", detail)
        except (
            ApplicationTargetIntegrationConflict,
            IndexError,
            KeyError,
            OSError,
            TypeError,
            UnicodeError,
            ValueError,
        ):
            return self._inspection_report(
                "malformed",
                "Hindsight owned profile or ownership fields are malformed.",
            )
        return self._inspection_report(
            "healthy", "Hindsight profile matches mastic ownership."
        )

    def _inspection_report(self, state: str, detail: str) -> Mapping[str, object]:
        return {
            "state": state,
            "detail": detail,
            "config_path": str(self.config_path),
            **(
                {"ownership_manifest_path": str(self.manifest_path)}
                if state == "malformed"
                else {}
            ),
            "next_actions": (
                []
                if state == "healthy"
                else _ownership_recovery_next_actions("hindsight")
                if state == "malformed"
                else ["mastic application-target configure hindsight --help"]
            ),
        }

    def test(
        self,
        configuration: ApplicationTargetConfiguration,
        request: TestRequest[TestResult],
        *,
        profile: str = "reflect",
    ) -> TestResult:
        return _test_request(configuration, request, profile, target="hindsight")

    def _manifest(self, *, optional: bool = False) -> dict[str, object]:
        manifest = _load_manifest(self.manifest_path, "hindsight", optional)
        _validate_manifest_paths(manifest, self.config_path, self.backup_path)
        return manifest

    def _desired(
        self, configuration: ApplicationTargetConfiguration
    ) -> Mapping[str, str]:
        token = (
            self._credential_reader(configuration.credential_path)
            if configuration.credential_path is not None
            else None
        )
        return _hindsight_fields(configuration, token=token)


def _hindsight_fields(
    configuration: ApplicationTargetConfiguration, *, token: str | None = None
) -> Mapping[str, str]:
    _validate_application_target_profiles(configuration, "hindsight")
    fields = {
        "HINDSIGHT_API_LLM_PROVIDER": configuration.hindsight_provider,
        "HINDSIGHT_API_LLM_BASE_URL": _profile_endpoint(
            configuration, "hindsight", "verification"
        ),
        "HINDSIGHT_API_LLM_MODEL": configuration.service_name,
        "HINDSIGHT_API_LLM_MAX_CONCURRENT": str(configuration.max_concurrent),
    }
    if token is not None:
        fields[_HINDSIGHT_API_KEY] = token
    operation_prefixes = {
        "retain": "HINDSIGHT_API_RETAIN_LLM_BASE_URL",
        "reflect": "HINDSIGHT_API_REFLECT_LLM_BASE_URL",
        "consolidation": "HINDSIGHT_API_CONSOLIDATION_LLM_BASE_URL",
    }
    for name, sampling in configuration.sampling_profiles.items():
        suffix = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
        if name in operation_prefixes:
            fields[operation_prefixes[name]] = _profile_endpoint(
                configuration, "hindsight", name
            )
        if sampling.temperature is not None:
            fields[f"HINDSIGHT_API_LLM_TEMPERATURE_{suffix}"] = str(
                sampling.temperature
            )
    return MappingProxyType(fields)


def _secret_matches(current: str | None, item: Mapping[str, object]) -> bool:
    return current is not None and _digest(current.encode()) == item.get("after_digest")


def _redact_change(change: SemanticChange) -> SemanticChange:
    if change.path != (_HINDSIGHT_API_KEY,):
        return change
    return SemanticChange(
        change.path,
        _REDACTED if change.before is not None else None,
        _REDACTED if change.after is not None else None,
    )


def _backup_env_value(path: Path, key: str) -> tuple[str, str]:
    raw, existed = _read(path)
    if not existed:
        raise ApplicationTargetIntegrationConflict(
            "Application Configuration Target backup is missing"
        )
    present, value, line = _EnvDocument(raw.decode()).lookup(key)
    if not present or value is None or line is None:
        raise ApplicationTargetIntegrationConflict(
            f"Application Configuration Target backup lacks {key}"
        )
    return value, line


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
                raise ApplicationTargetIntegrationConflict(
                    f"duplicate Hindsight setting: {key}"
                )
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
