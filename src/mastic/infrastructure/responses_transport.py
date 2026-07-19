"""Bounded HTTP transport adaptation for Responses protocol payloads."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from mastic.infrastructure.responses_adapter import (
    DELIMITER,
    CodexNamespaceAdapter,
    ReconstructionMap,
    TransformationTooLarge,
)


_SSE_INSPECTION_LIMIT = 1024 * 1024
_TERMINAL_RESPONSE_EVENTS = frozenset(
    {"response.completed", "response.failed", "response.incomplete"}
)


@dataclass(slots=True)
class _ResponsesSSEState:
    response_id: str | None = None
    next_sequence_number: int = 0

    def observe(self, frame: bytes) -> None:
        payload = _sse_json_payload(frame)
        if not isinstance(payload, dict):
            return
        sequence_number = payload.get("sequence_number")
        if type(sequence_number) is int:
            self.next_sequence_number = max(
                self.next_sequence_number, sequence_number + 1
            )
        response = payload.get("response")
        if isinstance(response, dict):
            response_id = response.get("id")
            if isinstance(response_id, str) and response_id:
                self.response_id = response_id
        response_id = payload.get("response_id")
        if isinstance(response_id, str) and response_id:
            self.response_id = response_id


class RequestTransformationTooLarge(RuntimeError):
    """A transformed Responses request would exceed its transport budget."""


class ResponseAdaptationTooLarge(RuntimeError):
    """A Responses body exceeded the memory allowed for safe adaptation."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"response adaptation exceeded the {limit}-byte limit")


class ResponsesTransport:
    """Own one service's namespace state and bounded Responses adaptation."""

    def __init__(self, *, max_request_bytes: int, max_response_bytes: int) -> None:
        if max_request_bytes <= 0 or max_response_bytes <= 0:
            raise ValueError("Responses transport limits must be positive")
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        self._adapter = CodexNamespaceAdapter()

    def transform_request(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], ReconstructionMap]:
        _enforce_transform_budget(payload, self._max_request_bytes)
        return self._adapter.transform_request(payload)

    async def adapt_response_body(
        self,
        upstream: httpx.Response,
        reconstruction: ReconstructionMap,
        *,
        content_type: str,
    ) -> bytes:
        body = bytearray()
        async for chunk in upstream.aiter_bytes():
            if len(body) + len(chunk) > self._max_response_bytes:
                raise ResponseAdaptationTooLarge(self._max_response_bytes)
            body.extend(chunk)
        raw = bytes(body)
        if "json" not in content_type or not raw:
            return raw
        try:
            original = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return raw
        try:
            return self._adapter.transform_response_body(
                original,
                raw,
                reconstruction,
                max_bytes=self._max_response_bytes,
            )
        except TransformationTooLarge as error:
            raise ResponseAdaptationTooLarge(self._max_response_bytes) from error

    async def adapt_sse_response(
        self,
        upstream: httpx.Response,
        reconstruction: ReconstructionMap,
    ) -> AsyncIterator[bytes]:
        frames = _iter_bounded_sse_frames(
            upstream, self._max_response_bytes
        ).__aiter__()
        try:
            first_raw, complete = await anext(frames)
        except StopAsyncIteration:
            return _empty_chunks()
        if not complete:
            return _single_chunk(first_raw)
        first = _adapt_sse_frame(
            first_raw,
            self._adapter,
            reconstruction,
            self._max_response_bytes,
        )
        state = _ResponsesSSEState()
        state.observe(first_raw)
        return _iter_adapted_sse_after_first(
            first,
            first_raw,
            frames,
            self._adapter,
            reconstruction,
            self._max_response_bytes,
            state,
        )


async def iter_sse_until_terminal(
    upstream: httpx.Response,
) -> AsyncIterator[bytes]:
    """Stop an SSE transport after its protocol-level terminal event."""

    pending = bytearray()
    async for chunk in upstream.aiter_raw():
        prior_pending_bytes = len(pending)
        pending.extend(chunk)
        consumed_bytes = 0
        while True:
            before = len(pending)
            event = _pop_sse_message(pending, include_separator=False)
            if event is None:
                break
            consumed_bytes += before - len(pending)
            if _is_terminal_sse_event(event):
                terminal_bytes = consumed_bytes - prior_pending_bytes
                if terminal_bytes > 0:
                    yield chunk[:terminal_bytes]
                return
        yield chunk
        if len(pending) > _SSE_INSPECTION_LIMIT:
            del pending[:-_SSE_INSPECTION_LIMIT]


async def _iter_bounded_sse_frames(
    upstream: httpx.Response, limit: int
) -> AsyncIterator[tuple[bytes, bool]]:
    pending = bytearray()
    async for chunk in upstream.aiter_bytes():
        view = memoryview(chunk)
        offset = 0
        while offset < len(view):
            available = limit - len(pending)
            if available <= 0:
                raise ResponseAdaptationTooLarge(limit)
            take = min(available, len(view) - offset)
            pending.extend(view[offset : offset + take])
            offset += take
            while (
                frame := _pop_sse_message(pending, include_separator=True)
            ) is not None:
                yield frame, True
            if len(pending) == limit and offset < len(view):
                raise ResponseAdaptationTooLarge(limit)
    if pending:
        yield bytes(pending), False


