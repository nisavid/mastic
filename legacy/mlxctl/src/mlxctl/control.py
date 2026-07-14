"""Expose daemon behavior through one versioned local control interface."""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from enum import Enum, StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from .adapters import Endpoint
from .config import ConfigError, load_config, validate_alias
from .metrics import MetricQuery, MetricsEngine, MetricSummary
from .supervisor import (
    GetModels,
    GetStatus,
    LifecycleState,
    ManagedServerStatus,
    StartServer,
    StopServer,
    Supervisor,
)


PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1024 * 1024
DEFAULT_IO_TIMEOUT_SECONDS = 5.0


class ControlCommand(StrEnum):
    START = "start"
    STOP = "stop"
    STATUS = "status"
    MODELS = "models"
    METRICS = "metrics"


_COMMANDS = frozenset(ControlCommand)
_REQUEST_FIELDS = frozenset(
    {"version", "command", "server_id", "model_alias", "start", "end"}
)


class ControlDomainError(RuntimeError):
    """A control request failed with a stable, user-facing error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ControlSocketError(RuntimeError):
    """The control socket cannot be safely created or used."""


@dataclass(frozen=True, slots=True)
class ControlRequest:
    """One immutable v1 command received by the control plane."""

    command: ControlCommand | str
    server_id: str | None = None
    model_alias: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        try:
            command = ControlCommand(self.command)
        except (TypeError, ValueError):
            return
        object.__setattr__(self, "command", command)


@dataclass(frozen=True, slots=True)
class ControlResult:
    """One immutable successful result returned by the control plane."""

    value: object

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _freeze(self.value))


class _ControlHandler(Protocol):
    def handle(self, request: ControlRequest) -> ControlResult: ...


class ControlPlane:
    """Adapt config reload, Supervisor commands, and metrics behind ``handle``."""

    def __init__(
        self,
        config_path: str | Path,
        supervisor: Supervisor,
        metrics_engine: MetricsEngine,
    ) -> None:
        self._config_path = Path(config_path)
        self._supervisor = supervisor
        self._metrics = metrics_engine

    def handle(self, request: ControlRequest) -> ControlResult:
        """Apply one validated control request."""
        _validate_request(request)
        try:
            if request.command == "start":
                return ControlResult(self._start(_required_server_id(request)))
            if request.command == "stop":
                status = self._supervisor.apply(
                    StopServer(_required_server_id(request))
                )
                return ControlResult(_status_value(status))
            if request.command == "status":
                return ControlResult(self._status(request.server_id))
            if request.command == "models":
                server_id = _required_server_id(request)
                models = self._supervisor.apply(GetModels(server_id))
                return ControlResult({"server_id": server_id, "models": models})
            if request.command == "metrics":
                summaries = self._metrics.query(
                    MetricQuery(
                        server_id=request.server_id,
                        model_alias=request.model_alias,
                        start_time=request.start,
                        end_time=request.end,
                    )
                )
                return ControlResult(
                    {"summaries": tuple(_metric_value(item) for item in summaries)}
                )
        except ControlDomainError:
            raise
        except ConfigError as error:
            raise ControlDomainError("invalid_request", str(error)) from error
        except (OSError, RuntimeError, ValueError) as error:
            raise ControlDomainError("command_failed", _safe_message(error)) from error
        raise ControlDomainError("unknown_command", "command is not supported")

    def _start(self, server_id: str) -> Mapping[str, object]:
        try:
            config = load_config(self._config_path)
        except ConfigError as error:
            raise ControlDomainError(
                "config_invalid", f"configuration is invalid: {_safe_message(error)}"
            ) from error
        try:
            server = config.servers[server_id]
        except KeyError as error:
            raise ControlDomainError(
                "server_not_found", f"server '{server_id}' is not configured"
            ) from error
        model = config.models[server.model]
        status = self._supervisor.apply(StartServer(server, model))
        return _status_value(status)

    def _status(self, server_id: str | None) -> Mapping[str, object]:
        config_error: str | None = None
        try:
            config = load_config(self._config_path)
        except ConfigError as error:
            config = None
            config_error = _safe_message(error)

        runtime = self._supervisor.apply(GetStatus(server_id))
        runtime_items = (
            (runtime,) if isinstance(runtime, ManagedServerStatus) else runtime
        )
        by_id = {item.server_id: item for item in runtime_items}
        if config is not None:
            configured_ids = (
                (server_id,)
                if server_id is not None and server_id in config.servers
                else tuple(config.servers)
                if server_id is None
                else ()
            )
            if (
                server_id is not None
                and not configured_ids
                and (server_id not in by_id or by_id[server_id].model_alias is None)
            ):
                raise ControlDomainError(
                    "server_not_found", f"server '{server_id}' is not configured"
                )
            for configured_id in configured_ids:
                if configured_id not in by_id:
                    definition = config.servers[configured_id]
                    by_id[configured_id] = ManagedServerStatus(
                        configured_id,
                        definition.model,
                        LifecycleState.STOPPED,
                        client_endpoint=Endpoint(definition.host, definition.port),
                    )
        result: dict[str, object] = {
            "servers": tuple(_status_value(by_id[key]) for key in sorted(by_id))
        }
        if config_error is not None:
            result["config_error"] = config_error
        return result


class UnixControlServer:
    """Serve one newline-delimited JSON request per Unix socket connection."""

    def __init__(
        self,
        path: str | Path,
        handler: _ControlHandler,
        *,
        io_timeout_seconds: float = DEFAULT_IO_TIMEOUT_SECONDS,
        activity_callback: Callable[[], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self._handler = handler
        self._io_timeout = io_timeout_seconds
        self._activity_callback = activity_callback or (lambda: None)
        self._lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._identity: tuple[int, int] | None = None
        self._thread: threading.Thread | None = None
        self._clients: set[threading.Thread] = set()
        self._connections: set[socket.socket] = set()
        self._closed = threading.Event()

    @property
    def has_active_clients(self) -> bool:
        """Return whether a control request is currently connected."""
        with self._lock:
            return bool(self._connections)

    def start(self) -> None:
        """Bind securely and start accepting concurrent clients."""
        with self._lock:
            if self._listener is not None:
                return
            if self._closed.is_set():
                raise RuntimeError("control server is closed")
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            self._prepare_socket_path()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(os.fspath(self.path))
                os.chmod(self.path, 0o600)
                listener.listen()
                listener.settimeout(0.2)
                details = os.lstat(self.path)
            except BaseException:
                listener.close()
                raise
            self._listener = listener
            self._identity = (details.st_dev, details.st_ino)
            self._thread = threading.Thread(
                target=self._accept_loop, name="mlxd-control", daemon=True
            )
            self._thread.start()

    def close(self) -> None:
        """Stop accepting and unlink only the socket inode this server bound."""
        self._closed.set()
        with self._lock:
            listener = self._listener
            self._listener = None
            accept_thread = self._thread
            identity = self._identity
        if listener is not None:
            listener.close()
        if (
            accept_thread is not None
            and accept_thread is not threading.current_thread()
        ):
            accept_thread.join(1)
        with self._lock:
            clients = tuple(self._clients)
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        deadline = time.monotonic() + min(self._io_timeout + 0.1, 1.0)
        for client in clients:
            if client is not threading.current_thread():
                client.join(max(0.0, deadline - time.monotonic()))
        if identity is not None:
            _unlink_owned_socket(self.path, identity)

    def _prepare_socket_path(self) -> None:
        try:
            details = os.lstat(self.path)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(details.st_mode):
            raise ControlSocketError("control socket path is occupied by a non-socket")
        identity = (details.st_dev, details.st_ino)
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(os.fspath(self.path))
        except ConnectionRefusedError:
            pass
        except OSError as error:
            raise ControlSocketError(
                "cannot safely probe the existing control socket"
            ) from error
        else:
            raise ControlSocketError("a control daemon is already listening")
        finally:
            probe.close()
        if not _unlink_owned_socket(self.path, identity):
            raise ControlSocketError(
                "control socket changed during stale-socket cleanup"
            )

    def _accept_loop(self) -> None:
        while not self._closed.is_set():
            with self._lock:
                listener = self._listener
            if listener is None:
                return
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._closed.is_set():
                    return
                continue
            self._activity_callback()
            thread = threading.Thread(
                target=self._serve_client,
                args=(connection,),
                name="mlxd-control-client",
                daemon=True,
            )
            with self._lock:
                self._clients.add(thread)
                self._connections.add(connection)
            thread.start()

    def _serve_client(self, connection: socket.socket) -> None:
        try:
            connection.settimeout(self._io_timeout)
            response = self._dispatch_connection(connection)
            encoded = _encode_response(response)
            connection.sendall(encoded)
        except (OSError, TimeoutError):
            pass
        finally:
            connection.close()
            with self._lock:
                self._clients.discard(threading.current_thread())
                self._connections.discard(connection)

    def _dispatch_connection(self, connection: socket.socket) -> dict[str, object]:
        try:
            raw = _read_line(connection, MAX_REQUEST_BYTES)
            request = _decode_request(raw)
            result = self._handler.handle(request)
            self._activity_callback()
            return {
                "version": PROTOCOL_VERSION,
                "ok": True,
                "result": _thaw(result.value),
            }
        except ControlDomainError as error:
            return _error_response(error.code, error.message)
        except Exception:
            return _error_response(
                "internal_error", "the control daemon could not complete the request"
            )


class ControlClient:
    """Send immutable requests to the local daemon control interface."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout_seconds: float = DEFAULT_IO_TIMEOUT_SECONDS,
    ) -> None:
        self.path = Path(path)
        self._timeout = timeout_seconds

    def send(self, request: ControlRequest) -> ControlResult:
        _validate_request(request)
        payload = _encode_request(request)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self._timeout)
            connection.connect(os.fspath(self.path))
            connection.sendall(payload)
            raw = _read_line(connection, MAX_REQUEST_BYTES)
        try:
            response = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ControlDomainError(
                "invalid_response", "control daemon returned malformed JSON"
            ) from error
        if (
            not isinstance(response, dict)
            or response.get("version") != PROTOCOL_VERSION
        ):
            raise ControlDomainError(
                "invalid_response", "control daemon returned an unsupported response"
            )
        if response.get("ok") is True and "result" in response:
            return ControlResult(response["result"])
        error_value = response.get("error")
        if response.get("ok") is False and isinstance(error_value, dict):
            code = error_value.get("code")
            message = error_value.get("message")
            if isinstance(code, str) and isinstance(message, str):
                raise ControlDomainError(code, message)
        raise ControlDomainError(
            "invalid_response", "control daemon returned an invalid response"
        )


