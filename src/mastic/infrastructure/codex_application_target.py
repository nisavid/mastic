"""Reversible Codex configuration and model-catalog adapter."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

import tomlkit
from tomlkit.toml_document import TOMLDocument

from mastic.infrastructure.application_target_contracts import (
    ApplicationTargetApplyResult,
    ApplicationTargetConfiguration,
    ApplicationTargetIntegrationConflict,
    ApplicationTargetRemovalResult,
    CodexTargetOptions,
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
    _plain,
    _read,
    _restore_files,
    _snapshot_files,
    _validate_manifest_paths,
    _write_private,
    _load_manifest,
)


class CodexApplicationTargetIntegration:
    """Manage only the Codex TOML fields recorded in an ownership manifest."""

    def __init__(
        self,
        config_path: str | Path,
        manifest_path: str | Path,
        backup_path: str | Path,
        *,
        replace: Replace | None = None,
        catalog_path: str | Path | None = None,
        catalog_backup_path: str | Path | None = None,
        bundled_catalog: Callable[[], Mapping[str, object]] | None = None,
        catalog_validator: Callable[[Path], None] | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.manifest_path = Path(manifest_path)
        self.backup_path = Path(backup_path)
        self._replace = replace or _atomic_replace
        self.catalog_path = Path(
            catalog_path or self.manifest_path.with_name("codex-model-catalog.json")
        )
        self.catalog_backup_path = Path(
            catalog_backup_path
            or self.manifest_path.with_name("codex-model-catalog.backup")
        )
        self._bundled_catalog = bundled_catalog or _default_bundled_codex_catalog
        self._catalog_validator = catalog_validator or _validate_codex_catalog

    def preview(
        self, configuration: ApplicationTargetConfiguration
    ) -> tuple[SemanticChange, ...]:
        document = _load_toml(self.config_path)
        return tuple(_toml_changes(document, self._desired(configuration)))

    def apply(
        self, configuration: ApplicationTargetConfiguration, *, takeover: bool = False
    ) -> ApplicationTargetApplyResult:
        raw, existed = _read(self.config_path)
        document = _parse_toml(raw)
        desired = self._desired(configuration)
        catalog_rendered = self._render_catalog(configuration)
        catalog_before, catalog_existed = _read(self.catalog_path)
        catalog_changed = (
            catalog_rendered is not None and catalog_before != catalog_rendered
        )
        prior_manifest = self._manifest(optional=True)
        catalog_ownership_new = catalog_rendered is not None and not isinstance(
            prior_manifest.get("catalog"), dict
        )
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
            elif path == ("model_catalog_json",) and catalog_ownership_new:
                before_present, before = True, current
            elif not takeover:
                continue
            else:
                before_present, before = False, None
            owned.append(
                {
                    "path": list(path),
                    "before_present": before_present,
                    "before": _plain(before),
                    "after": _plain(after),
                }
            )

        ownership_changed = bool(owned) and not prior_manifest
        if (
            not changes
            and not ownership_changed
            and not catalog_changed
            and not catalog_ownership_new
        ):
            return ApplicationTargetApplyResult(
                False, (), self.backup_path, self.manifest_path
            )

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
        if catalog_rendered is not None:
            previous_catalog = prior_manifest.get("catalog", {})
            manifest["catalog"] = {
                "path": str(self.catalog_path),
                "backup_path": str(self.catalog_backup_path),
                "existed": (
                    bool(previous_catalog.get("existed"))
                    if previous_catalog
                    else catalog_existed
                ),
                "before_digest": (
                    str(previous_catalog.get("before_digest"))
                    if previous_catalog
                    else _digest(catalog_before)
                ),
                "applied_digest": _digest(catalog_rendered),
                "slug": _codex_target(configuration).model.slug,
                "context_window": configuration.context_window,
            }
        support = _snapshot_files(self.manifest_path, self.backup_path)
        try:
            if not prior_manifest:
                _write_private(self.backup_path, raw)
            if catalog_ownership_new:
                _write_private(self.catalog_backup_path, catalog_before)
            if catalog_changed and catalog_rendered is not None:
                self._replace(self.catalog_path, catalog_rendered)
                self._catalog_validator(self.catalog_path)
            if changes:
                self._replace(self.config_path, rendered)
            _write_private(self.manifest_path, _json_bytes(manifest))
        except Exception:
            if changes:
                if existed:
                    _atomic_replace(self.config_path, raw)
                else:
                    self.config_path.unlink(missing_ok=True)
            if catalog_changed:
                if catalog_existed:
                    _atomic_replace(self.catalog_path, catalog_before)
                else:
                    self.catalog_path.unlink(missing_ok=True)
            _restore_files(support)
            if catalog_ownership_new:
                self.catalog_backup_path.unlink(missing_ok=True)
            raise
        return ApplicationTargetApplyResult(
            bool(changes) or ownership_changed or catalog_changed,
            tuple(changes),
            self.backup_path,
            self.manifest_path,
        )

    def rollback_point(self) -> Callable[[], None]:
        snapshot = _snapshot_files(
            self.config_path,
            self.catalog_path,
            self.manifest_path,
            self.backup_path,
            self.catalog_backup_path,
        )
        return lambda: _restore_files(snapshot)

    def remove(self) -> ApplicationTargetRemovalResult:
        manifest = self._manifest(optional=True)
        if not manifest:
            return ApplicationTargetRemovalResult(False, ())
        snapshot = _snapshot_files(
            self.config_path,
            self.catalog_path,
            self.manifest_path,
            self.backup_path,
            self.catalog_backup_path,
        )
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
            catalog_changed, catalog_skipped = self._remove_catalog(manifest)
            if catalog_skipped:
                skipped.append(("model_catalog_json",))
            self._finish_removal(manifest, retained, keep_catalog=catalog_skipped)
        except Exception:
            _restore_files(snapshot)
            raise
        return ApplicationTargetRemovalResult(
            bool(changes) or catalog_changed, tuple(changes), tuple(skipped)
        )

    def restore(self) -> None:
        manifest = self._manifest()
        snapshot = _snapshot_files(
            self.config_path,
            self.catalog_path,
            self.manifest_path,
            self.backup_path,
            self.catalog_backup_path,
        )
        current, _ = _read(self.config_path)
        if _digest(current) != manifest["applied_digest"]:
            raise ApplicationTargetIntegrationConflict(
                "Codex config changed after mastic applied the integration"
            )
        backup, _ = _read(self.backup_path)
        catalog = manifest.get("catalog")
        if isinstance(catalog, dict):
            current_catalog, _ = _read(self.catalog_path)
            if _digest(current_catalog) != catalog.get("applied_digest"):
                raise ApplicationTargetIntegrationConflict(
                    "Codex model catalog changed after mastic applied the integration"
                )
        try:
            if manifest["config_existed"]:
                self._replace(self.config_path, backup)
            else:
                self.config_path.unlink(missing_ok=True)
            if isinstance(catalog, dict):
                catalog_backup, _ = _read(self.catalog_backup_path)
                if catalog.get("existed"):
                    self._replace(self.catalog_path, catalog_backup)
                else:
                    self.catalog_path.unlink(missing_ok=True)
            self.manifest_path.unlink(missing_ok=True)
            self.backup_path.unlink(missing_ok=True)
            self.catalog_backup_path.unlink(missing_ok=True)
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
            return {
                "state": "malformed",
                "detail": "Codex ownership manifest is malformed.",
                "catalog_path": str(self.catalog_path),
                "ownership_manifest_path": str(self.manifest_path),
                "next_actions": _ownership_recovery_next_actions("codex"),
            }
        if not manifest:
            return {
                "state": "unmanaged",
                "next_actions": ["mastic application-target configure codex"],
            }
        catalog = manifest.get("catalog")
        if not isinstance(catalog, dict):
            return {
                "state": "missing",
                "detail": "Codex ownership predates the required custom model catalog.",
                "next_actions": ["mastic application-target configure codex"],
            }
        config_document = _load_toml(self.config_path)
        for item in manifest.get("fields", []):
            path = tuple(item["path"])
            present, current = _toml_lookup(config_document, path)
            if not present or _plain(current) != item.get("after"):
                return {
                    "state": "drifted",
                    "detail": f"Codex setting {'.'.join(path)} differs from mastic ownership.",
                    "catalog_path": str(self.catalog_path),
                    "next_actions": ["mastic application-target configure codex"],
                }
        raw, exists = _read(self.catalog_path)
        state = "healthy"
        detail = "Codex custom model catalog matches mastic ownership."
        if not exists:
            state, detail = "missing", "Codex custom model catalog is missing."
        elif _digest(raw) != catalog.get("applied_digest"):
            state, detail = (
                "drifted",
                "Codex custom model catalog differs from the applied catalog.",
            )
        else:
            try:
                document = json.loads(raw)
                models = document.get("models", [])
                model = next(
                    item for item in models if item.get("slug") == catalog.get("slug")
                )
                if model.get("context_window") != catalog.get("context_window"):
                    state, detail = (
                        "incompatible",
                        "Codex model context does not match the service capacity.",
                    )
                else:
                    self._catalog_validator(self.catalog_path)
            except (ValueError, TypeError, StopIteration, AttributeError):
                state, detail = "malformed", "Codex custom model catalog is malformed."
            except (
                ApplicationTargetIntegrationConflict,
                OSError,
                subprocess.SubprocessError,
            ) as error:
                state, detail = "incompatible", str(error)
        return {
            "state": state,
            "detail": detail,
            "catalog_path": str(self.catalog_path),
            "next_actions": []
            if state == "healthy"
            else ["mastic application-target configure codex"],
        }

    def test(
        self,
        configuration: ApplicationTargetConfiguration,
        request: TestRequest[TestResult],
        *,
        profile: str = "coding",
    ) -> TestResult:
        return _test_request(configuration, request, profile, target="codex")

    def _manifest(self, *, optional: bool = False) -> dict[str, object]:
        manifest = _load_manifest(self.manifest_path, "codex", optional)
        _validate_manifest_paths(manifest, self.config_path, self.backup_path)
        return manifest

    def _finish_removal(
        self,
        manifest: dict[str, object],
        retained: list[dict[str, object]],
        *,
        keep_catalog: bool = False,
    ) -> None:
        if retained or keep_catalog:
            manifest["fields"] = retained
            _write_private(self.manifest_path, _json_bytes(manifest))
        else:
            self.manifest_path.unlink(missing_ok=True)
            self.backup_path.unlink(missing_ok=True)
            self.catalog_backup_path.unlink(missing_ok=True)

    def _desired(
        self, configuration: ApplicationTargetConfiguration
    ) -> Mapping[tuple[str, ...], object]:
        fields = dict(_codex_fields(configuration))
        target = _codex_target(configuration)
        if target.model is not None:
            fields[("model_catalog_json",)] = str(self.catalog_path)
        return MappingProxyType(fields)

    def _render_catalog(
        self, configuration: ApplicationTargetConfiguration
    ) -> bytes | None:
        metadata = _codex_target(configuration).model
        if metadata is None:
            return None
        if configuration.context_window is None:
            raise ValueError("Codex custom model metadata requires a context window")
        bundled = self._bundled_catalog()
        models = bundled.get("models")
        if not isinstance(models, list):
            raise ApplicationTargetIntegrationConflict(
                "Codex bundled model catalog is malformed"
            )
        template = next(
            (
                item
                for item in models
                if isinstance(item, dict)
                and item.get("slug") == "gpt-5.4"
                and item.get("base_instructions")
            ),
            next(
                (
                    item
                    for item in models
                    if isinstance(item, dict) and item.get("base_instructions")
                ),
                None,
            ),
        )
        if template is None:
            raise ApplicationTargetIntegrationConflict(
                "Codex bundled model catalog has no instruction-bearing model"
            )
        model = dict(template)
        model.update(
            {
                "slug": metadata.slug,
                "display_name": metadata.display_name,
                "description": metadata.description,
                "context_window": configuration.context_window,
                "max_context_window": configuration.context_window,
                "default_reasoning_level": None,
                "supported_reasoning_levels": [],
                "supports_reasoning_summaries": False,
                "supports_parallel_tool_calls": False,
                "supports_image_detail_original": False,
                "supports_search_tool": False,
                "use_responses_lite": False,
                "input_modalities": ["text"],
                "additional_speed_tiers": [],
                "service_tiers": [],
                "experimental_supported_tools": [],
            }
        )
        for key in ("support_verbosity", "default_verbosity"):
            if key in model:
                model[key] = False if key == "support_verbosity" else None
        model.pop("apply_patch_tool_type", None)
        model.pop("web_search_tool_type", None)
        return _json_bytes({"models": [model]})

    def _remove_catalog(self, manifest: Mapping[str, object]) -> tuple[bool, bool]:
        catalog = manifest.get("catalog")
        if not isinstance(catalog, dict):
            return False, False
        current, exists = _read(self.catalog_path)
        if exists and _digest(current) != catalog.get("applied_digest"):
            return False, True
        backup, _ = _read(self.catalog_backup_path)
        if catalog.get("existed"):
            self._replace(self.catalog_path, backup)
        else:
            self.catalog_path.unlink(missing_ok=True)
        return True, False


def _codex_fields(
    configuration: ApplicationTargetConfiguration,
) -> Mapping[tuple[str, ...], object]:
    _validate_application_target_profiles(configuration, "codex")
    provider = _codex_target(configuration).provider_id
    fields: dict[tuple[str, ...], object] = {
        ("model",): configuration.service_name,
        ("model_provider",): provider,
        ("oss_provider",): provider,
        ("model_providers", provider, "name"): "Local mastic Gateway",
        ("model_providers", provider, "base_url"): _profile_endpoint(
            configuration, "codex", "coding"
        ),
        ("model_providers", provider, "wire_api"): "responses",
    }
    if configuration.context_window is not None:
        fields[("model_context_window",)] = configuration.context_window
    if configuration.credential_path is not None:
        auth = ("model_providers", provider, "auth")
        fields[(*auth, "command")] = "/bin/cat"
        fields[(*auth, "args")] = [str(configuration.credential_path)]
        fields[(*auth, "refresh_interval_ms")] = 0
    return MappingProxyType(fields)


def _codex_target(configuration: ApplicationTargetConfiguration) -> CodexTargetOptions:
    if not isinstance(configuration.target, CodexTargetOptions):
        raise ValueError("Codex configuration requires Codex target options")
    return configuration.target


def _default_bundled_codex_catalog() -> Mapping[str, object]:
    try:
        completed = subprocess.run(
            ("codex", "debug", "models", "--bundled"),
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        value = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise ApplicationTargetIntegrationConflict(
            "Codex bundled model catalog is unavailable; install or repair Codex and retry"
        ) from error
    if not isinstance(value, dict):
        raise ApplicationTargetIntegrationConflict(
            "Codex bundled model catalog is malformed"
        )
    return value


def _validate_codex_catalog(path: Path) -> None:
    try:
        source = json.loads(path.read_text(encoding="utf-8"))
        expected = {
            str(item["slug"]): int(item["context_window"]) for item in source["models"]
        }
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ApplicationTargetIntegrationConflict(
            "Codex custom model catalog is malformed"
        ) from error
    with tempfile.TemporaryDirectory(prefix="mastic-codex-validate-") as directory:
        home = Path(directory)
        document = tomlkit.document()
        document["model_catalog_json"] = str(path.resolve())
        _write_private(home / "config.toml", document.as_string().encode())
        try:
            completed = subprocess.run(
                ("codex", "debug", "models"),
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
                env={**os.environ, "CODEX_HOME": str(home)},
            )
            resolved = json.loads(completed.stdout)
            actual = {
                str(item["slug"]): int(item["context_window"])
                for item in resolved["models"]
            }
        except (
            OSError,
            subprocess.SubprocessError,
            ValueError,
            KeyError,
            TypeError,
        ) as error:
            detail = (
                str(error.stderr).strip()
                if isinstance(error, subprocess.CalledProcessError) and error.stderr
                else str(error)
            )
            raise ApplicationTargetIntegrationConflict(
                f"installed Codex rejected the custom model catalog: {detail}"
            ) from error
        if actual != expected or "fallback metadata" in completed.stderr.lower():
            raise ApplicationTargetIntegrationConflict(
                "installed Codex did not resolve the custom model metadata exactly"
            )


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
            raise ApplicationTargetIntegrationConflict(
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


def _load_toml(path: Path) -> TOMLDocument:
    raw, _ = _read(path)
    return _parse_toml(raw)


def _parse_toml(raw: bytes) -> TOMLDocument:
    text = raw.decode()
    return tomlkit.parse(text) if text.strip() else tomlkit.document()
