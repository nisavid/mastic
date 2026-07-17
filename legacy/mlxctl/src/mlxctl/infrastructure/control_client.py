"""Bounded clients for the Supervisor's versioned Unix control protocol."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .control_protocol import (
    MAX_FRAME_BYTES,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    ControlProtocolError,
    read_message,
    write_message,
)

JsonObject = dict[str, Any]


class ControlClientError(RuntimeError):
    """A stable failure raised by a Supervisor control client."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SupervisorUnavailableError(ControlClientError):
    """The Supervisor does not have a control socket to connect to."""


class ControlConnectionError(ControlClientError):
    """The Supervisor control connection failed outside the protocol."""


class ControlProtocolFailure(ControlClientError):
    """The Supervisor exchanged an invalid control-protocol message."""


class RemoteControlError(ControlClientError):
    """The Supervisor returned a stable operation or protocol error."""


@dataclass(frozen=True, slots=True)
class ControlResponse:
    """A completed operation and its correlated progress events."""

    request_id: str
    operation_id: str
    progress: tuple[Mapping[str, Any], ...]
    result: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "progress",
            tuple(MappingProxyType(dict(event)) for event in self.progress),
        )
        object.__setattr__(self, "result", MappingProxyType(dict(self.result)))


class AsyncUnixControlClient:
    """Execute operations over one bounded asynchronous Unix connection."""

    def __init__(
        self,
        socket_path: str | Path,
        *,
        timeout_seconds: float = 30.0,
        max_frame_bytes: int = MAX_FRAME_BYTES,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_frame_bytes <= 0:
            raise ValueError("max_frame_bytes must be positive")
        self.socket_path = Path(socket_path)
        self._timeout_seconds = timeout_seconds
        self._max_frame_bytes = max_frame_bytes

    async def execute(
        self,
        operation: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        request_id: str | None = None,
        operation_id: str | None = None,
    ) -> ControlResponse:
        """Negotiate v1, execute one operation, and collect its progress."""

        operation = _required_client_string(operation, "operation")
        request_id = _client_identifier(request_id, "request_id")
        operation_id = _client_identifier(operation_id, "operation_id")
        if parameters is not None and not isinstance(parameters, Mapping):
            raise ControlClientError("invalid_request", "parameters must be a mapping.")
        return await self._exchange(
            {
                "type": "request",
                "protocol": PROTOCOL_NAME,
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "operation_id": operation_id,
                "operation": operation,
                "parameters": dict(parameters or {}),
            },
            request_id,
            operation_id,
        )

    async def cancel(
        self, operation_id: str, *, request_id: str | None = None
    ) -> ControlResponse:
        """Ask the Supervisor to cancel one operation."""

        operation_id = _required_client_string(operation_id, "operation_id")
        request_id = _client_identifier(request_id, "request_id")
        return await self._exchange(
            {
                "type": "cancel",
                "protocol": PROTOCOL_NAME,
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "operation_id": operation_id,
            },
            request_id,
            operation_id,
        )

    async def _exchange(
        self,
        request: Mapping[str, Any],
        request_id: str,
        operation_id: str,
    ) -> ControlResponse:
        negotiation_id = str(uuid.uuid4())
        reader: asyncio.StreamReader
        writer: asyncio.StreamWriter
        try:
            async with asyncio.timeout(self._timeout_seconds):
                reader, writer = await asyncio.open_unix_connection(self.socket_path)
                try:
                    await write_message(
                        writer,
                        {
                            "type": "negotiate",
                            "protocol": PROTOCOL_NAME,
                            "supported_versions": [PROTOCOL_VERSION],
                            "request_id": negotiation_id,
                        },
                        max_frame_bytes=self._max_frame_bytes,
                    )
                    negotiated = await read_message(
                        reader, max_frame_bytes=self._max_frame_bytes
                    )
                    self._validate_negotiated(negotiated, negotiation_id)
                    await write_message(
                        writer, request, max_frame_bytes=self._max_frame_bytes
                    )
                    return await self._collect(reader, request_id, operation_id)
                finally:
                    writer.close()
                    with suppress(OSError):
                        await writer.wait_closed()
        except ControlClientError:
            raise
        except FileNotFoundError as error:
            raise SupervisorUnavailableError(
                "supervisor_unavailable",
                "The Supervisor is not running; its control socket is missing.",
            ) from error
        except TimeoutError as error:
            raise ControlConnectionError(
                "control_timeout",
                f"The Supervisor control exchange exceeded {self._timeout_seconds:g} seconds.",
            ) from error
        except ControlProtocolError as error:
            raise ControlProtocolFailure(error.code, error.message) from error
        except OSError as error:
            raise ControlConnectionError(
                "connection_failed",
                "The Supervisor control connection failed.",
            ) from error

    @staticmethod
    def _validate_negotiated(message: JsonObject, request_id: str) -> None:
        if message.get("type") == "error":
            raise _remote_error(message)
        if message != {
            "type": "negotiated",
            "protocol": PROTOCOL_NAME,
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
        }:
            raise ControlProtocolFailure(
                "invalid_response",
                "The Supervisor returned an invalid protocol negotiation response.",
            )

    async def _collect(
        self,
        reader: asyncio.StreamReader,
        request_id: str,
        operation_id: str,
    ) -> ControlResponse:
        progress: list[Mapping[str, Any]] = []
        expected_sequence = 1
        while True:
            message = await read_message(reader, max_frame_bytes=self._max_frame_bytes)
            _validate_correlation(message, request_id, operation_id)
            message_type = message.get("type")
            if message_type == "error":
                raise _remote_error(message)
            if message_type == "progress":
                event = message.get("progress")
                if message.get("sequence") != expected_sequence or not isinstance(
                    event, dict
                ):
                    raise ControlProtocolFailure(
                        "invalid_progress",
                        "The Supervisor returned invalid or out-of-order progress.",
                    )
                progress.append(event)
                expected_sequence += 1
                continue
            if message_type == "result" and isinstance(message.get("result"), dict):
                return ControlResponse(
                    request_id,
                    operation_id,
                    tuple(progress),
                    message["result"],
                )
            raise ControlProtocolFailure(
                "invalid_response",
                "The Supervisor returned an invalid operation response.",
            )


class UnixControlClient:
    """Synchronous facade for command-line and local application code."""

    def __init__(
        self,
        socket_path: str | Path,
        *,
        timeout_seconds: float = 30.0,
        max_frame_bytes: int = MAX_FRAME_BYTES,
    ) -> None:
        self._client = AsyncUnixControlClient(
            socket_path,
            timeout_seconds=timeout_seconds,
            max_frame_bytes=max_frame_bytes,
        )

    def execute(
        self,
        operation: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        request_id: str | None = None,
        operation_id: str | None = None,
    ) -> ControlResponse:
        """Execute one operation from synchronous application code."""

        return asyncio.run(
            self._client.execute(
                operation,
                parameters,
                request_id=request_id,
                operation_id=operation_id,
            )
        )

    def cancel(
        self, operation_id: str, *, request_id: str | None = None
    ) -> ControlResponse:
        """Ask the Supervisor to cancel one operation."""

        return asyncio.run(self._client.cancel(operation_id, request_id=request_id))


def _validate_correlation(
    message: Mapping[str, Any], request_id: str, operation_id: str
) -> None:
    if (
        message.get("protocol") != PROTOCOL_NAME
        or message.get("version") != PROTOCOL_VERSION
        or message.get("request_id") != request_id
        or message.get("operation_id") != operation_id
    ):
        raise ControlProtocolFailure(
            "invalid_response",
            "The Supervisor returned an uncorrelated control response.",
        )


def _client_identifier(value: str | None, field: str) -> str:
    if value is None:
        return str(uuid.uuid4())
    return _required_client_string(value, field)


def _required_client_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ControlClientError(
            "invalid_request", f"{field} must be a non-empty string."
        )
    return value


def _remote_error(message: Mapping[str, Any]) -> RemoteControlError:
    error = message.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        detail = error.get("message")
        if isinstance(code, str) and code and isinstance(detail, str) and detail:
            return RemoteControlError(code, detail)
    return RemoteControlError(
        "invalid_response", "The Supervisor returned an invalid error response."
    )
