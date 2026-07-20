"""Daemon-only local grant issuance and verifier-only receipt validation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import stat
import threading
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping, Sequence

from mastic.domain.canonical import (
    canonical_fingerprint,
    canonical_json_bytes,
    canonical_timestamp,
)
from mastic.domain.planning_records import PlanApproval


_KEY_BYTES = 32
_VERIFIER_ID = "mastic-local-auth:hmac-sha256:v1"


class LocalGrantReceiptError(RuntimeError):
    """The trusted local grant-receipt key is unavailable or unsafe."""


@dataclass(frozen=True, slots=True)
class PlanApprovalDraft:
    """Caller-selected Approval fields, excluding authenticated grant facts."""

    plan_identity: str
    plan_purpose: str
    policy_fingerprint: str
    evidence_set_fingerprint: str
    applicable_claim_ids: Sequence[str]
    rule_ids: Sequence[str]
    override_rule_ids: Sequence[str]
    valid_for: timedelta | None


class LocalGrantReceiptIssuer:
    """Issue an Approval for the authenticated daemon user only."""

    def __init__(
        self,
        key_path: Path,
        *,
        expected_uid: int | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._key = _LocalGrantKey(key_path, expected_uid=expected_uid)
        self._clock = clock

    def issue(self, draft: PlanApprovalDraft) -> PlanApproval:
        if not isinstance(draft, PlanApprovalDraft):
            raise TypeError("local grant issuance requires a Plan Approval draft")
        granted_at = self._clock()
        if granted_at.tzinfo is None or granted_at.utcoffset() is None:
            raise ValueError("local grant clock must return timezone-aware time")
        validity = draft.valid_for
        if validity is not None and (
            not isinstance(validity, timedelta) or validity <= timedelta(0)
        ):
            raise ValueError("Plan Approval validity must be positive")
        subject = _local_user_subject(self._key.expected_uid)
        statement: dict[str, object] = {
            "schema_version": 2,
            "kind": "mastic.plan-approval",
            "authorization_subject": subject,
            "plan_identity": draft.plan_identity,
            "plan_purpose": draft.plan_purpose,
            "policy_fingerprint": draft.policy_fingerprint,
            "evidence_set_fingerprint": draft.evidence_set_fingerprint,
            "applicable_claim_ids": list(draft.applicable_claim_ids),
            "rule_ids": list(draft.rule_ids),
            "override_rule_ids": list(draft.override_rule_ids),
            "granted_at": canonical_timestamp(granted_at),
            "valid_until": (
                None if validity is None else canonical_timestamp(granted_at + validity)
            ),
        }
        statement_fingerprint = canonical_fingerprint(statement)
        unsigned_receipt = {
            "kind": "authenticated_grant_receipt",
            "verifier_id": _VERIFIER_ID,
            "statement_fingerprint": statement_fingerprint,
            "proof": "base64url:pending-local-authentication",
        }
        unsigned_record = {**statement, "grant_receipt": unsigned_receipt}
        unsigned_record["approval_identity"] = canonical_fingerprint(unsigned_record)
        PlanApproval.from_mapping(unsigned_record)
        receipt = {
            "kind": "authenticated_grant_receipt",
            "verifier_id": _VERIFIER_ID,
            "statement_fingerprint": statement_fingerprint,
            "proof": _proof(
                self._key.load_or_create(),
                statement_fingerprint,
                subject,
            ),
        }
        record = {**statement, "grant_receipt": receipt}
        record["approval_identity"] = canonical_fingerprint(record)
        return PlanApproval.from_mapping(record)


class LocalGrantReceiptVerifier:
    """Verify persisted Approvals without possessing issuance capability."""

    def __init__(self, key_path: Path, *, expected_uid: int | None = None) -> None:
        self._key = _LocalGrantKey(key_path, expected_uid=expected_uid)

    def verify(self, approval: PlanApproval) -> bool:
        if not isinstance(approval, PlanApproval):
            return False
        try:
            subject = _local_user_subject(self._key.expected_uid)
            receipt = approval.grant_receipt
            if (
                approval.authorization_subject.to_mapping() != subject
                or receipt.verifier_id != _VERIFIER_ID
                or receipt.statement_fingerprint != approval.statement_fingerprint
            ):
                return False
            record = approval.to_mapping()
            statement = {
                key: value
                for key, value in record.items()
                if key not in {"approval_identity", "grant_receipt"}
            }
            if canonical_fingerprint(statement) != approval.statement_fingerprint:
                return False
            expected = _proof(
                self._key.load_existing(),
                approval.statement_fingerprint,
                subject,
            )
            return hmac.compare_digest(receipt.proof, expected)
        except (LocalGrantReceiptError, TypeError, ValueError):
            return False


class _LocalGrantKey:
    def __init__(self, path: Path, *, expected_uid: int | None) -> None:
        self.path = Path(path)
        self.expected_uid = os.getuid() if expected_uid is None else expected_uid
        if not self.path.is_absolute():
            raise ValueError("local grant key path must be absolute")
        if type(self.expected_uid) is not int or self.expected_uid < 0:
            raise ValueError("local grant key requires a nonnegative UID")
        self._creation_lock = threading.Lock()

    def load_or_create(self) -> bytes:
        with self._creation_lock:
            parent = self._open_parent(create=True)
            temporary_name = f".{self.path.name}.{secrets.token_hex(8)}.tmp"
            try:
                try:
                    return self._read(self.path.name, parent)
                except FileNotFoundError:
                    pass
                descriptor = os.open(
                    temporary_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_CLOEXEC
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=parent,
                )
                key = os.urandom(_KEY_BYTES)
                try:
                    self._validate_descriptor(descriptor)
                    _write_all(descriptor, key)
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
                    return self._read(self.path.name, parent)
                os.fsync(parent)
                return key
            except OSError as error:
                raise LocalGrantReceiptError(
                    "local grant key creation failed"
                ) from error
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary_name, dir_fd=parent)
                os.close(parent)

    def load_existing(self) -> bytes:
        try:
            parent = self._open_parent(create=False)
        except (FileNotFoundError, OSError) as error:
            raise LocalGrantReceiptError("local grant key is unavailable") from error
        try:
            return self._read(self.path.name, parent)
        except OSError as error:
            raise LocalGrantReceiptError("local grant key is unavailable") from error
        finally:
            os.close(parent)

    def _open_parent(self, *, create: bool) -> int:
        parent_path = self.path.parent
        descriptor: int | None = None
        try:
            if create:
                _create_private_parents(parent_path, self.expected_uid)
            descriptor = os.open(
                parent_path,
                os.O_RDONLY
                | os.O_CLOEXEC
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != self.expected_uid
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise LocalGrantReceiptError("local grant key directory is unsafe")
            return descriptor
        except LocalGrantReceiptError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise LocalGrantReceiptError(
                "local grant key directory is unavailable"
            ) from error

    def _read(self, name: str, parent: int) -> bytes:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        try:
            self._validate_descriptor(descriptor)
            key = os.read(descriptor, _KEY_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(key) != _KEY_BYTES:
            raise LocalGrantReceiptError("local grant key has invalid length")
        return key

    def _validate_descriptor(self, descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self.expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size not in {0, _KEY_BYTES}
        ):
            raise LocalGrantReceiptError("local grant key is unsafe")


def _create_private_parents(path: Path, expected_uid: int) -> None:
    missing: list[Path] = []
    candidate = path
    while not candidate.exists():
        missing.append(candidate)
        candidate = candidate.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            # A concurrent first-use issuer may have created this exact parent.
            pass
        metadata = directory.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise LocalGrantReceiptError("new local grant key directory is unsafe")


def _local_user_subject(uid: int) -> dict[str, str]:
    preimage = {"kind": "local_user", "id": str(uid)}
    return {**preimage, "fingerprint": canonical_fingerprint(preimage)}


def _proof(
    key: bytes,
    statement_fingerprint: str,
    authorization_subject: Mapping[str, object],
) -> str:
    payload = canonical_json_bytes(
        {
            "authorization_subject": dict(authorization_subject),
            "statement_fingerprint": statement_fingerprint,
            "verifier_id": _VERIFIER_ID,
        }
    )
    encoded = base64.urlsafe_b64encode(
        hmac.new(key, payload, hashlib.sha256).digest()
    ).rstrip(b"=")
    return "base64url:" + encoded.decode("ascii")


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("local grant key write did not make progress")
        offset += written
