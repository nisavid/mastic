"""Shared contracts for local Application Configuration Target adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, TypeVar
from urllib.parse import urlsplit

from mastic.application.application_targets import (
    SamplingProfile,
    validate_application_target_sampling_profiles,
)


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
class CodexTargetOptions:
    provider_id: str = "mlx-local"
    model: CodexModelMetadata | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", self.provider_id):
            raise ValueError("Codex provider ID must be a TOML-safe identifier")


@dataclass(frozen=True, slots=True)
class HindsightTargetOptions:
    provider: str = "openai"
    max_concurrent: int = 1

    def __post_init__(self) -> None:
        if not self.provider:
            raise ValueError("Hindsight provider is required")
        if self.max_concurrent <= 0:
            raise ValueError("Hindsight max_concurrent must be positive")


@dataclass(frozen=True, slots=True)
class ApplicationTargetConfiguration:
    gateway_endpoint: str
    service_name: str
    context_window: int | None = None
    sampling_profiles: Mapping[str, SamplingProfile] = field(default_factory=dict)
    target: CodexTargetOptions | HindsightTargetOptions = field(
        default_factory=CodexTargetOptions
    )
    credential_path: Path | None = None
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
        if self.context_window is not None and self.context_window <= 0:
            raise ValueError("context_window must be positive")
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


Replace = Callable[[Path, bytes], None]
TestResult = TypeVar("TestResult")
TestRequest = Callable[[str, str, Mapping[str, object]], TestResult]


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
    validate_application_target_sampling_profiles(
        application_target, configuration.sampling_profiles
    )


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
