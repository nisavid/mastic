"""Version-2 canonical JSON, timestamps, and content fingerprints."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

import rfc8785


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ARTIFACT_DIGEST = re.compile(r"(?:sha256:[0-9a-f]{64}|sha512:[0-9a-f]{128})\Z")


def require_identity(value: object, field_name: str) -> str:
    """Require one nonempty, whitespace-free canonical identity."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise ValueError(f"{field_name} must be a nonempty identity")
    return value


def require_sha256(value: object, field_name: str) -> str:
    """Require one lowercase SHA-256 digest."""

    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def require_artifact_digest(value: object, field_name: str) -> str:
    """Require one lowercase SHA-256 or SHA-512 artifact digest."""

    if not isinstance(value, str) or _ARTIFACT_DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 or SHA-512 digest")
    return value


def require_aware(value: object, field_name: str) -> datetime:
    """Require one timezone-aware datetime."""

    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


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
