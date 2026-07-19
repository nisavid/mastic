"""Canonical conversion of application values to deterministic plain data."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path


def to_plain_data(value: object) -> object:
    """Convert supported internal values to JSON-shaped deterministic data."""
    if hasattr(value, "unwrap"):
        return to_plain_data(value.unwrap())  # type: ignore[union-attr]
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_plain_data(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): to_plain_data(item) for key, item in value.items()}
    if isinstance(value, set | frozenset):
        items = [to_plain_data(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ),
        )
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [to_plain_data(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    return value
