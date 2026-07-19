"""Precise, reversible Codex and Hindsight Gateway integrations.

This module is the stable import facade and local adapter factory. Target-specific
implementation, ownership discovery, and persistence live behind internal seams.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from mastic.application.application_targets import SamplingProfile
from mastic.application.config_schema import (
    ApplicationTargetSettings,
    validate_hindsight_profile_name,
)
from mastic.infrastructure.application_target_contracts import (
    ApplicationTargetApplyResult,
    ApplicationTargetConfiguration,
    ApplicationTargetIntegrationConflict,
    ApplicationTargetOwnershipRecoveryRequired,
    ApplicationTargetRemovalResult,
    CodexModelMetadata,
    CodexTargetOptions as CodexTargetOptions,
    HindsightTargetOptions as HindsightTargetOptions,
    SemanticChange,
)
from mastic.infrastructure.application_target_ownership import (
    ApplicationTargetOwnership,
    ApplicationTargetOwnershipDiscovery,
    OwnershipDiscoveryPolicy,
)
from mastic.infrastructure.application_target_persistence import (
    _safe_directory,
    _safe_target,
)
from mastic.infrastructure.codex_application_target import (
    CodexApplicationTargetIntegration,
)
from mastic.infrastructure.gateway_credential import read_gateway_token
from mastic.infrastructure.hindsight_application_target import (
    HindsightApplicationTargetIntegration,
)


class LocalApplicationTargetIntegrationFactory:
    """Select one precise Application Configuration Target from intent and state."""

    def __init__(
        self,
        *,
        codex_config_path: str | Path,
        hindsight_profiles_dir: str | Path,
        ownership_dir: str | Path,
        credential_reader: Callable[[Path], str] = read_gateway_token,
        resolve_executable: Callable[[str], Path] | None = None,
    ) -> None:
        self.codex_config_path = Path(codex_config_path).expanduser()
        self.hindsight_profiles_dir = Path(hindsight_profiles_dir).expanduser()
        self.ownership_dir = Path(ownership_dir).expanduser()
        self._credential_reader = credential_reader
        self._resolve_executable = resolve_executable
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
                resolve_executable=self._resolve_executable,
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
        desired_profile = settings.profile if settings is not None else None
        if operation == "application-target.configure":
            desired_profile = parameters.get("profile") or desired_profile
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


__all__ = [
    "ApplicationTargetApplyResult",
    "ApplicationTargetConfiguration",
    "ApplicationTargetIntegrationConflict",
    "ApplicationTargetOwnership",
    "ApplicationTargetOwnershipDiscovery",
    "ApplicationTargetOwnershipRecoveryRequired",
    "ApplicationTargetRemovalResult",
    "CodexApplicationTargetIntegration",
    "CodexModelMetadata",
    "HindsightApplicationTargetIntegration",
    "LocalApplicationTargetIntegrationFactory",
    "OwnershipDiscoveryPolicy",
    "SamplingProfile",
    "SemanticChange",
]
