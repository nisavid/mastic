"""Private exec gate for crash-safe Supervisor runtime launches."""

from __future__ import annotations

import json
import os
import sys
from typing import Sequence


_HEADER_BYTES = 8
_MAX_PAYLOAD_BYTES = 1024 * 1024


def _read_exact(descriptor: int, size: int) -> bytes | None:
    payload = bytearray()
    while len(payload) < size:
        try:
            chunk = os.read(descriptor, size - len(payload))
        except InterruptedError:
            continue
        if not chunk:
            return None
        payload.extend(chunk)
    return bytes(payload)


def _decode_launch(payload: bytes) -> tuple[tuple[str, ...], dict[str, str]]:
    decoded = json.loads(payload)
    if not isinstance(decoded, dict) or set(decoded) != {"argv", "environment"}:
        raise ValueError("invalid launch envelope")
    argv = decoded["argv"]
    environment = decoded["environment"]
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
        or not os.path.isabs(argv[0])
    ):
        raise ValueError("invalid launch argv")
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environment.items()
    ):
        raise ValueError("invalid launch environment")
    return tuple(argv), dict(environment)


def _gate(descriptor: int) -> int:
    header = _read_exact(descriptor, _HEADER_BYTES)
    if header is None:
        return 0
    size = int.from_bytes(header, "big")
    if size <= 0 or size > _MAX_PAYLOAD_BYTES:
        return 78
    payload = _read_exact(descriptor, size)
    if payload is None:
        return 0
    try:
        argv, environment = _decode_launch(payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        return 78
    finally:
        payload = b""
    os.close(descriptor)
    try:
        os.execve(argv[0], argv, environment)
    except OSError:
        os.write(2, b"mastic launch gate could not exec the runtime\n")
        return 126


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        return 64
    try:
        descriptor = int(arguments[0])
    except ValueError:
        return 64
    if descriptor < 0:
        return 64
    try:
        return _gate(descriptor)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