async def _iter_adapted_sse_after_first(
    first: bytes,
    first_raw: bytes,
    frames: AsyncIterator[tuple[bytes, bool]],
    adapter: CodexNamespaceAdapter,
    reconstruction: ReconstructionMap,
    limit: int,
    state: _ResponsesSSEState,
) -> AsyncIterator[bytes]:
    yield first
    if _is_terminal_sse_event(first_raw):
        return

    try:
        async for frame, complete in frames:
            if not complete:
                yield frame
                return
            try:
                transformed = _adapt_sse_frame(frame, adapter, reconstruction, limit)
            except ResponseAdaptationTooLarge:
                yield _response_adaptation_failed_sse(state)
                return
            state.observe(frame)
            yield transformed
            if _is_terminal_sse_event(frame):
                return
    except ResponseAdaptationTooLarge:
        yield _response_adaptation_failed_sse(state)
    except (httpx.HTTPError, OSError, TimeoutError):
        yield _response_transport_failed_sse(state)


def _adapt_sse_frame(
    frame: bytes,
    adapter: CodexNamespaceAdapter,
    reconstruction: ReconstructionMap,
    limit: int,
) -> bytes:
    if len(frame) > limit:
        raise ResponseAdaptationTooLarge(limit)
    try:
        return adapter.transform_sse_frame(frame, reconstruction, max_bytes=limit)
    except TransformationTooLarge as error:
        raise ResponseAdaptationTooLarge(limit) from error


async def _single_chunk(chunk: bytes) -> AsyncIterator[bytes]:
    yield chunk


async def _empty_chunks() -> AsyncIterator[bytes]:
    if False:
        yield b""


def _response_adaptation_failed_sse(state: _ResponsesSSEState) -> bytes:
    return _response_failed_sse(
        state,
        code="response_adaptation_too_large",
        message="MASTIC could not safely adapt the upstream response.",
    )


def _response_transport_failed_sse(state: _ResponsesSSEState) -> bytes:
    return _response_failed_sse(
        state,
        code="upstream_unavailable",
        message="The upstream service stopped while returning the response.",
    )


def _response_failed_sse(
    state: _ResponsesSSEState, *, code: str, message: str
) -> bytes:
    if state.response_id is None:
        event_name = "error"
        payload: dict[str, Any] = {
            "type": "error",
            "code": code,
            "message": message,
            "param": None,
            "sequence_number": state.next_sequence_number,
        }
    else:
        event_name = "response.failed"
        payload = {
            "type": "response.failed",
            "response": {
                "id": state.response_id,
                "object": "response",
                "created_at": 0,
                "status": "failed",
                "completed_at": None,
                "error": {
                    "code": "server_error",
                    "message": message,
                },
                "incomplete_details": None,
                "instructions": None,
                "max_output_tokens": None,
                "model": "mastic-gateway",
                "output": [],
                "previous_response_id": None,
                "reasoning_effort": None,
                "store": False,
                "temperature": 1,
                "text": {"format": {"type": "text"}},
                "tool_choice": "auto",
                "tools": [],
                "top_p": 1,
                "truncation": "disabled",
                "usage": None,
                "user": None,
                "metadata": {},
            },
            "sequence_number": state.next_sequence_number,
        }
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    return f"event: {event_name}\n".encode() + b"data: " + encoded + b"\n\n"


def _pop_sse_message(pending: bytearray, *, include_separator: bool) -> bytes | None:
    boundaries = tuple(
        (index, separator)
        for separator in (b"\r\n\r\n", b"\n\n", b"\r\r")
        if (index := pending.find(separator)) >= 0
    )
    if not boundaries:
        return None
    index, separator = min(boundaries, key=lambda item: item[0])
    end = index + len(separator)
    message = bytes(pending[: end if include_separator else index])
    del pending[:end]
    return message


def _is_terminal_sse_event(event: bytes) -> bool:
    event_name = ""
    data_lines: list[bytes] = []
    for line in event.splitlines():
        field, separator, value = line.partition(b":")
        if not separator:
            continue
        value = value.lstrip(b" ")
        if field == b"event":
            event_name = value.decode("utf-8", errors="replace")
        elif field == b"data":
            data_lines.append(value)
    if event_name in _TERMINAL_RESPONSE_EVENTS:
        return True
    data = b"\n".join(data_lines).strip()
    if data == b"[DONE]":
        return True
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict) and payload.get("type") in _TERMINAL_RESPONSE_EVENTS
    )


def _sse_json_payload(event: bytes) -> Any:
    data_lines = []
    for line in event.splitlines():
        field, separator, value = line.partition(b":")
        if separator and field == b"data":
            data_lines.append(value.lstrip(b" "))
    data = b"\n".join(data_lines).strip()
    if not data or data == b"[DONE]":
        return None
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _enforce_transform_budget(payload: dict[str, Any], limit: int) -> None:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return

    namespaces: list[tuple[str, str, list[Any]]] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "namespace":
            continue
        namespace = tool.get("name")
        description = tool.get("description", "")
        children = tool.get("tools")
        if (
            not isinstance(namespace, str)
            or not namespace
            or not isinstance(description, str)
            or not isinstance(children, list)
            or any(
                not isinstance(child, dict)
                or child.get("type") != "function"
                or not isinstance(child.get("name"), str)
                or not child.get("name")
                or not isinstance(child.get("description", ""), str)
                for child in children
            )
        ):
            return
        namespaces.append((namespace, description, children))

    transformed_string_bytes = 0
    for namespace, namespace_description, children in namespaces:
        namespace_bytes = len(namespace.encode())
        namespace_description_bytes = len(namespace_description.encode())
        for child in children:
            child_name = child["name"]
            child_description = child.get("description", "")
            transformed_string_bytes += (
                namespace_bytes + len(DELIMITER) + len(child_name.encode())
            )
            if namespace_description:
                transformed_string_bytes += (
                    namespace_bytes
                    + namespace_description_bytes
                    + len(child_description.encode())
                    + (5 if child_description else 3)
                )
            if transformed_string_bytes > limit:
                raise RequestTransformationTooLarge
