"""Render and run the local MLX server dashboard."""

from __future__ import annotations

import shutil
import sys
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, replace

from .control import ControlClient, ControlDomainError, ControlRequest


@dataclass(frozen=True, slots=True)
class DashboardRow:
    """One immutable server observation shown by the dashboard."""

    server_id: str
    model_alias: str | None
    lifecycle: str
    client_endpoint: str | None = None
    pid: int | None = None
    advertised_models: tuple[str, ...] = ()
    request_count: int | None = None
    success_count: int | None = None
    failure_count: int | None = None
    total_tokens: int | None = None
    average_duration_ms: float | None = None
    average_ttft_ms: float | None = None
    peak_rss_bytes: int | None = None
    average_cpu_percent: float | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    """One immutable dashboard observation and its selected row."""

    rows: tuple[DashboardRow, ...] = ()
    selected_index: int = 0
    config_error: str | None = None
    control_error: str | None = None
    feedback: str | None = None


def render_plain(snapshot: DashboardSnapshot, width: int) -> str:
    """Render one deterministic terminal-safe dashboard snapshot."""
    lines = ["MLX server dashboard"]
    if snapshot.config_error:
        lines.append(f"Configuration error: {snapshot.config_error}")
    if snapshot.control_error:
        lines.append(f"Control error: {snapshot.control_error}")
    if snapshot.feedback:
        lines.append(snapshot.feedback)
    if not snapshot.rows:
        lines.append("No configured or running servers.")
    if width < 60:
        lines.extend(_narrow_rows(snapshot))
        return "\n".join(line[: max(width, 1)] for line in lines)
    for index, row in enumerate(snapshot.rows):
        selected = ">" if index == snapshot.selected_index else " "
        lines.extend(
            (
                f"{selected} {row.server_id} / {_value(row.model_alias)} "
                f"[{row.lifecycle}]",
                "    endpoint "
                f"{_value(row.client_endpoint)} | PID {_value(row.pid)} | models "
                f"{', '.join(row.advertised_models) or '-'}",
                "    requests "
                f"{_value(row.request_count)} | success {_value(row.success_count)} | "
                f"failure {_value(row.failure_count)} | tokens {_value(row.total_tokens)} | "
                f"latency {_milliseconds(row.average_duration_ms)} | "
                f"TTFT {_milliseconds(row.average_ttft_ms)} | "
                f"peak RSS {_bytes(row.peak_rss_bytes)} | "
                f"CPU {_percent(row.average_cpu_percent)}",
            )
        )
        if row.error:
            lines.append(f"    lifecycle error: {row.error}")
    return "\n".join(line[: max(width, 1)] for line in lines)


def run_dashboard(client: ControlClient, refresh_interval: float) -> int:
    """Run curses on a terminal, or print one snapshot when redirected."""
    if sys.stdin.isatty() and sys.stdout.isatty():
        return _run_curses(client, refresh_interval)
    snapshot = _read_snapshot(client)
    width = shutil.get_terminal_size(fallback=(120, 24)).columns
    print(render_plain(snapshot, width))
    return 0


def _read_snapshot(
    client: ControlClient,
    *,
    selected_server_id: str | None = None,
    feedback: str | None = None,
) -> DashboardSnapshot:
    try:
        status_value = client.send(ControlRequest("status")).value
    except (ControlDomainError, OSError) as error:
        return DashboardSnapshot(
            control_error=_control_message(error), feedback=feedback
        )
    if not isinstance(status_value, Mapping):
        return DashboardSnapshot(control_error="status response is invalid")
    metrics_error: str | None = None
    try:
        metrics_value = client.send(ControlRequest("metrics")).value
    except (ControlDomainError, OSError) as error:
        metrics_value = {}
        metrics_error = _control_message(error)
    summaries = _metric_summaries(metrics_value)
    rows = tuple(
        _dashboard_row(item, summaries)
        for item in _mapping_items(status_value.get("servers"))
    )
    selected_index = 0
    if selected_server_id is not None:
        selected_index = next(
            (
                index
                for index, row in enumerate(rows)
                if row.server_id == selected_server_id
            ),
            0,
        )
    config_error = status_value.get("config_error")
    return DashboardSnapshot(
        rows=rows,
        selected_index=selected_index,
        config_error=config_error if isinstance(config_error, str) else None,
        control_error=metrics_error,
        feedback=feedback,
    )


def _metric_summaries(
    value: object,
) -> dict[tuple[str, str | None], Mapping[str, object]]:
    if not isinstance(value, Mapping):
        return {}
    summaries: dict[tuple[str, str | None], Mapping[str, object]] = {}
    for item in _mapping_items(value.get("summaries")):
        server_id = item.get("server_id")
        model_alias = item.get("model_alias")
        if isinstance(server_id, str) and (
            isinstance(model_alias, str) or model_alias is None
        ):
            summaries[(server_id, model_alias)] = item
    return summaries


