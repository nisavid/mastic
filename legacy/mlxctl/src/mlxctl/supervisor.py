"""Own managed inference processes behind one lifecycle interface."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TypeAlias

try:
    import psutil
except ImportError:  # pragma: no cover - exercised only without installed dependencies
    psutil = None

from .adapters import AdapterRegistry, Endpoint
from .config import (
    DaemonSettings,
    ModelDefinition,
    ServerDefinition,
    validate_alias,
)
from .metrics import MetricsEngine, ProcessSample
from .metrics_proxy import MetricsProxy
from .probe import ProbeError, probe_liveness, probe_readiness


class LifecycleState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    UNHEALTHY = "unhealthy"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ManagedServerStatus:
    server_id: str
    model_alias: str | None
    lifecycle: LifecycleState
    client_endpoint: Endpoint | None = None
    upstream_endpoint: Endpoint | None = None
    run_id: str | None = None
    pid: int | None = None
    advertised_models: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class StartServer:
    server: ServerDefinition
    model: ModelDefinition


@dataclass(frozen=True, slots=True)
class StopServer:
    server_id: str


@dataclass(frozen=True, slots=True)
class GetStatus:
    server_id: str | None = None


@dataclass(frozen=True, slots=True)
class GetModels:
    server_id: str


LifecycleCommand: TypeAlias = StartServer | StopServer | GetStatus | GetModels
LifecycleResult: TypeAlias = (
    ManagedServerStatus | tuple[ManagedServerStatus, ...] | tuple[str, ...]
)


@dataclass(slots=True)
class _ManagedServerRun:
    status: ManagedServerStatus
    start_done: threading.Event
    process: subprocess.Popen[bytes] | None = None
    process_identity: object | None = None
    process_create_time: float | None = None
    proxy: MetricsProxy | None = None
    log_stream: object | None = None
    monitor_stop: threading.Event | None = None
    monitor: threading.Thread | None = None
    stop_requested: bool = False
    operation_lock: threading.RLock = field(default_factory=threading.RLock)


class Supervisor:
    """Own every child run through ``apply`` and bounded ``close``."""

    _PROBE_INTERVAL_SECONDS = 0.02
    _PROBE_TIMEOUT_SECONDS = 0.1
    _STATE_FILE = "runtime.json"

    def __init__(
        self,
        settings: DaemonSettings,
        metrics_engine: MetricsEngine,
        state_dir: str | Path,
        log_dir: str | Path,
        adapter_registry: AdapterRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._metrics = metrics_engine
        self._state_dir = Path(state_dir)
        self._log_dir = Path(log_dir)
        self._registry = adapter_registry or AdapterRegistry()
        self._lock = threading.RLock()
        self._runs: dict[str, _ManagedServerRun] = {}
        self._closed = False
        self._close_complete = threading.Event()
        self._close_error: BaseException | None = None
        self._prepare_directory(self._state_dir)
        self._prepare_directory(self._log_dir)
        self._state_path = self._state_dir / self._STATE_FILE
        self._recover_orphans()

    def apply(self, command: LifecycleCommand) -> LifecycleResult:
        if isinstance(command, StartServer):
            return self._start(command)
        if isinstance(command, StopServer):
            return self._stop(command.server_id)
        if isinstance(command, GetStatus):
            return self._status(command.server_id)
        if isinstance(command, GetModels):
            status = self._status(command.server_id)
            assert isinstance(status, ManagedServerStatus)
            return status.advertised_models
        raise TypeError(f"unsupported lifecycle command {type(command).__name__}")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                owns_close = False
            else:
                self._closed = True
                owns_close = True
                server_ids = tuple(self._runs)
        if not owns_close:
            self._close_complete.wait()
            if self._close_error is not None:
                raise RuntimeError("supervisor close failed") from self._close_error
            return
        try:
            if server_ids:
                with ThreadPoolExecutor(max_workers=len(server_ids)) as pool:
                    tuple(pool.map(self._stop, server_ids))
        except BaseException as error:
            with self._lock:
                self._close_error = error
            raise
        finally:
            self._close_complete.set()

    def _start(self, command: StartServer) -> ManagedServerStatus:
        server = command.server
        model = command.model
        validate_alias(server.name, "server")
        validate_alias(model.alias, "model")
        if server.model != model.alias:
            raise ValueError(
                f"server '{server.name}' expects model alias '{server.model}', not '{model.alias}'"
            )
        wait_for: threading.Event | None = None
        with self._lock:
            if self._closed:
                raise RuntimeError("supervisor is closed")
            current = self._runs.get(server.name)
            if current is not None and current.status.lifecycle in {
                LifecycleState.READY,
                LifecycleState.UNHEALTHY,
            }:
                return current.status
            if current is not None and current.status.lifecycle in {
                LifecycleState.STARTING,
                LifecycleState.STOPPING,
            }:
                wait_for = current.start_done
            else:
                client = Endpoint(server.host, server.port)
                run_id = uuid.uuid4().hex
                status = ManagedServerStatus(
                    server.name,
                    model.alias,
                    LifecycleState.STARTING,
                    client_endpoint=client,
                    run_id=run_id,
                )
                current = _ManagedServerRun(status, threading.Event())
                self._runs[server.name] = current
        if wait_for is not None:
            wait_for.wait(
                self._settings.readiness_timeout_seconds
                + self._settings.stop_timeout_seconds
                + 1
            )
            with self._lock:
                return self._runs[server.name].status
        assert current is not None
        return self._launch(current, server, model)

    def _launch(
        self,
        run: _ManagedServerRun,
        server: ServerDefinition,
        model: ModelDefinition,
    ) -> ManagedServerStatus:
        with run.operation_lock:
            try:
                upstream = self._allocate_upstream()
                proxy = MetricsProxy(
                    run.status.client_endpoint,
                    upstream,
                    self._metrics,
                    server.name,
                    model.alias,
                    run.status.run_id,
                )
                proxy.__enter__()
                run.proxy = proxy
                prepared = self._registry.prepare(server, model, upstream)
                log_stream = self._open_log(server.name)
                run.log_stream = log_stream
                environment = os.environ.copy()
                environment.update(prepared.environment)
                process = subprocess.Popen(
                    prepared.argv,
                    shell=False,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                )
                run.process = process
                (
                    run.process_identity,
                    run.process_create_time,
                ) = _capture_process_identity(process.pid)
                with self._lock:
                    run.status = replace(
                        run.status, upstream_endpoint=upstream, pid=process.pid
                    )
                    self._persist_locked()
            except Exception as error:
                return self._fail(run, f"start failed: {error}", terminate=True)

        deadline = time.monotonic() + self._settings.readiness_timeout_seconds
        upstream_url = f"http://{upstream.host}:{upstream.port}"
        while time.monotonic() < deadline:
            return_code = process.poll()
            if return_code is not None:
                return self._fail(
                    run,
                    f"process exited with status {return_code}",
                    terminate=False,
                )
            with self._lock:
                if run.stop_requested:
                    stopping = True
                else:
                    stopping = False
            if stopping:
                run.start_done.wait(self._settings.stop_timeout_seconds * 2 + 1)
                with self._lock:
                    return run.status
            live = False
            try:
                live = probe_liveness(
                    upstream_url, timeout_seconds=self._PROBE_TIMEOUT_SECONDS
                )
            except ProbeError:
                pass
            try:
                advertised = probe_readiness(
                    upstream_url, timeout_seconds=self._PROBE_TIMEOUT_SECONDS
                )
            except ProbeError:
                advertised = None
            if live and advertised is not None:
                with self._lock:
                    if not run.stop_requested:
                        run.status = replace(
                            run.status,
                            lifecycle=LifecycleState.READY,
                            advertised_models=advertised,
                            error=None,
                        )
                        run.start_done.set()
                        self._start_monitor(run, server.name, model.alias, upstream_url)
                    return run.status
            time.sleep(self._PROBE_INTERVAL_SECONDS)
        return_code = process.poll()
        if return_code is not None:
            return self._fail(
                run,
                f"process exited with status {return_code}",
                terminate=False,
            )
        return self._fail(run, "readiness timed out", terminate=True)

    def _stop(self, server_id: str) -> ManagedServerStatus:
        validate_alias(server_id, "server")
        with self._lock:
            run = self._runs.get(server_id)
            if run is None:
                status = ManagedServerStatus(server_id, None, LifecycleState.STOPPED)
                self._runs[server_id] = _ManagedServerRun(status, threading.Event())
                self._runs[server_id].start_done.set()
                return status
            if run.status.lifecycle is LifecycleState.STOPPED:
                return run.status
            run.stop_requested = True
            run.status = replace(
                run.status, lifecycle=LifecycleState.STOPPING, error=None
            )
            if run.monitor_stop is not None:
                run.monitor_stop.set()
        with run.operation_lock:
            self._close_proxy(run)
            self._terminate_child(run)
            self._close_log(run)
        self._join_monitor(run)
        with self._lock:
            run.status = replace(
                run.status,
                lifecycle=LifecycleState.STOPPED,
                upstream_endpoint=None,
                run_id=None,
                pid=None,
                advertised_models=(),
                error=None,
            )
            run.start_done.set()
            self._persist_locked()
            return run.status

    def _status(
        self, server_id: str | None
    ) -> ManagedServerStatus | tuple[ManagedServerStatus, ...]:
        with self._lock:
            if server_id is None:
                return tuple(self._runs[key].status for key in sorted(self._runs))
            validate_alias(server_id, "server")
            run = self._runs.get(server_id)
            if run is None:
                return ManagedServerStatus(server_id, None, LifecycleState.STOPPED)
            return run.status

    def _start_monitor(
        self,
        run: _ManagedServerRun,
        server_id: str,
        model_alias: str,
        upstream_url: str,
    ) -> None:
        stop = threading.Event()
        run.monitor_stop = stop
        monitor = threading.Thread(
            target=self._monitor,
            args=(run, server_id, model_alias, upstream_url, stop),
            name=f"mlxctl-monitor-{server_id}",
            daemon=True,
        )
        run.monitor = monitor
        monitor.start()

    def _monitor(
        self,
        run: _ManagedServerRun,
        server_id: str,
        model_alias: str,
        upstream_url: str,
        stop: threading.Event,
    ) -> None:
        interval = self._settings.metrics_interval_seconds
        while not stop.wait(interval):
            process = run.process
            if process is None:
                return
            return_code = process.poll()
            if return_code is not None:
                with self._lock:
                    stopping = run.stop_requested
                if not stopping:
                    self._fail(
                        run,
                        f"process exited with status {return_code}",
                        terminate=False,
                    )
                return
            try:
                if run.process_identity is not None and not _identity_matches(
                    run.process_identity, run.process_create_time
                ):
                    self._fail(run, "process identity changed", terminate=False)
                    return
                if run.process_identity is not None:
                    rss_bytes, cpu_percent = _process_sample(run.process_identity)
                    self._metrics.record(
                        ProcessSample(
                            server_id,
                            model_alias,
                            run.status.run_id,
                            datetime.now(UTC),
                            rss_bytes,
                            cpu_percent,
                        )
                    )
            except (OSError, RuntimeError):
                if process.poll() is not None:
                    continue
            lifecycle = LifecycleState.UNHEALTHY
            advertised: tuple[str, ...] | None = None
            live = False
            try:
                live = probe_liveness(
                    upstream_url, timeout_seconds=self._PROBE_TIMEOUT_SECONDS
                )
            except ProbeError:
                pass
            try:
                advertised = probe_readiness(
                    upstream_url, timeout_seconds=self._PROBE_TIMEOUT_SECONDS
                )
            except ProbeError:
                pass
            if live and advertised is not None:
                lifecycle = LifecycleState.READY
            with self._lock:
                if not run.stop_requested and run.status.lifecycle in {
                    LifecycleState.READY,
                    LifecycleState.UNHEALTHY,
                }:
                    run.status = replace(
                        run.status,
                        lifecycle=lifecycle,
                        advertised_models=(
                            advertised
                            if advertised is not None
                            else run.status.advertised_models
                        ),
                    )

    def _fail(
        self, run: _ManagedServerRun, message: str, *, terminate: bool
    ) -> ManagedServerStatus:
        with run.operation_lock:
            if run.monitor_stop is not None:
                run.monitor_stop.set()
            self._close_proxy(run)
            if terminate:
                self._terminate_child(run)
            self._close_log(run)
        with self._lock:
            if run.stop_requested:
                return run.status
            run.status = replace(
                run.status,
                lifecycle=LifecycleState.FAILED,
                upstream_endpoint=None,
                pid=None,
                advertised_models=(),
                error=message,
            )
            run.start_done.set()
            self._persist_locked()
            return run.status

    def _terminate_child(self, run: _ManagedServerRun) -> None:
        process = run.process
        if process is None or process.poll() is not None:
            return
        if not self._child_identity_matches(run):
            process.poll()
            return
        process.terminate()
        if not self._child_identity_matches(run):
            process.poll()
            return
        try:
            process.wait(timeout=self._settings.stop_timeout_seconds)
        except subprocess.TimeoutExpired:
            if not self._child_identity_matches(run):
                process.poll()
                return
            process.kill()
            try:
                process.wait(timeout=self._settings.stop_timeout_seconds)
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _child_identity_matches(run: _ManagedServerRun) -> bool:
        if run.process_identity is None:
            return True
        return _identity_matches(run.process_identity, run.process_create_time)

    def _join_monitor(self, run: _ManagedServerRun) -> None:
        monitor = run.monitor
        if monitor is None or monitor is threading.current_thread():
            return
        monitor.join(
            self._settings.stop_timeout_seconds + self._PROBE_TIMEOUT_SECONDS * 2 + 0.1
        )

    @staticmethod
    def _close_proxy(run: _ManagedServerRun) -> None:
        proxy = run.proxy
        run.proxy = None
        if proxy is not None:
            proxy.__exit__()

    @staticmethod
    def _close_log(run: _ManagedServerRun) -> None:
        stream = run.log_stream
        run.log_stream = None
        if stream is not None:
            stream.close()

    def _open_log(self, server_id: str):
        validate_alias(server_id, "server")
        path = self._log_dir / f"{server_id}.log"
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        os.chmod(path, 0o600)
        return os.fdopen(descriptor, "ab", buffering=0)

    @staticmethod
    def _allocate_upstream() -> Endpoint:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return Endpoint("127.0.0.1", listener.getsockname()[1])

    @staticmethod
    def _prepare_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)

    def _recover_orphans(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            if psutil is None:
                self._write_state({})
                return
            for identity in raw.values():
                if not isinstance(identity, dict):
                    continue
                pid = identity.get("pid")
                create_time = identity.get("create_time")
                if type(pid) is not int or not isinstance(create_time, (int, float)):
                    continue
                try:
                    process = psutil.Process(pid)
                    actual = process.create_time()
                except psutil.Error:
                    continue
                if _same_create_time(actual, float(create_time)):
                    _terminate_process_identity(
                        process,
                        float(create_time),
                        self._settings.stop_timeout_seconds,
                    )
        self._write_state({})

    def _persist_locked(self) -> None:
        active = {}
        for server_id, run in self._runs.items():
            if (
                run.process is not None
                and run.process.poll() is None
                and run.status.pid is not None
                and run.process_create_time is not None
                and run.status.lifecycle
                not in {LifecycleState.STOPPED, LifecycleState.FAILED}
            ):
                active[server_id] = {
                    "pid": run.status.pid,
                    "create_time": run.process_create_time,
                }
        self._write_state(active)

    def _write_state(self, value: dict[str, object]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".runtime-", suffix=".tmp", dir=self._state_dir
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._state_path)
            os.chmod(self._state_path, 0o600)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _same_create_time(first: float | None, second: float | None) -> bool:
    return first is not None and second is not None and abs(first - second) < 0.01


def _capture_process_identity(pid: int) -> tuple[object | None, float | None]:
    if psutil is None:
        return None, None
    try:
        process = psutil.Process(pid)
        return process, process.create_time()
    except psutil.Error:
        return None, None


def _identity_matches(process: object, create_time: float | None) -> bool:
    if psutil is None or create_time is None:
        return False
    try:
        return _same_create_time(process.create_time(), create_time)
    except psutil.Error:
        return False


def _process_sample(process: object) -> tuple[int, float]:
    try:
        return process.memory_info().rss, process.cpu_percent(interval=None)
    except psutil.Error as error:
        raise OSError(str(error)) from error


def _terminate_process_identity(
    process: object, create_time: float, timeout_seconds: float
) -> None:
    if not _identity_matches(process, create_time):
        return
    try:
        process.terminate()
    except psutil.Error:
        return
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _identity_matches(process, create_time):
            return
        remaining = deadline - time.monotonic()
        try:
            process.wait(timeout=min(remaining, 0.05))
            return
        except psutil.TimeoutExpired:
            pass
        except psutil.Error:
            return
    if not _identity_matches(process, create_time):
        return
    try:
        process.kill()
    except psutil.Error:
        return
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _identity_matches(process, create_time):
            return
        remaining = deadline - time.monotonic()
        try:
            process.wait(timeout=min(remaining, 0.05))
            return
        except psutil.TimeoutExpired:
            pass
        except psutil.Error:
            return