def _decode_request(raw: bytes) -> ControlRequest:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlDomainError(
            "malformed_request", "request must be one JSON object"
        ) from error
    if not isinstance(value, dict):
        raise ControlDomainError("malformed_request", "request must be a JSON object")
    unknown = value.keys() - _REQUEST_FIELDS
    if unknown:
        raise ControlDomainError(
            "invalid_request", f"request field '{sorted(unknown)[0]}' is not supported"
        )
    version = value.get("version")
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise ControlDomainError("unsupported_version", "request version must be 1")
    command = value.get("command")
    if not isinstance(command, str):
        raise ControlDomainError("invalid_request", "request command must be a string")
    if command not in _COMMANDS:
        raise ControlDomainError(
            "unknown_command", f"command '{command}' is not supported"
        )
    request = ControlRequest(
        version=version,
        command=command,
        server_id=_optional_string(value, "server_id"),
        model_alias=_optional_string(value, "model_alias"),
        start=_optional_datetime(value, "start"),
        end=_optional_datetime(value, "end"),
    )
    _validate_request(request)
    return request


def _validate_request(request: ControlRequest) -> None:
    if type(request.version) is not int or request.version != PROTOCOL_VERSION:
        raise ControlDomainError("unsupported_version", "request version must be 1")
    if not isinstance(request.command, str):
        raise ControlDomainError("invalid_request", "request command must be a string")
    if request.command not in _COMMANDS:
        raise ControlDomainError(
            "unknown_command", f"command '{request.command}' is not supported"
        )
    if request.server_id is not None:
        try:
            validate_alias(request.server_id, "server")
        except ConfigError as error:
            raise ControlDomainError("invalid_request", str(error)) from error
    if request.model_alias is not None:
        try:
            validate_alias(request.model_alias, "model")
        except ConfigError as error:
            raise ControlDomainError("invalid_request", str(error)) from error
    if request.command in {"start", "stop", "models"} and request.server_id is None:
        raise ControlDomainError(
            "invalid_request", f"{request.command} requires server_id"
        )
    if request.command != "metrics" and any(
        value is not None for value in (request.model_alias, request.start, request.end)
    ):
        raise ControlDomainError(
            "invalid_request", f"{request.command} does not accept metric filters"
        )
    for field_name, value in (("start", request.start), ("end", request.end)):
        if value is not None and not isinstance(value, datetime):
            raise ControlDomainError(
                "invalid_request", f"metrics {field_name} must be a timestamp"
            )
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ControlDomainError(
                "invalid_request",
                f"metrics {field_name} timestamp must include a timezone",
            )
    if (
        request.start is not None
        and request.end is not None
        and request.start >= request.end
    ):
        raise ControlDomainError("invalid_request", "metrics start must be before end")


