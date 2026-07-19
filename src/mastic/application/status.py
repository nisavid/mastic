"""Application-owned status read models shared by supported interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Protocol


@dataclass(frozen=True, slots=True)
class ServiceSnapshot:
    name: str
    state: str
    model: str
    runtime: str
    route: str | None = None
    pinned: bool = False
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class GatewaySnapshot:
    state: str
    host: str | None = None
    port: int | None = None


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    supervisor: str
    gateway: GatewaySnapshot
    services: tuple[ServiceSnapshot, ...] = ()
    active_operations: int = 0
    pressure: str = "unknown"
    completion: str = "partial"
    readiness: str = "pending"
    application_target_readiness: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "application_target_readiness",
            MappingProxyType(dict(self.application_target_readiness)),
        )


class SnapshotProvider(Protocol):
    def snapshot(self) -> StatusSnapshot:
        pass
