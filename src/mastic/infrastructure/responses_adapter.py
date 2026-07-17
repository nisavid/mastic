"""Lossless Codex Responses namespace adaptation for the MASTIC Gateway."""

from __future__ import annotations

import copy
import json
import threading
from collections import OrderedDict
from typing import Any

DELIMITER = "__"
ReconstructionMap = dict[str, tuple[str, str]]
_DEFAULT_STATE_CAPACITY = 256
_DEFAULT_STATE_BYTE_CAPACITY = 256 * 1024


class TransformationError(ValueError):
    """Raised when namespace flattening would be ambiguous or lossy."""


class TransformationTooLarge(RuntimeError):
    """Raised before a transformed representation exceeds its byte budget."""


class NamespaceState:
    """Bounded process-local reconstruction state for Responses continuations."""

    def __init__(
        self,
        capacity: int = _DEFAULT_STATE_CAPACITY,
        byte_capacity: int = _DEFAULT_STATE_BYTE_CAPACITY,
    ) -> None:
        if capacity <= 0:
            raise ValueError("namespace state capacity must be positive")
        if byte_capacity <= 0:
            raise ValueError("namespace state byte capacity must be positive")
        self._capacity = capacity
        self._byte_capacity = byte_capacity
        self._maps: OrderedDict[str, ReconstructionMap] = OrderedDict()
        self._weights: dict[str, int] = {}
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, response_id: str) -> ReconstructionMap | None:
        with self._lock:
            mapping = self._maps.get(response_id)
            if mapping is None:
                return None
            self._maps.move_to_end(response_id)
            return dict(mapping)

    def remember(self, response_id: str, mapping: ReconstructionMap) -> None:
        if not response_id:
            return
        stored_mapping = dict(mapping)
        weight = _reconstruction_weight(response_id, stored_mapping)
        with self._lock:
            existing_weight = self._weights.pop(response_id, 0)
            self._bytes -= existing_weight
            self._maps.pop(response_id, None)

            if weight > self._byte_capacity:
                return

            self._maps[response_id] = stored_mapping
            self._weights[response_id] = weight
            self._bytes += weight
            self._maps.move_to_end(response_id)
            while len(self._maps) > self._capacity or self._bytes > self._byte_capacity:
                evicted_response_id, _ = self._maps.popitem(last=False)
                self._bytes -= self._weights.pop(evicted_response_id)


class CodexNamespaceAdapter:
    """Adapt Codex namespace tools while the Gateway owns HTTP transport."""

    def __init__(
        self,
        capacity: int = _DEFAULT_STATE_CAPACITY,
        byte_capacity: int = _DEFAULT_STATE_BYTE_CAPACITY,
    ) -> None:
        self._state = NamespaceState(capacity, byte_capacity)

    def transform_request(
        self, data: dict[str, Any]
    ) -> tuple[dict[str, Any], ReconstructionMap]:
        previous_id = data.get("previous_response_id")
        inherited: ReconstructionMap = {}
        if previous_id is not None:
            if not isinstance(previous_id, str) or not previous_id:
                raise TransformationError(
                    "previous_response_id must be a non-empty string"
                )
            inherited_mapping = self._state.get(previous_id)
            if inherited_mapping is None:
                raise TransformationError(
                    "previous_response_id namespace state is unavailable"
                )
            inherited = inherited_mapping

        current_ordinary_names = _ordinary_names(data)
        transformed, current = flatten_request(data)
        if inherited.keys() & current_ordinary_names:
            raise TransformationError(
                "inherited namespace mapping collides with current ordinary tool"
            )
        return transformed, _merge_reconstruction(inherited, current)

    def transform_response_body(
        self,
        data: Any,
        raw: bytes,
        reconstruction: ReconstructionMap,
        *,
        max_bytes: int,
    ) -> bytes:
        """Reconstruct and encode one JSON response within a strict byte budget."""

        transformed = transform_response(data, reconstruction)
        if transformed == data:
            if len(raw) > max_bytes:
                raise TransformationTooLarge
            encoded = raw
        else:
            encoded = _encode_json_limited(transformed, max_bytes)
        for response_id in response_ids(data):
            self._state.remember(response_id, reconstruction)
        return encoded

    def transform_sse_frame(
        self,
        frame: bytes,
        reconstruction: ReconstructionMap,
        *,
        max_bytes: int | None = None,
    ) -> bytes:
        lines = frame.splitlines(keepends=True)
        transformed, response_id_values = _transform_sse_lines(
            lines, reconstruction, max_bytes=max_bytes
        )
        for response_id in response_id_values:
            self._state.remember(response_id, reconstruction)
        return transformed


