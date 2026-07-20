"""Private persistent bearer credential for the per-user Gateway."""

from __future__ import annotations

import hmac
import os
import secrets
import stat
import threading
from contextlib import suppress
from pathlib import Path


_MAX_CREDENTIAL_BYTES = 256


class GatewayCredential:
    """Create, validate, and read one owner-only Gateway bearer token."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("Gateway credential path must be absolute")
        self._token: str | None = None
        self._token_lock = threading.Lock()

    def load_or_create(self) -> str:
        if self._token is not None:
            return self._token
        with self._token_lock:
            if self._token is None:
                self._token = self._load_or_create_uncached()
            return self._token

    def _load_or_create_uncached(self) -> str:
        parent = _open_parent(self.path.parent)
        temporary_name = f".{self.path.name}.{secrets.token_hex(8)}.tmp"
        try:
            try:
                return _read_gateway_token(self.path.name, parent)
            except FileNotFoundError:
                pass
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent,
            )
            token = secrets.token_urlsafe(32)
            try:
                _validate_descriptor(descriptor)
                payload = (token + "\n").encode("ascii")
                _write_all(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                os.link(
                    temporary_name,
                    self.path.name,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
            except FileExistsError:
                return _read_gateway_token(self.path.name, parent)
            os.fsync(parent)
            return token
        finally:
            # Another creator may already have consumed the temporary link.
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=parent)
            os.close(parent)

    def authenticate(self, authorization: str | None) -> bool:
        expected = self.load_or_create()
        candidate = ""
        if authorization is not None and authorization.startswith("Bearer "):
            candidate = authorization.removeprefix("Bearer ")
        try:
            candidate_bytes = candidate.encode("ascii")
            expected_bytes = expected.encode("ascii")
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(candidate_bytes, expected_bytes)

    def authorization_header(self) -> str:
        return f"Bearer {self.load_or_create()}"


def read_gateway_token(path: str | Path) -> str:
    """Read an existing credential through an owner/type/mode checked descriptor."""

    credential_path = Path(path)
    parent = _open_parent(credential_path.parent)
    try:
        return _read_gateway_token(credential_path.name, parent)
    finally:
        os.close(parent)


def _read_gateway_token(name: str, parent: int) -> str:
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent,
    )
    try:
        _validate_descriptor(descriptor)
        payload = os.read(descriptor, _MAX_CREDENTIAL_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(payload) > _MAX_CREDENTIAL_BYTES:
        raise PermissionError("Gateway credential is unexpectedly large")
    try:
        token = payload.decode("ascii").removesuffix("\n")
    except UnicodeDecodeError as error:
        raise PermissionError("Gateway credential is not ASCII") from error
    if not 32 <= len(token) <= 128 or any(character.isspace() for character in token):
        raise PermissionError("Gateway credential has an invalid token format")
    return token


def _open_parent(path: Path) -> int:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise PermissionError("Gateway credential parent must be a directory")
    if metadata.st_uid != os.getuid():
        os.close(descriptor)
        raise PermissionError("Gateway credential parent must be user-owned")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        os.close(descriptor)
        raise PermissionError("Gateway credential parent must have mode 0700")
    return descriptor


def _validate_descriptor(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise PermissionError("Gateway credential must be a regular file")
    if metadata.st_uid != os.getuid():
        raise PermissionError("Gateway credential must be user-owned")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise PermissionError("Gateway credential must have mode 0600")


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("Gateway credential write did not make progress")
        offset += written
