"""Command-line client for the mlxd control interface."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from . import dashboard
from .control import (
    ControlClient,
    ControlDomainError,
    ControlRequest,
    ControlResult,
    parse_timestamp,
)
from .paths import resolve_paths


_LAUNCHD_LABEL = "io.nisavid.mlxd"
_ACTIVATION_WAIT_SECONDS = 5.0
_DASHBOARD_CONTROL_TIMEOUT_SECONDS = 1.0


def main(
    argv: Sequence[str] | None = None,
    *,
    activator: Callable[[], None] | None = None,
    launchctl_path: str = "/bin/launchctl",
    platform: str | None = None,
    uid: int | None = None,
    activation_wait_seconds: float = _ACTIVATION_WAIT_SECONDS,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    socket_path = resolve_paths().state_dir / "mlxd.sock"
    if activator is None:
        effective_platform = sys.platform if platform is None else platform
        effective_uid = os.getuid() if uid is None else uid

        def activate() -> None:
            activate_daemon(
                launchctl_path=launchctl_path,
                platform=effective_platform,
                uid=effective_uid,
            )

        activator = activate
    if args.command == "dashboard":
        client = ControlClient(
            socket_path, timeout_seconds=_DASHBOARD_CONTROL_TIMEOUT_SECONDS
        )
        try:
            _send_with_activation(
                client,
                ControlRequest("status"),
                activator,
                wait_seconds=activation_wait_seconds,
            )
        except ControlDomainError as error:
            print(f"mlxctl: {error.message}", file=sys.stderr)
            return 1
        except OSError as error:
            print(
                f"mlxctl: cannot reach mlxd: {error.strerror or error}",
                file=sys.stderr,
            )
            return 1
        return dashboard.run_dashboard(client, args.refresh_interval)

    request = _request(args)
    try:
        result = _send_with_activation(
            ControlClient(socket_path),
            request,
            activator,
            wait_seconds=activation_wait_seconds,
        )
    except ControlDomainError as error:
        print(f"mlxctl: {error.message}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"mlxctl: cannot reach mlxd: {error.strerror or error}", file=sys.stderr)
        return 1

    value = _plain(result.value)
    if args.json:
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    else:
        _print_human(args.command, value)
    return 0


def activate_daemon(
    *,
    launchctl_path: str = "/bin/launchctl",
    platform: str | None = None,
    uid: int | None = None,
) -> None:
    """Ask launchd to start the per-user daemon at the activation seam."""
    effective_platform = sys.platform if platform is None else platform
    if effective_platform != "darwin":
        raise ControlDomainError(
            "daemon_unavailable", "mlxd is not running and launchd is unavailable"
        )
    effective_uid = os.getuid() if uid is None else uid
    try:
        result = subprocess.run(
            [
                launchctl_path,
                "kickstart",
                f"gui/{effective_uid}/{_LAUNCHD_LABEL}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ControlDomainError(
            "activation_failed", "launchd could not start mlxd"
        ) from error
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        message = detail[-1] if detail else "launchctl kickstart failed"
        raise ControlDomainError("activation_failed", message)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mlxctl", description="Manage local MLX inference servers."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "stop"):
        command = commands.add_parser(name, help=f"{name} a configured server")
        command.add_argument("server")
        command.add_argument("--json", action="store_true")
    status = commands.add_parser("status", help="show lifecycle status")
    status.add_argument("server", nargs="?")
    status.add_argument("--json", action="store_true")
    models = commands.add_parser("models", help="show advertised models")
    models.add_argument("server")
    models.add_argument("--json", action="store_true")
    metrics = commands.add_parser("metrics", help="show collected metrics")
    metrics.add_argument("server", nargs="?")
    metrics.add_argument("--model", dest="model_alias")
    metrics.add_argument("--start", type=_timestamp)
    metrics.add_argument("--end", type=_timestamp)
    metrics.add_argument("--json", action="store_true")
    dashboard_command = commands.add_parser(
        "dashboard", help="monitor and control configured servers"
    )
    dashboard_command.add_argument(
        "--refresh-interval",
        type=_positive_seconds,
        default=1.0,
        metavar="SECONDS",
        help="seconds between automatic refreshes (default: 1)",
    )
    return parser


def _request(args: argparse.Namespace) -> ControlRequest:
    return ControlRequest(
        command=args.command,
        server_id=getattr(args, "server", None),
        model_alias=getattr(args, "model_alias", None),
        start=getattr(args, "start", None),
        end=getattr(args, "end", None),
    )


def _send_with_activation(
    client: ControlClient,
    request: ControlRequest,
    activator: Callable[[], None],
    *,
    wait_seconds: float,
) -> ControlResult:
    try:
        return client.send(request)
    except OSError as error:
        if not _connection_unavailable(error):
            raise
    activator()
    deadline = time.monotonic() + wait_seconds
    last_error: OSError | None = None
    while True:
        try:
            return client.send(request)
        except OSError as error:
            if not _connection_unavailable(error):
                raise
            last_error = error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ControlDomainError(
                "daemon_unavailable", "mlxd did not open its control socket in time"
            ) from last_error
        time.sleep(min(0.05, remaining))


def _connection_unavailable(error: OSError) -> bool:
    return isinstance(
        error,
        (
            FileNotFoundError,
            ConnectionRefusedError,
        ),
    ) or error.errno in {2, 61}


def _timestamp(value: str) -> datetime:
    try:
        return parse_timestamp(value)
    except ControlDomainError as error:
        raise argparse.ArgumentTypeError(error.message) from error


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive finite number") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return seconds


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _print_human(command: str, value: object) -> None:
    if not isinstance(value, dict):
        raise TypeError(
            f"expected an object response for {command!r}, got {type(value).__name__}"
        )
    if command in {"start", "stop"}:
        print(_status_line(value))
        return
    if command == "status":
        config_error = value.get("config_error")
        if config_error:
            print(f"Configuration error: {config_error}", file=sys.stderr)
        servers = value.get("servers", [])
        if not servers:
            print("No configured or running servers.")
        for status in servers:
            print(_status_line(status))
        return
    if command == "models":
        models = value.get("models", [])
        server_id = value.get("server_id")
        if models:
            print(f"{server_id} advertises:")
            for model in models:
                print(f"  {model}")
        else:
            print(f"{server_id} has no advertised models.")
        return
    summaries = value.get("summaries", [])
    if not summaries:
        print("No metrics match the requested filters.")
        return
    for summary in summaries:
        print(
            f"{summary['server_id']} / {summary['model_alias']}: "
            f"{summary['request_count']} requests, "
            f"{summary['success_count']} completed, "
            f"{summary['failure_count']} failed"
        )


def _status_line(status: Mapping[str, object]) -> str:
    server_id = status["server_id"]
    lifecycle = status["lifecycle"]
    endpoint = status.get("client_endpoint")
    suffix = ""
    if isinstance(endpoint, dict):
        suffix = f" at http://{endpoint['host']}:{endpoint['port']}"
    error = status.get("error")
    if error:
        suffix += f" ({error})"
    return f"{server_id} is {lifecycle}{suffix}."


if __name__ == "__main__":
    raise SystemExit(main())