def flatten_request(
    data: dict[str, Any],
) -> tuple[dict[str, Any], ReconstructionMap]:
    """Return a transformed copy and its exact flattened-name reconstruction map."""
    transformed = copy.deepcopy(data)
    reconstruction: ReconstructionMap = {}
    ordinary_names = _ordinary_names(transformed)
    tools = transformed.get("tools")
    if isinstance(tools, list):
        flat: list[Any] = []
        for tool in tools:
            if isinstance(tool, dict) and tool.get("type") == "namespace":
                namespace = tool.get("name", "")
                if not isinstance(namespace, str) or not namespace:
                    raise TransformationError(
                        "namespace tools require a non-empty name"
                    )
                namespace_description = tool.get("description", "")
                if not isinstance(namespace_description, str):
                    raise TransformationError("namespace descriptions must be strings")
                children = tool.get("tools", [])
                if not isinstance(children, list):
                    raise TransformationError("namespace tools require a tools list")
                for nested in children:
                    if not isinstance(nested, dict) or nested.get("type") != "function":
                        raise TransformationError(
                            "namespace tools may contain only function children"
                        )
                    nested = dict(nested)
                    call_name = nested.get("name", "")
                    if not isinstance(call_name, str) or not call_name:
                        raise TransformationError(
                            "namespace functions require a non-empty name"
                        )
                    flat_name = f"{namespace}{DELIMITER}{call_name}"
                    _register_flat_name(
                        reconstruction,
                        ordinary_names,
                        flat_name,
                        namespace,
                        call_name,
                        allow_existing=False,
                    )
                    nested["name"] = flat_name
                    description = nested.get("description", "")
                    if not isinstance(description, str):
                        raise TransformationError(
                            "namespace function descriptions must be strings"
                        )
                    if namespace_description and description:
                        nested["description"] = (
                            f"[{namespace}] {namespace_description}\n\n{description}"
                        )
                    elif namespace_description:
                        nested["description"] = f"[{namespace}] {namespace_description}"
                    flat.append(nested)
            else:
                flat.append(tool)
        transformed["tools"] = flat

    inputs = transformed.get("input")
    if isinstance(inputs, list):
        transformed["input"] = [
            _flatten_input_item(item, reconstruction, ordinary_names) for item in inputs
        ]
    return transformed, reconstruction


def transform_response(data: Any, reconstruction: ReconstructionMap) -> Any:
    """Return a response copy with flattened call names reconstructed."""
    if not isinstance(data, dict):
        return data
    transformed = copy.deepcopy(data)
    item = transformed.get("item")
    if isinstance(item, dict):
        _split_call_name(item, reconstruction)
    response = transformed.get("response")
    if isinstance(response, dict):
        output = response.get("output")
        if isinstance(output, list):
            for output_item in output:
                _split_call_name(output_item, reconstruction)
    output = transformed.get("output")
    if isinstance(output, list):
        for output_item in output:
            _split_call_name(output_item, reconstruction)
    return transformed


def response_ids(data: Any) -> set[str]:
    """Return Responses identifiers exposed by a JSON response or SSE event."""
    ids: set[str] = set()
    if not isinstance(data, dict):
        return ids
    response_id = data.get("id")
    if isinstance(response_id, str) and response_id:
        ids.add(response_id)
    response = data.get("response")
    if isinstance(response, dict):
        nested_id = response.get("id")
        if isinstance(nested_id, str) and nested_id:
            ids.add(nested_id)
    return ids