def _dashboard_row(
    status: Mapping[str, object],
    summaries: Mapping[tuple[str, str | None], Mapping[str, object]],
) -> DashboardRow:
    server_id = _string(status.get("server_id"))
    model_alias = _optional_string(status.get("model_alias"))
    summary = summaries.get((server_id, model_alias), {})
    endpoint = status.get("client_endpoint")
    return DashboardRow(
        server_id=server_id,
        model_alias=model_alias,
        lifecycle=_string(status.get("lifecycle"), default="unknown"),
        client_endpoint=_client_endpoint(endpoint),
        pid=_integer(status.get("pid")),
        advertised_models=tuple(
            item
            for item in _sequence(status.get("advertised_models"))
            if isinstance(item, str)
        ),
        request_count=_integer(summary.get("request_count")),
        success_count=_integer(summary.get("success_count")),
        failure_count=_integer(summary.get("failure_count")),
        total_tokens=_integer(summary.get("total_tokens")),
        average_duration_ms=_number(summary.get("average_duration_ms")),
        average_ttft_ms=_number(summary.get("average_ttft_ms")),
        peak_rss_bytes=_integer(summary.get("peak_rss_bytes")),
        average_cpu_percent=_number(summary.get("average_cpu_percent")),
        error=_optional_string(status.get("error")),
    )


def _mapping_items(value: object) -> tuple[Mapping[str, object], ...]:
    return tuple(item for item in _sequence(value) if isinstance(item, Mapping))


def _sequence(value: object) -> tuple[object, ...]:
    return value if isinstance(value, tuple) else ()


def _client_endpoint(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    host = value.get("host")
    port = value.get("port")
    if not isinstance(host, str) or type(port) is not int:
        return None
    return f"http://{host}:{port}"


def _string(value: object, *, default: str = "-") -> str:
    return value if isinstance(value, str) else default


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: object) -> int | None:
    return value if type(value) is int else None


def _number(value: object) -> float | None:
    return float(value) if type(value) in {int, float} else None


def _control_message(error: ControlDomainError | OSError) -> str:
    if isinstance(error, ControlDomainError):
        return error.message
    return f"cannot reach mlxd: {error.strerror or error}"


def _run_curses(client: ControlClient, refresh_interval: float) -> int:
    import curses

    return curses.wrapper(_curses_loop, client, refresh_interval)


def _curses_loop(screen: object, client: ControlClient, refresh_interval: float) -> int:
    import curses

    window = screen
    window.keypad(True)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    snapshot = _read_snapshot(client)
    next_refresh = time.monotonic() + refresh_interval
    pending: Future[DashboardSnapshot] | None = None
    dirty = True
    try:
        while True:
            if pending is not None and pending.done():
                selected_server_id = _selected_server_id(snapshot)
                try:
                    refreshed = pending.result()
                except Exception:
                    refreshed = replace(
                        snapshot,
                        control_error="dashboard refresh failed",
                        feedback=None,
                    )
                snapshot = _select_server(refreshed, selected_server_id)
                pending = None
                next_refresh = time.monotonic() + refresh_interval
                dirty = True

            if dirty:
                _draw_curses(window, snapshot)
                dirty = False
            window.timeout(50)
            key = window.getch()
            if key in (ord("q"), ord("Q")):
                return 0
            if key in (curses.KEY_UP, ord("k"), ord("K")):
                snapshot = _move_selection(snapshot, -1)
                dirty = True
                continue
            if key in (curses.KEY_DOWN, ord("j"), ord("J")):
                snapshot = _move_selection(snapshot, 1)
                dirty = True
                continue
            if key in (ord("r"), ord("R")):
                if pending is None:
                    pending = _submit_dashboard(
                        _read_snapshot,
                        client,
                        selected_server_id=_selected_server_id(snapshot),
                        feedback="Refreshed.",
                    )
                    snapshot = replace(snapshot, feedback="Refreshing...")
                    dirty = True
                continue
            if key in (ord("s"), ord("S")):
                snapshot, pending = _schedule_action(pending, client, snapshot, "start")
                dirty = True
                continue
            if key in (ord("x"), ord("X")):
                snapshot, pending = _schedule_action(pending, client, snapshot, "stop")
                dirty = True
                continue
            if key == curses.KEY_RESIZE:
                dirty = True
                continue
            if time.monotonic() >= next_refresh and pending is None:
                pending = _submit_dashboard(
                    _read_snapshot,
                    client,
                    selected_server_id=_selected_server_id(snapshot),
                )
    finally:
        if pending is not None:
            pending.cancel()


def _schedule_action(
    pending: Future[DashboardSnapshot] | None,
    client: ControlClient,
    snapshot: DashboardSnapshot,
    command: str,
) -> tuple[DashboardSnapshot, Future[DashboardSnapshot] | None]:
    row = _selected_row(snapshot)
    if row is None:
        return replace(snapshot, feedback="No server is selected."), pending
    if pending is not None:
        return replace(
            snapshot, feedback="A control request is already running."
        ), pending
    eligible = (
        row.lifecycle in {"stopped", "failed"}
        if command == "start"
        else row.lifecycle in {"starting", "ready", "unhealthy"}
    )
    if not eligible:
        return (
            replace(
                snapshot,
                feedback=f"{row.server_id} is {row.lifecycle}; cannot {command}.",
            ),
            None,
        )
    future = _submit_dashboard(_apply_action, client, row.server_id, command)
    return replace(
        snapshot, feedback=f"{command.title()}ing {row.server_id}..."
    ), future


