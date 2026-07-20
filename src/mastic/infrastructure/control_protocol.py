"""Versioned local control protocol for the mastic Supervisor."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import socket
import stat
import struct
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROTOCOL_NAME = "mastic.control"
PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1024 * 1024

JsonObject = dict[str, Any]
ProgressEmitter = Callable[[Mapping[str, Any]], Awaitable[None]]


class ControlProtocolError(Exception):
    """A stable control-protocol failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ControlSocketError(ControlProtocolError):
    """A failure to create a safe local control socket."""


@dataclass(frozen=True, slots=True)
class ControlRequest:
    request_id: str
    operation_id: str
    operation: str
    parameters: JsonObject


async def read_message(
    reader: asyncio.StreamReader, *, max_frame_bytes: int = MAX_FRAME_BYTES
) -> JsonObject:
    """Read one length-prefixed JSON object from a control connection."""

    try:
        header = await reader.readexactly(4)
    except asyncio.IncompleteReadError as error:
        raise ControlProtocolError(
            "connection_closed", "The control connection closed."
        ) from error
    length = struct.unpack("!I", header)[0]
    if length == 0:
        raise ControlProtocolError(
            "malformed_frame", "A control frame cannot be empty."
        )
    if length > max_frame_bytes:
        raise ControlProtocolError(
            "frame_too_large",
            f"The control frame exceeds the {max_frame_bytes}-byte limit.",
        )
    try:
        payload = await reader.readexactly(length)
    except asyncio.IncompleteReadError as error:
        raise ControlProtocolError(
            "malformed_frame", "The control frame is incomplete."
        ) from error
    try:
        message = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlProtocolError(
            "malformed_frame", "The control frame is not valid JSON."
        ) from error
    if not isinstance(message, dict):
        raise ControlProtocolError(
            "invalid_message", "A control message must be a JSON object."
        )
    return message


async def write_message(
    writer: asyncio.StreamWriter,
    message: Mapping[str, Any],
    *,
    max_frame_bytes: int = MAX_FRAME_BYTES,
) -> None:
    """Write one bounded length-prefixed JSON object."""

    try:
        payload = json.dumps(
            message, separators=(",", ":"), ensure_ascii=False
        ).encode()
    except (TypeError, ValueError) as error:
        raise ControlProtocolError(
            "invalid_message", "The control message is not JSON serializable."
        ) from error
    if not payload or len(payload) > max_frame_bytes:
        raise ControlProtocolError(
            "frame_too_large",
            f"The control frame exceeds the {max_frame_bytes}-byte limit.",
        )
    writer.write(struct.pack("!I", len(payload)) + payload)
    await writer.drain()


def resolve_peer_uid(peer_socket: socket.socket) -> int | None:
    """Return the authenticated peer UID when the host exposes it."""

    if hasattr(socket, "SO_PEERCRED"):
        credentials = peer_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _, uid, _ = struct.unpack("3i", credentials)
        return uid
    if hasattr(socket, "LOCAL_PEERCRED"):
        # Darwin's xucred begins with cr_version and cr_uid. SOL_LOCAL is 0.
        credentials = peer_socket.getsockopt(0, socket.LOCAL_PEERCRED, 8)
        _, uid = struct.unpack("=II", credentials[:8])
        return uid
    return None


