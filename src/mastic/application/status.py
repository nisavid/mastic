"""Application-owned status read models shared by supported interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


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


class SnapshotProvider(Protocol):
    def snapshot(self) -> StatusSnapshot: ...