def _submit_dashboard(
    function: Callable[..., DashboardSnapshot],
    *args: object,
    **kwargs: object,
) -> Future[DashboardSnapshot]:
    future: Future[DashboardSnapshot] = Future()

    def run() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as error:
            future.set_exception(error)

    threading.Thread(
        target=run,
        name="mlxctl-dashboard-control",
        daemon=True,
    ).start()
    return future


def _apply_action(
    client: ControlClient, server_id: str, command: str
) -> DashboardSnapshot:
    try:
        result = client.send(ControlRequest(command, server_id)).value
        lifecycle = result.get("lifecycle") if isinstance(result, Mapping) else None
        feedback = (
            f"{server_id} is {lifecycle}."
            if isinstance(lifecycle, str)
            else f"{command.title()} requested for {server_id}."
        )
    except (ControlDomainError, OSError) as error:
        feedback = f"{command.title()} failed: {_control_message(error)}"
    return _read_snapshot(client, selected_server_id=server_id, feedback=feedback)


def _move_selection(snapshot: DashboardSnapshot, offset: int) -> DashboardSnapshot:
    if not snapshot.rows:
        return snapshot
    selected_index = min(
        max(snapshot.selected_index + offset, 0), len(snapshot.rows) - 1
    )
    return replace(snapshot, selected_index=selected_index, feedback=None)


def _selected_row(snapshot: DashboardSnapshot) -> DashboardRow | None:
    if not snapshot.rows:
        return None
    index = min(max(snapshot.selected_index, 0), len(snapshot.rows) - 1)
    return snapshot.rows[index]


def _selected_server_id(snapshot: DashboardSnapshot) -> str | None:
    row = _selected_row(snapshot)
    return row.server_id if row is not None else None


def _select_server(
    snapshot: DashboardSnapshot, server_id: str | None
) -> DashboardSnapshot:
    if server_id is None:
        return snapshot
    index = next(
        (
            index
            for index, row in enumerate(snapshot.rows)
            if row.server_id == server_id
        ),
        0,
    )
    return replace(snapshot, selected_index=index)


def _draw_curses(window: object, snapshot: DashboardSnapshot) -> None:
    import curses

    height, width = window.getmaxyx()
    window.erase()
    if width < 60 or height < 10:
        lines = [
            f"Terminal too small: {width}x{height}",
            "Minimum size: 60 columns x 10 rows",
            "q quit | r refresh",
        ]
        if snapshot.feedback:
            lines.append(snapshot.feedback)
    else:
        lines = _visible_full_lines(snapshot, width, height - 1)
        lines.append("Up/Down or j/k select | s start | x stop | r refresh | q quit")
    for row_number, line in enumerate(lines[:height]):
        try:
            window.addnstr(row_number, 0, line, max(width - 1, 1))
        except curses.error:
            pass
    try:
        window.refresh()
    except curses.error:
        pass


def _visible_full_lines(
    snapshot: DashboardSnapshot, width: int, available_height: int
) -> list[str]:
    lines = render_plain(snapshot, width).splitlines()
    if len(lines) <= available_height:
        return lines
    selected_line = next(
        (index for index, line in enumerate(lines) if line.startswith(">")), 0
    )
    start = max(0, selected_line - available_height // 2)
    start = min(start, len(lines) - available_height)
    return lines[start : start + available_height]


def _narrow_rows(snapshot: DashboardSnapshot) -> list[str]:
    lines: list[str] = []
    for index, row in enumerate(snapshot.rows):
        selected = ">" if index == snapshot.selected_index else " "
        lines.extend(
            (
                f"{selected} {row.server_id}/{_value(row.model_alias)} [{row.lifecycle}]",
                f"  {_value(row.client_endpoint)} | PID {_value(row.pid)}",
                f"  models {', '.join(row.advertised_models) or '-'}",
                f"  {_value(row.request_count)} req | {_value(row.success_count)} ok | "
                f"{_value(row.failure_count)} failed | {_value(row.total_tokens)} tok",
                f"  latency {_milliseconds(row.average_duration_ms)} | "
                f"TTFT {_milliseconds(row.average_ttft_ms)}",
                f"  peak RSS {_bytes(row.peak_rss_bytes)} | "
                f"CPU {_percent(row.average_cpu_percent)}",
            )
        )
        if row.error:
            lines.append(f"  lifecycle error: {row.error}")
    return lines


def _value(value: object | None) -> str:
    return "-" if value is None else str(value)


def _milliseconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f} ms"


def _bytes(value: int | None) -> str:
    if value is None:
        return "-"
    if value >= 1024**3:
        return f"{value / 1024**3:.1f} GiB"
    if value >= 1024**2:
        return f"{value / 1024**2:.1f} MiB"
    if value >= 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value} B"


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}%"