def _reconstruction_weight(response_id: str, mapping: ReconstructionMap) -> int:
    return len(response_id.encode("utf-8")) + sum(
        len(value.encode("utf-8"))
        for flat_name, (namespace, function_name) in mapping.items()
        for value in (flat_name, namespace, function_name)
    )


def _register_flat_name(
    reconstruction: ReconstructionMap,
    ordinary_names: set[str],
    flat_name: str,
    namespace: str,
    call_name: str,
    *,
    allow_existing: bool,
) -> None:
    expected = (namespace, call_name)
    if flat_name in ordinary_names:
        raise TransformationError(f"flattened tool name collision: {flat_name}")
    existing = reconstruction.get(flat_name)
    if existing is not None and (existing != expected or not allow_existing):
        raise TransformationError(f"flattened tool name collision: {flat_name}")
    reconstruction[flat_name] = expected


def _flatten_input_item(
    item: Any, reconstruction: ReconstructionMap, ordinary_names: set[str]
) -> Any:
    if not isinstance(item, dict):
        return item
    transformed = dict(item)
    item_type = transformed.get("type", "")
    if item_type == "custom_tool_call" and "namespace" in transformed:
        namespace = transformed.get("namespace")
        name = transformed.get("name")
        if not isinstance(namespace, str) or not namespace:
            raise TransformationError("history namespace must be a non-empty string")
        if not isinstance(name, str) or not name:
            raise TransformationError(
                "history function name must be a non-empty string"
            )
        raise TransformationError("namespaced custom tool history is not supported")
    if item_type == "function_call" and "namespace" in transformed:
        namespace = transformed.get("namespace")
        call_name = transformed.get("name")
        if not isinstance(namespace, str) or not namespace:
            raise TransformationError("history namespace must be a non-empty string")
        if not isinstance(call_name, str) or not call_name:
            raise TransformationError(
                "history function name must be a non-empty string"
            )
        transformed.pop("namespace")
        flat_name = f"{namespace}{DELIMITER}{call_name}"
        _register_flat_name(
            reconstruction,
            ordinary_names,
            flat_name,
            namespace,
            call_name,
            allow_existing=True,
        )
        transformed["name"] = flat_name
    for key, value in list(transformed.items()):
        if isinstance(value, list):
            transformed[key] = [
                _flatten_input_item(child, reconstruction, ordinary_names)
                for child in value
            ]
    return transformed


def _ordinary_names(data: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    tools = data.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type") not in {
                "function",
                "custom",
            }:
                continue
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                raise TransformationError("ordinary tools require a non-empty name")
            names.add(name)

    def collect_history_calls(items: list[Any]) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call" and "namespace" not in item:
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    raise TransformationError(
                        "ordinary history calls require a non-empty name"
                    )
                names.add(name)
            for value in item.values():
                if isinstance(value, list):
                    collect_history_calls(value)

    inputs = data.get("input")
    if isinstance(inputs, list):
        collect_history_calls(inputs)
    return names


def _merge_reconstruction(
    inherited: ReconstructionMap, current: ReconstructionMap
) -> ReconstructionMap:
    merged = dict(inherited)
    for flat_name, target in current.items():
        existing = merged.get(flat_name)
        if existing is not None and existing != target:
            raise TransformationError(f"flattened tool name collision: {flat_name}")
        merged[flat_name] = target
    return merged


def _split_call_name(item: Any, reconstruction: ReconstructionMap) -> None:
    if not isinstance(item, dict) or item.get("type") != "function_call":
        return
    name = item.get("name", "")
    if isinstance(name, str) and name in reconstruction:
        namespace, call_name = reconstruction[name]
        item["name"] = call_name
        item["namespace"] = namespace


