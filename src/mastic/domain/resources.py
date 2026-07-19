"""Desired, physical, and observed local-inference resources."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


_RESOURCE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_INSTALLATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]*\Z")
_IMMUTABLE_REVISION = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")


def validate_runtime_installation_id(value: object) -> str:
    """Return one path-safe Runtime Installation identity."""

    if (
        not isinstance(value, str)
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or _INSTALLATION_ID.fullmatch(value) is None
    ):
        raise ValueError("runtime installation ID contains unsafe characters")
    return value


class ResourceName(str):
    """A stable user-facing resource identity safe for paths and routing."""

    def __new__(cls, value: str) -> ResourceName:
        if (
            not isinstance(value, str)
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or _RESOURCE_NAME.fullmatch(value) is None
        ):
            raise ValueError("resource name must match [A-Za-z0-9][A-Za-z0-9._-]*")
        return str.__new__(cls, value)


class RuntimeFamily(StrEnum):
    MLX_LM = "mlx-lm"
    MLX_VLM = "mlx-vlm"
    OPTIQ = "optiq"


class ActivationPolicy(StrEnum):
    MANUAL = "manual"
    SUPERVISOR = "supervisor"


class ServiceRunState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    UNHEALTHY = "unhealthy"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class RuntimeInstallation:
    installation_id: str
    family: RuntimeFamily
    version: str
    provenance: str
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        validate_runtime_installation_id(self.installation_id)
        if not self.version or not self.provenance:
            raise ValueError("runtime version and provenance are required")


@dataclass(frozen=True, slots=True)
class ModelRevision:
    repository: str
    revision: str

    def __post_init__(self) -> None:
        if not self.repository or self.repository.startswith(("/", ".")):
            raise ValueError("model repository must be a repository ID")
        if _IMMUTABLE_REVISION.fullmatch(self.revision) is None:
            raise ValueError("model revision must be an immutable commit SHA")


@dataclass(frozen=True, slots=True)
class CachedRevision:
    revision: ModelRevision
    complete: bool
    size_bytes: int | None = None
    cache_path: str | None = None

    def __post_init__(self) -> None:
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("cached size must be non-negative")


@dataclass(frozen=True, slots=True)
class ModelInstallation:
    name: str
    revision: ModelRevision
    provenance: str = "cached"
    path: str | None = None

    def __post_init__(self) -> None:
        ResourceName(self.name)
        if self.provenance not in {"cached", "adopted"}:
            raise ValueError("model provenance must be cached or adopted")
        if self.provenance == "adopted":
            if self.path is None or not self.path.startswith("/"):
                raise ValueError("adopted model path must be absolute")
        elif self.path is not None:
            raise ValueError("cached model installations cannot declare a path")


@dataclass(frozen=True, slots=True)
class ModelAlias:
    name: ResourceName
    installation_name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", ResourceName(self.name))
        ResourceName(self.installation_name)


@dataclass(frozen=True, slots=True)
class InferenceService:
    name: ResourceName
    model_alias: ResourceName
    runtime_installation: str
    route: ResourceName
    activation: ActivationPolicy = ActivationPolicy.MANUAL
    pinned: bool = False
    options: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", ResourceName(self.name))
        object.__setattr__(self, "model_alias", ResourceName(self.model_alias))
        object.__setattr__(self, "route", ResourceName(self.route))
        validate_runtime_installation_id(self.runtime_installation)
        object.__setattr__(
            self,
            "options",
            MappingProxyType(
                {
                    str(key): _freeze_option_value(value)
                    for key, value in self.options.items()
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class ServiceRun:
    run_id: str
    service_name: ResourceName
    state: ServiceRunState
    upstream_port: int | None = None
    pid: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "service_name", ResourceName(self.service_name))
        if not self.run_id:
            raise ValueError("run ID is required")
        if self.upstream_port is not None and (
            type(self.upstream_port) is not int or not 1 <= self.upstream_port <= 65535
        ):
            raise ValueError("upstream port must be in 1..65535")


def _freeze_option_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_option_value(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_option_value(item) for item in value)
    return value
