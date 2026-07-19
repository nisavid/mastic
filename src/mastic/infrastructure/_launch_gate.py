"""Private exec gate for crash-safe Supervisor runtime launches."""

from __future__ import annotations

import json
import os
import sys
from typing import Sequence


# Keep these framing limits paired with system_adapters.py.  This gate stays
# dependency-free and directly executable before package imports are available.
_HEADER_BYTES = 8
_MAX_PAYLOAD_BYTES = 1024 * 1024
_EXIT_OK = 0
_EXIT_USAGE = 64
_EXIT_SOFTWARE = 70
_EXIT_IO = 74
_EXIT_DATA = 78
_EXIT_CANNOT_EXEC = 126


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
        or any("\0" in item for item in argv)
        or not os.path.isabs(argv[0])
    ):
        raise ValueError("invalid launch argv")
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environment.items()
    ):
        raise ValueError("invalid launch environment")
    if any("\0" in key or "\0" in value for key, value in environment.items()):
        raise ValueError("invalid launch environment")
    return tuple(argv), dict(environment)


def _gate(descriptor: int) -> int:
    try:
        header = _read_exact(descriptor, _HEADER_BYTES)
    except (OSError, OverflowError):
        return _EXIT_IO
    if header is None:
        return _EXIT_OK
    size = int.from_bytes(header, "big")
    if size <= 0 or size > _MAX_PAYLOAD_BYTES:
        return _EXIT_DATA
    try:
        payload = _read_exact(descriptor, size)
    except (OSError, OverflowError):
        return _EXIT_IO
    if payload is None:
        return _EXIT_OK
    try:
        argv, environment = _decode_launch(payload)
    except (json.JSONDecodeError, RecursionError, TypeError, UnicodeError, ValueError):
        return _EXIT_DATA
    finally:
        payload = b""
    try:
        os.close(descriptor)
    except OSError:
        return _EXIT_IO
    try:
        os.execve(argv[0], argv, environment)
    except (OSError, ValueError):
        try:
            os.write(2, b"mastic launch gate could not exec the runtime\n")
        except OSError:
            pass
        return _EXIT_CANNOT_EXEC


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        return _EXIT_USAGE
    try:
        descriptor = int(arguments[0])
    except ValueError:
        return _EXIT_USAGE
    if descriptor < 0:
        return _EXIT_USAGE
    try:
        try:
            return _gate(descriptor)
        except (OSError, OverflowError):
            return _EXIT_IO
        except Exception:
            return _EXIT_SOFTWARE
    finally:
        try:
            os.close(descriptor)
        except (OSError, OverflowError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
