"""Lossless Codex Responses namespace adaptation for the MASTIC Gateway."""

from __future__ import annotations

import copy
import json
import threading
from collections import OrderedDict
from typing import Any

DELIMITER = "__"
ReconstructionMap = dict[str, tuple[str, str]]


class TransformationError(ValueError):
    """Raised when namespace flattening would be ambiguous or lossy."""


class NamespaceState:
    """Bounded process-local reconstruction state for Responses continuations."""

    def __init__(self, capacity: int = 256) -> None:
        if capacity <= 0:
            raise ValueError("namespace state capacity must be positive")
        self._capacity = capacity
        self._maps: OrderedDict[str, ReconstructionMap] = OrderedDict()
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
        with self._lock:
            self._maps[response_id] = dict(mapping)
            self._maps.move_to_end(response_id)
            while len(self._maps) > self._capacity:
                self._maps.popitem(last=False)


class CodexNamespaceAdapter:
    """Adapt Codex namespace tools while the Gateway owns HTTP transport."""

    def __init__(self, capacity: int = 256) -> None:
        self._state = NamespaceState(capacity)

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

    def transform_response(self, data: Any, reconstruction: ReconstructionMap) -> Any:
        transformed = transform_response(data, reconstruction)
        for response_id in response_ids(data):
            self._state.remember(response_id, reconstruction)
        return transformed

    def transform_sse_frame(
        self, frame: bytes, reconstruction: ReconstructionMap
    ) -> bytes:
        lines = frame.splitlines(keepends=True)
        transformed, response_id_values = _transform_sse_lines(lines, reconstruction)
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
    lines: list[bytes], reconstruction: ReconstructionMap
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
        return raw, ids
    first = data_indexes[0]
    newline = b"\r\n" if lines[first].endswith(b"\r\n") else b"\n"
    replacement = (
        b"data: " + json.dumps(transformed, separators=(",", ":")).encode() + newline
    )
    rebuilt = [
        replacement if index == first else line
        for index, line in enumerate(lines)
        if index not in data_indexes[1:]
    ]
    return b"".join(rebuilt), ids
