"""Foreground supervisor daemon for local MLX inference servers."""

from __future__ import annotations

import argparse
import math
import os
import signal
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from .config import ConfigError, load_config
from .control import ControlPlane, ControlSocketError, UnixControlServer
from .metrics import MetricsEngine
from .paths import resolve_paths
from .supervisor import GetStatus, LifecycleState, Supervisor


DEFAULT_IDLE_GRACE_SECONDS = 15.0
_POLL_INTERVAL_SECONDS = 0.05
_ACTIVE_STATES = frozenset(
    {
        LifecycleState.STARTING,
        LifecycleState.READY,
        LifecycleState.UNHEALTHY,
        LifecycleState.STOPPING,
    }
)


class _ActivityClock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = time.monotonic()

    def touch(self) -> None:
        with self._lock:
            self._last = time.monotonic()

    def read(self) -> float:
        with self._lock:
            return self._last


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except (
        ConfigError,
        ControlSocketError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        print(f"mlxd: {error}", file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mlxd", description="Supervise local MLX inference servers."
    )
    parser.add_argument("--config", type=Path, help="config.toml path")
    parser.add_argument("--state-dir", type=Path, help="runtime state directory")
    parser.add_argument("--log-dir", type=Path, help="server log directory")
    parser.add_argument("--socket", type=Path, help="Unix control socket path")
    parser.add_argument(
        "--idle-grace-seconds",
        type=_nonnegative_float,
        default=DEFAULT_IDLE_GRACE_SECONDS,
        help="exit after this many idle seconds with no active servers",
    )
    return parser


def _run(args: argparse.Namespace) -> int:
    resolved = resolve_paths()
    config_path = args.config or resolved.config_file
    state_dir = args.state_dir or resolved.state_dir
    log_dir = args.log_dir or resolved.log_dir
    socket_path = args.socket or state_dir / "mlxd.sock"

    config = load_config(config_path)
    _secure_directory(state_dir)
    _secure_directory(log_dir)
    old_umask = os.umask(0o077)
    metrics: MetricsEngine | None = None
    supervisor: Supervisor | None = None
    control: UnixControlServer | None = None
    previous_handlers: dict[signal.Signals, object] = {}
    stop = threading.Event()
    try:
        metrics_path = state_dir / "metrics.db"
        metrics = MetricsEngine(metrics_path, config.metrics.retention_days)
        os.chmod(metrics_path, 0o600)
        metrics.prune(_utc_now())
        supervisor = Supervisor(
            config.daemon,
            metrics,
            state_dir,
            log_dir,
        )
        activity = _ActivityClock()
        control = UnixControlServer(
            socket_path,
            ControlPlane(config_path, supervisor, metrics),
            activity_callback=activity.touch,
        )

        if threading.current_thread() is threading.main_thread():
            for signal_number in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signal_number] = signal.getsignal(signal_number)
                signal.signal(signal_number, lambda _number, _frame: stop.set())

        control.start()
        inactive_since = time.monotonic()
        while not stop.wait(_POLL_INTERVAL_SECONDS):
            statuses = supervisor.apply(GetStatus())
            active = any(item.lifecycle in _ACTIVE_STATES for item in statuses)
            now = time.monotonic()
            if active or control.has_active_clients:
                inactive_since = None
                continue
            if inactive_since is None:
                inactive_since = now
            idle_since = max(inactive_since, activity.read())
            if now - idle_since >= args.idle_grace_seconds:
                break
        return 0
    finally:
        if control is not None:
            control.close()
        if supervisor is not None:
            supervisor.close()
        for signal_number, previous in previous_handlers.items():
            signal.signal(signal_number, previous)
        os.umask(old_umask)


def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return parsed


def _utc_now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