def _required_server_id(request: ControlRequest) -> str:
    if request.server_id is None:  # guarded by validation; keeps the type narrow
        raise ControlDomainError(
            "invalid_request", f"{request.command} requires server_id"
        )
    return request.server_id


def _optional_string(value: Mapping[str, object], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str):
        raise ControlDomainError("invalid_request", f"request {key} must be a string")
    return item


def _optional_datetime(value: Mapping[str, object], key: str) -> datetime | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str):
        raise ControlDomainError(
            "invalid_request", f"request {key} must be a timestamp"
        )
    return parse_timestamp(item, field_name=f"request {key}")


def parse_timestamp(value: str, *, field_name: str = "timestamp") -> datetime:
    """Parse one timezone-aware ISO 8601 value into canonical UTC."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ControlDomainError(
            "invalid_request", f"{field_name} must be an ISO 8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ControlDomainError(
            "invalid_request", f"{field_name} must include a timezone"
        )
    return parsed.astimezone(UTC)


def _encode_request(request: ControlRequest) -> bytes:
    value: dict[str, object] = {
        "version": request.version,
        "command": request.command,
    }
    for key in ("server_id", "model_alias"):
        item = getattr(request, key)
        if item is not None:
            value[key] = item
    for key in ("start", "end"):
        item = getattr(request, key)
        if item is not None:
            value[key] = _datetime_value(item)
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _encode_response(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _error_response(code: str, message: str) -> dict[str, object]:
    return {
        "version": PROTOCOL_VERSION,
        "ok": False,
        "error": {"code": code, "message": _safe_protocol_text(message)},
    }


def _read_line(connection: socket.socket, maximum: int) -> bytes:
    value = bytearray()
    while True:
        remaining = maximum + 1 - len(value)
        if remaining <= 0:
            raise ControlDomainError(
                "request_too_large", f"request exceeds {maximum} bytes"
            )
        chunk = connection.recv(min(65536, remaining))
        if not chunk:
            raise ControlDomainError(
                "malformed_request", "request must end with a newline"
            )
        newline = chunk.find(b"\n")
        if newline >= 0:
            value.extend(chunk[:newline])
            if len(value) > maximum:
                raise ControlDomainError(
                    "request_too_large", f"request exceeds {maximum} bytes"
                )
            return bytes(value)
        value.extend(chunk)


def _status_value(status: ManagedServerStatus) -> dict[str, object]:
    return {
        "server_id": status.server_id,
        "model_alias": status.model_alias,
        "lifecycle": status.lifecycle.value,
        "client_endpoint": _endpoint_value(status.client_endpoint),
        "upstream_endpoint": _endpoint_value(status.upstream_endpoint),
        "run_id": status.run_id,
        "pid": status.pid,
        "advertised_models": status.advertised_models,
        "error": (
            _safe_protocol_text(status.error) if status.error is not None else None
        ),
    }


def _endpoint_value(endpoint: Endpoint | None) -> dict[str, object] | None:
    if endpoint is None:
        return None
    return {"host": endpoint.host, "port": endpoint.port}


def _metric_value(summary: MetricSummary) -> dict[str, object]:
    return {field.name: getattr(summary, field.name) for field in fields(summary)}


def _datetime_value(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ControlDomainError(
            "invalid_request", "metric timestamps must include a timezone"
        )
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _datetime_value(value)
    return value


def _safe_message(error: BaseException) -> str:
    message = str(error).replace("\n", " ").replace("\r", " ")
    if not message:
        return type(error).__name__
    return _safe_protocol_text(message)


def _safe_protocol_text(message: str) -> str:
    # Filesystem exceptions may include operator-local absolute paths.  The
    # protocol only needs a stable domain explanation, never those details.
    message = re.sub(
        r"(?<=['\"])/[^'\"]+|(?<![\w'\"])/[^\s'\"]+",
        "<path>",
        message,
    )
    words = message.replace("\n", " ").replace("\r", " ").split()
    sanitized = ["<path>" if word.startswith("/") else word for word in words]
    return " ".join(sanitized)[:1024]


def _unlink_owned_socket(path: Path, identity: tuple[int, int]) -> bool:
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return True
    if (
        not stat.S_ISSOCK(details.st_mode)
        or (details.st_dev, details.st_ino) != identity
    ):
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    return True
