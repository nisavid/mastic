"""Precise, reversible Codex and Hindsight Gateway integrations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from ipaddress import ip_address
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, TypeVar
from urllib.parse import urlsplit

import tomlkit
from tomlkit.toml_document import TOMLDocument

from mastic.application.application_targets import SamplingProfile
from mastic.application.config_schema import (
    ApplicationTargetSettings,
    validate_hindsight_profile_name,
)
from mastic.infrastructure.gateway_credential import read_gateway_token


_HINDSIGHT_API_KEY = "HINDSIGHT_API_LLM_API_KEY"
_REDACTED = "<redacted>"


def _ownership_recovery_next_actions(integration: str) -> list[str]:
    return [
        "move invalid or conflicting ownership manifests out of the mastic application-target ownership directory",
        f"mastic application-target inspect {integration}",
    ]


@dataclass(frozen=True, slots=True)
class CodexModelMetadata:
    slug: str
    display_name: str
    description: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", self.slug):
            raise ValueError("Codex model slug must be a route-safe name")
        if not self.display_name or not self.description:
            raise ValueError("Codex model display name and description are required")


@dataclass(frozen=True, slots=True)
class ApplicationTargetConfiguration:
    gateway_endpoint: str
    service_name: str
    context_window: int | None = None
    sampling_profiles: Mapping[str, SamplingProfile] = field(default_factory=dict)
    codex_provider_id: str = "mlx-local"
    hindsight_provider: str = "openai"
    max_concurrent: int = 1
    credential_path: Path | None = None
    codex_model: CodexModelMetadata | None = None
    service_identity: str | None = None

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
        if self.credential_path is not None:
            credential_path = Path(self.credential_path)
            if not credential_path.is_absolute():
                raise ValueError("credential_path must be absolute")
            object.__setattr__(self, "credential_path", credential_path)
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
class ApplicationTargetApplyResult:
    changed: bool
    changes: tuple[SemanticChange, ...]
    backup_path: Path
    manifest_path: Path


@dataclass(frozen=True, slots=True)
class ApplicationTargetRemovalResult:
    changed: bool
    changes: tuple[SemanticChange, ...]
    skipped_paths: tuple[tuple[str, ...], ...] = ()


class ApplicationTargetIntegrationConflict(RuntimeError):
    """A managed field or snapshot changed outside mastic."""


class ApplicationTargetOwnershipRecoveryRequired(RuntimeError):
    """On-disk ownership evidence must be resolved before mutation."""


class OwnershipDiscoveryPolicy(Enum):
    """How ownership discovery handles recognizable invalid manifests."""

    INSPECT_RECOVERY = "inspect-recovery"
    STRICT = "strict"


@dataclass(frozen=True, slots=True)
class ApplicationTargetOwnership:
    integration: str
    profile: str | None
    config_path: Path
    manifest_path: Path
    backup_path: Path


class ApplicationTargetOwnershipDiscovery:
    """Discover and validate application-target ownership manifests once."""

    def __init__(
        self,
        *,
        codex_config_path: str | Path,
        hindsight_profiles_dir: str | Path,
        ownership_dir: str | Path,
    ) -> None:
        self.codex_config_path = Path(codex_config_path).expanduser()
        self.hindsight_profiles_dir = Path(hindsight_profiles_dir).expanduser()
        self.ownership_dir = Path(ownership_dir).expanduser()
        for path, label in (
            (self.codex_config_path, "Codex config path"),
            (self.hindsight_profiles_dir, "Hindsight profiles directory"),
            (self.ownership_dir, "application-target ownership directory"),
        ):
            if not path.is_absolute():
                raise ValueError(f"{label} must be absolute")

    def discover(
        self,
        policy: OwnershipDiscoveryPolicy,
        *,
        integration: str | None = None,
    ) -> tuple[ApplicationTargetOwnership, ...]:
        if integration not in {None, "codex", "hindsight"}:
            raise ValueError(
                f"unsupported Application Configuration Target: {integration}"
            )
        _safe_directory(self.ownership_dir, "application-target ownership directory")
        if not self.ownership_dir.exists():
            return ()
        ownership: list[ApplicationTargetOwnership] = []
        for manifest_path in sorted(self.ownership_dir.iterdir()):
            if integration == "codex" and manifest_path.name != "codex.ownership.json":
                continue
            if integration == "hindsight" and not (
                manifest_path.name.startswith("hindsight-")
                and manifest_path.name.endswith(".ownership.json")
            ):
                continue
            try:
                recognized = self._recognize(manifest_path)
            except ValueError as error:
                raise ApplicationTargetOwnershipRecoveryRequired(
                    "invalid Hindsight ownership manifest name"
                ) from error
            if recognized is None:
                continue
            owned_integration, profile, config_path, backup_path = recognized
            if integration is not None and owned_integration != integration:
                continue
            try:
                manifest = _load_manifest(manifest_path, owned_integration, False)
                _validate_manifest_paths(manifest, config_path, backup_path)
            except (
                ApplicationTargetIntegrationConflict,
                OSError,
                UnicodeError,
                ValueError,
            ) as error:
                if policy is OwnershipDiscoveryPolicy.STRICT:
                    raise ApplicationTargetOwnershipRecoveryRequired(
                        f"invalid {owned_integration} ownership manifest"
                    ) from error
            ownership.append(
                ApplicationTargetOwnership(
                    owned_integration,
                    profile,
                    config_path,
                    manifest_path,
                    backup_path,
                )
            )
        hindsight = [item for item in ownership if item.integration == "hindsight"]
        if len(hindsight) > 1:
            raise ApplicationTargetOwnershipRecoveryRequired(
                "Hindsight profile recovery requires exactly one recognizable ownership manifest"
            )
        return tuple(ownership)

    @staticmethod
    def reconcile_hindsight(
        desired_profile: object,
        ownership: tuple[ApplicationTargetOwnership, ...],
    ) -> ApplicationTargetOwnership | None:
        hindsight = tuple(item for item in ownership if item.integration == "hindsight")
        if not hindsight:
            return None
        profile = (
            validate_hindsight_profile_name(desired_profile)
            if desired_profile is not None
            else None
        )
        if profile is not None and hindsight[0].profile != profile:
            raise ApplicationTargetOwnershipRecoveryRequired(
                "Desired Hindsight profile does not match the owned profile"
            )
        return hindsight[0]

    def _recognize(
        self, manifest_path: Path
    ) -> tuple[str, str | None, Path, Path] | None:
        if manifest_path.name == "codex.ownership.json":
            return (
                "codex",
                None,
                self.codex_config_path,
                self.ownership_dir / "codex.config.backup",
            )
        prefix = "hindsight-"
        suffix = ".ownership.json"
        if not manifest_path.name.startswith(prefix) or not manifest_path.name.endswith(
            suffix
        ):
            return None
        profile = validate_hindsight_profile_name(
            manifest_path.name[len(prefix) : -len(suffix)]
        )
        return (
            "hindsight",
            profile,
            self.hindsight_profiles_dir / f"{profile}.env",
            self.ownership_dir / f"hindsight-{profile}.config.backup",
        )


Replace = Callable[[Path, bytes], None]
TestResult = TypeVar("TestResult")
TestRequest = Callable[[str, str, Mapping[str, object]], TestResult]


class LocalApplicationTargetIntegrationFactory:
    """Select one precise Application Configuration Target from intent and state."""

    def __init__(
        self,
        *,
        codex_config_path: str | Path,
        hindsight_profiles_dir: str | Path,
        ownership_dir: str | Path,
        credential_reader: Callable[[Path], str] = read_gateway_token,
    ) -> None:
        self.codex_config_path = Path(codex_config_path).expanduser()
        self.hindsight_profiles_dir = Path(hindsight_profiles_dir).expanduser()
        self.ownership_dir = Path(ownership_dir).expanduser()
        self._credential_reader = credential_reader
        self._ownership = ApplicationTargetOwnershipDiscovery(
            codex_config_path=self.codex_config_path,
            hindsight_profiles_dir=self.hindsight_profiles_dir,
            ownership_dir=self.ownership_dir,
        )

    def __call__(
        self,
        operation: str,
        name: str,
        parameters: Mapping[str, object],
        settings: ApplicationTargetSettings | None,
    ) -> CodexApplicationTargetIntegration | HindsightApplicationTargetIntegration:
        _safe_directory(self.ownership_dir, "application-target ownership directory")
        if name == "codex":
            _safe_target(self.codex_config_path, "Codex config")
            policy = (
                OwnershipDiscoveryPolicy.INSPECT_RECOVERY
                if operation == "application-target.inspect"
                else OwnershipDiscoveryPolicy.STRICT
            )
            self._ownership.discover(policy, integration="codex")
            return CodexApplicationTargetIntegration(
                self.codex_config_path,
                self.ownership_dir / "codex.ownership.json",
                self.ownership_dir / "codex.config.backup",
                catalog_path=self.ownership_dir / "codex-model-catalog.json",
                catalog_backup_path=self.ownership_dir / "codex-model-catalog.backup",
            )
        if name != "hindsight":
            raise ValueError(f"unsupported Application Configuration Target: {name}")
        _safe_directory(self.hindsight_profiles_dir, "Hindsight profiles directory")
        policy = (
            OwnershipDiscoveryPolicy.INSPECT_RECOVERY
            if operation == "application-target.inspect"
            else OwnershipDiscoveryPolicy.STRICT
        )
        ownership = self._ownership.discover(policy, integration="hindsight")
        desired_profile = (
            parameters.get("profile")
            if operation == "application-target.configure"
            else settings.profile
            if settings is not None
            else None
        )
        owned = self._ownership.reconcile_hindsight(
            desired_profile,
            ownership,
        )
        profile = (
            desired_profile
            if desired_profile is not None
            else owned.profile
            if owned
            else None
        )
        profile_name = validate_hindsight_profile_name(profile)
        config_path = self.hindsight_profiles_dir / f"{profile_name}.env"
        _safe_target(config_path, "Hindsight profile")
        return HindsightApplicationTargetIntegration(
            config_path,
            self.ownership_dir / f"hindsight-{profile_name}.ownership.json",
            self.ownership_dir / f"hindsight-{profile_name}.config.backup",
            credential_reader=self._credential_reader,
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
                "slug": configuration.codex_model.slug,
                "context_window": configuration.context_window,
            }
        support = _support_snapshot(self.manifest_path, self.backup_path)
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
            _restore_support(self.manifest_path, self.backup_path, support)
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
        if configuration.codex_model is not None:
            fields[("model_catalog_json",)] = str(self.catalog_path)
        return MappingProxyType(fields)

    def _render_catalog(
        self, configuration: ApplicationTargetConfiguration
    ) -> bytes | None:
        metadata = configuration.codex_model
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


def _codex_fields(
    configuration: ApplicationTargetConfiguration,
) -> Mapping[tuple[str, ...], object]:
    _validate_application_target_profiles(configuration, "codex")
    provider = configuration.codex_provider_id
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


def _profile_endpoint(
    configuration: ApplicationTargetConfiguration, application_target: str, profile: str
) -> str:
    if profile not in configuration.sampling_profiles:
        raise ValueError(
            f"required {application_target} Application Configuration Target profile is missing: {profile}"
        )
    root = configuration.gateway_endpoint.removesuffix("/").removesuffix("/v1")
    return f"{root}/application-targets/{application_target}/profiles/{profile}/v1"


def _validate_application_target_profiles(
    configuration: ApplicationTargetConfiguration, application_target: str
) -> None:
    required = (
        {"coding"}
        if application_target == "codex"
        else {"verification", "retain", "reflect", "consolidation"}
    )
    missing = required - set(configuration.sampling_profiles)
    if missing:
        raise ValueError(
            f"{application_target} Application Configuration Target requires sampling profiles: {', '.join(sorted(required))}"
        )
    if application_target == "codex":
        coding = configuration.sampling_profiles["coding"]
        if (
            coding.min_p not in {None, 0.0}
            or coding.presence_penalty not in {None, 0.0}
            or coding.repetition_penalty not in {None, 1.0}
            or coding.max_tokens is not None
        ):
            raise ValueError(
                "Codex coding profile contains values OptiQ Responses cannot represent"
            )


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


def _test_request(
    configuration: ApplicationTargetConfiguration,
    request: TestRequest[TestResult],
    profile: str,
    *,
    target: str,
) -> TestResult:
    if profile not in configuration.sampling_profiles:
        error = KeyError(profile)
        raise KeyError(f"unknown sampling profile: {profile}") from error
    return request(
        _profile_endpoint(configuration, target, profile),
        configuration.service_name,
        {},
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


def _load_toml(path: Path) -> TOMLDocument:
    raw, _ = _read(path)
    return _parse_toml(raw)


def _parse_toml(raw: bytes) -> TOMLDocument:
    text = raw.decode()
    return tomlkit.parse(text) if text.strip() else tomlkit.document()


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
