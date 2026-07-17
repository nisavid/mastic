"""Discovery and validation of application-target ownership manifests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from mastic.application.config_schema import validate_hindsight_profile_name
from mastic.infrastructure.application_target_contracts import (
    ApplicationTargetIntegrationConflict,
    ApplicationTargetOwnershipRecoveryRequired,
)
from mastic.infrastructure.application_target_persistence import (
    _load_manifest,
    _safe_directory,
    _validate_manifest_paths,
)


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
            try:
                recognized = self._recognize(manifest_path, integration)
            except ValueError as error:
                raise ApplicationTargetOwnershipRecoveryRequired(
                    "invalid Hindsight ownership manifest name"
                ) from error
            if recognized is None:
                continue
            owned_integration, profile, config_path, backup_path = recognized
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
        self, manifest_path: Path, integration: str | None
    ) -> tuple[str, str | None, Path, Path] | None:
        if manifest_path.name == "codex.ownership.json":
            if integration == "hindsight":
                return None
            return (
                "codex",
                None,
                self.codex_config_path,
                self.ownership_dir / "codex.config.backup",
            )
        if integration == "codex":
            return None
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