class UnixControlServer:
    """Mode-0600 Unix-socket server for control protocol version 1."""

    def __init__(
        self,
        socket_path: str | Path,
        handler: Callable[
            [ControlRequest, ProgressEmitter], Awaitable[Mapping[str, Any]]
        ],
        *,
        cancel_handler: Callable[[str], bool | Awaitable[bool]] | None = None,
        supported_versions: tuple[int, ...] = (PROTOCOL_VERSION,),
        max_frame_bytes: int = MAX_FRAME_BYTES,
        expected_uid: int | None = None,
        peer_uid_resolver: Callable[[socket.socket], int | None] = resolve_peer_uid,
        connection_drain_timeout: float = 10.0,
        connection_idle_timeout: float = 30.0,
        max_connections: int = 64,
    ) -> None:
        if connection_drain_timeout <= 0:
            raise ValueError("connection drain timeout must be positive")
        if connection_idle_timeout <= 0:
            raise ValueError("connection idle timeout must be positive")
        if type(max_connections) is not int or max_connections <= 0:
            raise ValueError("maximum control connections must be a positive integer")
        if supported_versions != (PROTOCOL_VERSION,):
            raise ValueError("only control protocol version 1 is implemented")
        self.socket_path = Path(socket_path)
        self._handler = handler
        self._cancel_handler = cancel_handler
        self._supported_versions = supported_versions
        self._max_frame_bytes = max_frame_bytes
        self._expected_uid = os.getuid() if expected_uid is None else expected_uid
        self._peer_uid_resolver = peer_uid_resolver
        self._connection_drain_timeout = connection_drain_timeout
        self._connection_idle_timeout = connection_idle_timeout
        self._max_connections = max_connections
        self._server: asyncio.AbstractServer | None = None
        self._bound_socket_identity: tuple[int, int] | None = None
        self._connections: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """Bind the control socket without replacing an unsafe path."""

        if self._server is not None:
            return
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = self.socket_path.parent.stat()
        if directory.st_uid != os.getuid() or directory.st_mode & 0o022:
            raise ControlSocketError(
                "unsafe_socket_directory",
                "The control socket directory must be user-owned and not group- or world-writable.",
            )
        await self._prepare_socket_path()
        try:
            server = await asyncio.start_unix_server(
                self._accept,
                path=self.socket_path,
            )
            self._server = server
            os.chmod(self.socket_path, 0o600, follow_symlinks=False)
            metadata = self.socket_path.lstat()
            self._bound_socket_identity = (metadata.st_dev, metadata.st_ino)
        except BaseException:
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
                self._server = None
                self._unlink_our_socket()
            raise

    async def close(self) -> None:
        """Stop accepting connections and remove this server's socket."""

        server = self._server
        if server is not None:
            self._server = None
            server.close()
        await asyncio.sleep(0)
        connections = tuple(self._connections)
        if connections:
            done, pending = await asyncio.wait(
                connections, timeout=self._connection_drain_timeout
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            del done
        if server is not None:
            await server.wait_closed()
        self._unlink_our_socket()

    async def _prepare_socket_path(self) -> None:
        try:
            metadata = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ControlSocketError(
                "unsafe_socket_path",
                "Refusing to replace a path that is not a user-owned Unix socket.",
            )
        try:
            reader, writer = await asyncio.open_unix_connection(self.socket_path)
        except (ConnectionRefusedError, FileNotFoundError):
            self.socket_path.unlink(missing_ok=True)
            return
        writer.close()
        await writer.wait_closed()
        del reader
        raise ControlSocketError(
            "socket_in_use", "Another Supervisor is using the control socket."
        )

    def _unlink_our_socket(self) -> None:
        identity = self._bound_socket_identity
        if identity is None:
            return
        try:
            metadata = self.socket_path.lstat()
        except FileNotFoundError:
            self._bound_socket_identity = None
            return
        if (
            stat.S_ISSOCK(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and (metadata.st_dev, metadata.st_ino) == identity
        ):
            self.socket_path.unlink(missing_ok=True)
        self._bound_socket_identity = None

    async def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        connection = asyncio.current_task()
        if len(self._connections) >= self._max_connections:
            await write_message(
                writer,
                self._error(
                    "connection_limit",
                    "The control server has reached its connection limit.",
                ),
                max_frame_bytes=self._max_frame_bytes,
            )
            writer.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await writer.wait_closed()
            return
        if connection is not None:
            self._connections.add(connection)
        send_lock = asyncio.Lock()
        tasks: set[asyncio.Task[None]] = set()

        async def send(message: Mapping[str, Any]) -> None:
            async with send_lock:
                await write_message(
                    writer, message, max_frame_bytes=self._max_frame_bytes
                )

        try:
            peer_socket = writer.get_extra_info("socket")
            peer_uid = (
                self._peer_uid_resolver(peer_socket)
                if peer_socket is not None
                else None
            )
            if peer_uid != self._expected_uid:
                await send(
                    self._error(
                        "peer_not_authorized",
                        "The control peer identity is unavailable or belongs to a different user.",
                    )
                )
                return

            negotiated = await self._negotiate(reader, send)
            if not negotiated:
                return
            while True:
                try:
                    message = await self._read_message(reader)
                except ControlProtocolError as error:
                    if error.code == "connection_timeout" and tasks:
                        continue
                    if error.code != "connection_closed":
                        await send(self._error(error.code, error.message))
                    break
                try:
                    request = self._parse_request(message)
                except ControlProtocolError as error:
                    await send(
                        self._error(
                            error.code,
                            error.message,
                            request_id=_string(message.get("request_id")),
                            operation_id=_string(message.get("operation_id")),
                        )
                    )
                    continue
                if message["type"] == "cancel":
                    task = asyncio.create_task(self._handle_cancel(request, send))
                else:
                    task = asyncio.create_task(self._handle_request(request, send))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except (BrokenPipeError, ConnectionResetError):
            # A peer disconnect is the normal terminal state for this handler.
            return
        finally:
            if connection is not None and connection.cancelling():
                for task in tasks:
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await writer.wait_closed()
            if connection is not None:
                self._connections.discard(connection)

    async def _negotiate(self, reader: asyncio.StreamReader, send) -> bool:
        try:
            message = await self._read_message(reader)
        except ControlProtocolError as error:
            if error.code != "connection_closed":
                await send(self._error(error.code, error.message))
            return False
        request_id = _string(message.get("request_id"))
        if (
            message.get("type") != "negotiate"
            or message.get("protocol") != PROTOCOL_NAME
        ):
            await send(
                self._error(
                    "unsupported_protocol",
                    f"Negotiate {PROTOCOL_NAME!r} before sending operations.",
                    request_id=request_id,
                )
            )
            return False
        versions = message.get("supported_versions")
        if not isinstance(versions, list) or not all(
            isinstance(item, int) for item in versions
        ):
            await send(
                self._error(
                    "invalid_message",
                    "supported_versions must be an integer list.",
                    request_id=request_id,
                )
            )
            return False
        compatible = sorted(
            set(versions).intersection(self._supported_versions), reverse=True
        )
        if not compatible:
            await send(
                self._error(
                    "unsupported_version",
                    f"Supported control protocol versions: {list(self._supported_versions)}.",
                    request_id=request_id,
                )
            )
            return False
        await send(
            {
                "type": "negotiated",
                "protocol": PROTOCOL_NAME,
                "version": compatible[0],
                "request_id": request_id,
            }
        )
        return True

    async def _read_message(self, reader: asyncio.StreamReader) -> JsonObject:
        try:
            return await asyncio.wait_for(
                read_message(reader, max_frame_bytes=self._max_frame_bytes),
                timeout=self._connection_idle_timeout,
            )
        except TimeoutError as error:
            raise ControlProtocolError(
                "connection_timeout", "The control connection was idle for too long."
            ) from error

    def _parse_request(self, message: JsonObject) -> tuple[str, ControlRequest]:
        if (
            message.get("protocol") != PROTOCOL_NAME
            or message.get("version") not in self._supported_versions
        ):
            raise ControlProtocolError(
                "unsupported_version", "The negotiated protocol version is required."
            )
        message_type = message.get("type")
        if message_type not in {"request", "cancel"}:
            raise ControlProtocolError(
                "invalid_message", "Expected a request or cancel message."
            )
        request_id = _required_string(message, "request_id")
        if message_type == "cancel":
            operation_id = _required_string(message, "operation_id")
            return message_type, ControlRequest(
                request_id, operation_id, "operation.cancel", {}
            )
        operation_id = _string(message.get("operation_id")) or str(uuid.uuid4())
        operation = _required_string(message, "operation")
        parameters = message.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ControlProtocolError(
                "invalid_message", "parameters must be a JSON object."
            )
        return message_type, ControlRequest(
            request_id, operation_id, operation, parameters
        )

    async def _handle_request(self, parsed, send) -> None:
        _, request = parsed
        sequence = 0

        async def emit(progress: Mapping[str, Any]) -> None:
            nonlocal sequence
            sequence += 1
            await send(
                self._envelope(
                    "progress", request, sequence=sequence, progress=dict(progress)
                )
            )

        try:
            result = await self._handler(request, emit)
            await send(self._envelope("result", request, result=dict(result)))
        except ControlProtocolError as error:
            await send(
                self._error(
                    error.code,
                    error.message,
                    request_id=request.request_id,
                    operation_id=request.operation_id,
                )
            )
        except Exception:
            await send(
                self._error(
                    "internal_error",
                    "The Supervisor could not complete the operation.",
                    request_id=request.request_id,
                    operation_id=request.operation_id,
                )
            )

    async def _handle_cancel(self, parsed, send) -> None:
        _, request = parsed
        accepted = False
        if self._cancel_handler is not None:
            accepted_value = self._cancel_handler(request.operation_id)
            accepted = (
                await accepted_value
                if inspect.isawaitable(accepted_value)
                else accepted_value
            )
        await send(
            self._envelope(
                "result", request, result={"cancel_requested": bool(accepted)}
            )
        )

    @staticmethod
    def _envelope(
        message_type: str, request: ControlRequest, **payload: Any
    ) -> JsonObject:
        return {
            "type": message_type,
            "protocol": PROTOCOL_NAME,
            "version": PROTOCOL_VERSION,
            "request_id": request.request_id,
            "operation_id": request.operation_id,
            **payload,
        }

    @staticmethod
    def _error(
        code: str,
        message: str,
        *,
        request_id: str = "unknown",
        operation_id: str | None = None,
    ) -> JsonObject:
        envelope: JsonObject = {
            "type": "error",
            "protocol": PROTOCOL_NAME,
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "error": {"code": code, "message": message},
        }
        if operation_id:
            envelope["operation_id"] = operation_id
        return envelope


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _required_string(message: Mapping[str, Any], field: str) -> str:
    value = _string(message.get(field))
    if not value:
        raise ControlProtocolError(
            "invalid_message", f"{field} must be a non-empty string."
        )
    return value
