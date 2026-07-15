"""Concrete host-facing adapters for the supported-v1 interfaces."""

from __future__ import annotations

import os
import re
import socket
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from mlxctl.application.dispatch import OperationRequest
from mlxctl.interfaces.tui import ServiceSnapshot, TuiSnapshot


_RESOURCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class LaunchdPort(Protocol):
    def status(self): ...

    def register(self): ...

    def kickstart(self): ...


class StatusDispatcher(Protocol):
    def execute(self, request: OperationRequest): ...


class MetricsStore(Protocol):
    def metrics(self, kind: str | None = None) -> Sequence[Mapping[str, object]]: ...


class LaunchdSupervisorActivator:
    """Explicitly register/start mlxd and await its private control socket."""

    def __init__(
        self,
        launchd: LaunchdPort,
        socket_path: str | Path,
        *,
        timeout_seconds: float = 5.0,
        poll_interval: float = 0.05,
        socket_ready: Callable[[Path], bool] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_seconds <= 0 or poll_interval <= 0:
            raise ValueError("activation timeouts must be positive")
        self._launchd = launchd
        self._socket_path = Path(socket_path)
        self._timeout = timeout_seconds
        self._poll = poll_interval
        self._socket_ready = socket_ready or private_socket_ready
        self._monotonic = monotonic
        self._sleep = sleep

    def activate(self) -> None:
        status = self._launchd.status()
        if not status.registered:
            self._launchd.register()
        if not status.running:
            self._launchd.kickstart()
        deadline = self._monotonic() + self._timeout
        while not self._socket_ready(self._socket_path):
            if self._monotonic() >= deadline:
                raise RuntimeError(
                    "mlxd did not open its private control socket before the timeout"
                )
            self._sleep(self._poll)


class LocalSnapshotProvider:
    """Adapt the read-only status operation to the TUI snapshot contract."""

    def __init__(self, dispatcher: StatusDispatcher) -> None:
        self._dispatcher = dispatcher

    def snapshot(self) -> TuiSnapshot:
        result = self._dispatcher.execute(OperationRequest("status"))
        value = result.value
        supervisor = _mapping(value.get("supervisor"))
        gateway = _mapping(value.get("gateway"))
        services = tuple(
            self._service(_mapping(item)) for item in _sequence(value.get("services"))
        )
        host = str(gateway.get("host", "127.0.0.1"))
        port = gateway.get("port")
        gateway_state = str(gateway.get("state", "stopped"))
        gateway_text = (
            f"{gateway_state} · {host}:{port}"
            if isinstance(port, int)
            else gateway_state
        )
        operations = _sequence(value.get("operations"))
        active = sum(
            1
            for item in operations
            if _mapping(item).get("status") in {"queued", "running", "resuming"}
        )
        return TuiSnapshot(
            supervisor=str(supervisor.get("state", "stopped")),
            gateway=gateway_text,
            services=services,
            active_operations=active,
            pressure=str(value.get("pressure", supervisor.get("pressure", "unknown"))),
        )

    @staticmethod
    def _service(item: Mapping[str, object]) -> ServiceSnapshot:
        desired = _mapping(item.get("desired"))
        run = _mapping(item.get("run"))
        return ServiceSnapshot(
            name=str(item.get("name", "unknown")),
            state=str(run.get("state", "stopped")),
            model=str(desired.get("model_alias", "unconfigured")),
            runtime=str(desired.get("runtime_installation", "unconfigured")),
            route=str(desired.get("route")) if desired.get("route") else None,
            pinned=bool(desired.get("pinned", False)),
            detail=str(run["error"]) if run.get("error") else None,
        )


class PrivateLogReader:
    """Read bounded tails of product-owned logs without following links."""

    def __init__(
        self, log_dir: str | Path, *, max_lines: int = 200, max_bytes: int = 1024 * 1024
    ) -> None:
        if max_lines <= 0 or max_bytes <= 0:
            raise ValueError("log bounds must be positive")
        self._log_dir = Path(log_dir)
        self._max_lines = max_lines
        self._max_bytes = max_bytes

    def read(
        self, scope: str, resource: str | None = None
    ) -> Sequence[Mapping[str, object]]:
        paths = self._paths(scope, resource)
        rows: list[dict[str, object]] = []
        for path in paths:
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_size > self._max_bytes
            ):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            rows.extend(
                {"source": path.name, "message": line}
                for line in lines[-self._max_lines :]
            )
        return tuple(rows[-self._max_lines :])

    def _paths(self, scope: str, resource: str | None) -> tuple[Path, ...]:
        if resource is not None:
            if _RESOURCE.fullmatch(resource) is None:
                return ()
            return (self._log_dir / f"{resource}.log",)
        if scope in {"supervisor", "gateway"}:
            return (self._log_dir / f"{scope}.log",)
        try:
            return tuple(sorted(self._log_dir.glob("*.log")))
        except OSError:
            return ()


class StateMetricsSource:
    """Expose content-free operational metrics through the backend query port."""

    def __init__(self, state: MetricsStore) -> None:
        self._state = state

    def query(
        self, scope: str, resource: str | None = None
    ) -> Sequence[Mapping[str, object]]:
        return tuple(
            item
            for item in self._state.metrics()
            if scope == "all"
            or (
                item.get("scope") == scope
                and (resource is None or item.get("resource") == resource)
            )
        )


def private_socket_ready(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        return probe.connect_ex(str(path)) == 0
    except OSError:
        return False
    finally:
        probe.close()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    return value if isinstance(value, tuple | list) else ()