def _transform_sse_lines(
    lines: list[bytes],
    reconstruction: ReconstructionMap,
    *,
    max_bytes: int | None,
) -> tuple[bytes, set[str]]:
    raw = b"".join(lines)
    data_indexes: list[int] = []
    data_parts: list[bytes] = []
    for index, line in enumerate(lines):
        content = line.rstrip(b"\r\n")
        if content.startswith(b"data:"):
            payload = content[5:]
            if payload.startswith(b" "):
                payload = payload[1:]
            data_indexes.append(index)
            data_parts.append(payload)
    if not data_parts:
        return raw, set()
    payload = b"\n".join(data_parts)
    if not payload.strip() or payload.strip() == b"[DONE]":
        return raw, set()
    try:
        original = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw, set()
    ids = response_ids(original)
    transformed = transform_response(original, reconstruction)
    if transformed == original:
        if max_bytes is not None and len(raw) > max_bytes:
            raise TransformationTooLarge
        return raw, ids
    first = data_indexes[0]
    newline = b"\r\n" if lines[first].endswith(b"\r\n") else b"\n"
    rebuilt = bytearray()
    for index, line in enumerate(lines):
        if index == first:
            _append_limited(rebuilt, b"data: ", max_bytes)
            _append_json_limited(rebuilt, transformed, max_bytes)
            _append_limited(rebuilt, newline, max_bytes)
        elif index not in data_indexes[1:]:
            _append_limited(rebuilt, line, max_bytes)
    return bytes(rebuilt), ids


def _encode_json_limited(value: Any, max_bytes: int) -> bytes:
    encoded = bytearray()
    _append_json_limited(encoded, value, max_bytes)
    return bytes(encoded)


def _append_json_limited(output: bytearray, value: Any, max_bytes: int | None) -> None:
    for part in _iter_json_bytes(value):
        _append_limited(output, part, max_bytes)


def _append_limited(output: bytearray, part: bytes, max_bytes: int | None) -> None:
    if max_bytes is not None and len(output) + len(part) > max_bytes:
        raise TransformationTooLarge
    output.extend(part)


def _iter_json_bytes(value: Any):
    if value is None:
        yield b"null"
    elif value is True:
        yield b"true"
    elif value is False:
        yield b"false"
    elif isinstance(value, str):
        yield from _iter_json_string_bytes(value)
    elif isinstance(value, int):
        yield str(value).encode("ascii")
    elif isinstance(value, float):
        yield json.dumps(value).encode("ascii")
    elif isinstance(value, list):
        yield b"["
        for index, item in enumerate(value):
            if index:
                yield b","
            yield from _iter_json_bytes(item)
        yield b"]"
    elif isinstance(value, dict):
        yield b"{"
        for index, (key, item) in enumerate(value.items()):
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            if index:
                yield b","
            yield from _iter_json_string_bytes(key)
            yield b":"
            yield from _iter_json_bytes(item)
        yield b"}"
    else:
        raise TypeError(
            f"Object of type {type(value).__name__} is not JSON serializable"
        )


def _iter_json_string_bytes(value: str):
    chunk = bytearray(b'"')
    escapes = {
        '"': b'\\"',
        "\\": b"\\\\",
        "\b": b"\\b",
        "\f": b"\\f",
        "\n": b"\\n",
        "\r": b"\\r",
        "\t": b"\\t",
    }
    for character in value:
        escaped = escapes.get(character)
        if escaped is None:
            codepoint = ord(character)
            if codepoint < 0x20:
                escaped = f"\\u{codepoint:04x}".encode("ascii")
            elif codepoint <= 0x7F:
                escaped = bytes((codepoint,))
            elif codepoint <= 0xFFFF:
                escaped = f"\\u{codepoint:04x}".encode("ascii")
            else:
                codepoint -= 0x10000
                high = 0xD800 | (codepoint >> 10)
                low = 0xDC00 | (codepoint & 0x3FF)
                escaped = f"\\u{high:04x}\\u{low:04x}".encode("ascii")
        if len(chunk) + len(escaped) > 1024:
            yield bytes(chunk)
            chunk.clear()
        chunk.extend(escaped)
    chunk.extend(b'"')
    yield bytes(chunk)
