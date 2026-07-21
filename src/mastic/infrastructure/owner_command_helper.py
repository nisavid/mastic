"""Blocked helper that launches an owner command only after explicit release."""

from __future__ import annotations

import json
import os
import sys
from typing import Callable


_MAX_CONTROL_BYTES = 64 * 1024
_NO_RELEASE_EXIT = 125
_COMMAND_FAILURE_EXIT = 126


def run_controlled_owner_command(
    control_fd: int,
    *,
    read: Callable[[int, int], bytes] = os.read,
    close: Callable[[int], object] = os.close,
    execv: Callable[[str, list[str]], object] = os.execv,
    guard: Callable[[list[str]], int] | None = None,
) -> int:
    """Wait for a complete release payload, then launch the exact command."""

    payload = bytearray()
    while len(payload) <= _MAX_CONTROL_BYTES:
        chunk = read(control_fd, min(4096, _MAX_CONTROL_BYTES + 1 - len(payload)))
        if not chunk:
            break
        payload.extend(chunk)
    if not payload or len(payload) > _MAX_CONTROL_BYTES:
        return _NO_RELEASE_EXIT
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _NO_RELEASE_EXIT
    if (
        not isinstance(decoded, list)
        or not decoded
        or any(
            not isinstance(argument, str) or not argument or "\x00" in argument
            for argument in decoded
        )
    ):
        return _NO_RELEASE_EXIT
    if not os.path.isabs(decoded[0]):
        return _NO_RELEASE_EXIT
    try:
        close(control_fd)
        if guard is not None:
            return guard(decoded)
        return _run_guarded_owner_command(decoded, execv=execv)
    except OSError:
        return _COMMAND_FAILURE_EXIT


def _run_guarded_owner_command(
    argv: list[str],
    *,
    fork: Callable[[], int] = os.fork,
    execv: Callable[[str, list[str]], object] = os.execv,
    child_exit: Callable[[int], object] = os._exit,
    waitpid: Callable[[int, int], tuple[int, int]] = os.waitpid,
) -> int:
    """Fork the exact target inside the helper's recorded private group."""

    try:
        child_pid = fork()
    except OSError:
        return _COMMAND_FAILURE_EXIT
    if child_pid == 0:
        try:
            execv(argv[0], argv)
        except OSError:
            child_exit(_COMMAND_FAILURE_EXIT)
        child_exit(_COMMAND_FAILURE_EXIT)
        return _COMMAND_FAILURE_EXIT
    try:
        _waited_pid, status = waitpid(child_pid, 0)
    except OSError:
        return _COMMAND_FAILURE_EXIT
    return os.waitstatus_to_exitcode(status)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        return _NO_RELEASE_EXIT
    try:
        control_fd = int(arguments[0])
    except ValueError:
        return _NO_RELEASE_EXIT
    if control_fd < 0:
        return _NO_RELEASE_EXIT
    try:
        return run_controlled_owner_command(control_fd)
    finally:
        try:
            os.close(control_fd)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
