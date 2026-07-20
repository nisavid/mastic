"""Version-2 canonical JSON, timestamps, and content fingerprints."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import rfc8785


def canonical_json_bytes(value: object) -> bytes:
    """Serialize one I-JSON value with RFC 8785 JCS."""

    return rfc8785.dumps(value)


def canonical_fingerprint(value: object) -> str:
    """Return the version-2 lowercase SHA-256 identity for one value."""

    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def canonical_timestamp(value: datetime) -> str:
    """Render an aware datetime in the version-2 canonical UTC subset."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("canonical timestamp must be timezone-aware")
    normalized = value.astimezone(UTC)
    whole_seconds = (
        f"{normalized.year:04d}-{normalized.month:02d}-{normalized.day:02d}"
        f"T{normalized.hour:02d}:{normalized.minute:02d}:{normalized.second:02d}"
    )
    if normalized.microsecond == 0:
        return whole_seconds + "Z"
    fraction = f"{normalized.microsecond:06d}".rstrip("0")
    return f"{whole_seconds}.{fraction}Z"
