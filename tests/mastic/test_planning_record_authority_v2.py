import os
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mastic.domain.canonical import canonical_fingerprint
from mastic.domain.planning_records import PlanApproval
from mastic.infrastructure.planning_record_authority import (
    LocalGrantReceiptIssuer,
    LocalGrantReceiptVerifier,
    PlanApprovalDraft,
)


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
NOW = datetime(2026, 7, 20, 21, 46, tzinfo=UTC)


def draft() -> PlanApprovalDraft:
    return PlanApprovalDraft(
        plan_identity=SHA_A,
        plan_purpose="reconciliation",
        policy_fingerprint=SHA_B,
        evidence_set_fingerprint=SHA_A,
        applicable_claim_ids=(),
        rule_ids=("owner-upgrade-review",),
        override_rule_ids=(),
        valid_for=timedelta(hours=1),
    )


class LocalGrantReceiptAuthorityTests(unittest.TestCase):
    def test_concurrent_first_use_converges_on_one_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "state" / "private" / "planning-grant.key"
            callers = 16
            ready = threading.Barrier(callers)
            approvals: list[PlanApproval] = []
            failures: list[Exception] = []
            results_lock = threading.Lock()

            def issue() -> None:
                try:
                    issuer = LocalGrantReceiptIssuer(key, clock=lambda: NOW)
                    ready.wait()
                    approval = issuer.issue(draft())
                    with results_lock:
                        approvals.append(approval)
                except Exception as error:
                    with results_lock:
                        failures.append(error)

            workers = [threading.Thread(target=issue) for _index in range(callers)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(5)

            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(failures, [])
            self.assertEqual(len(approvals), callers)
            self.assertEqual(
                {approval.approval_identity for approval in approvals},
                {approvals[0].approval_identity},
            )
            verifier = LocalGrantReceiptVerifier(key)
            self.assertTrue(all(verifier.verify(approval) for approval in approvals))

    def test_issuer_derives_trusted_fields_and_verifier_revalidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "state" / "private" / "planning-grant.key"
            issuer = LocalGrantReceiptIssuer(key, clock=lambda: NOW)
            verifier = LocalGrantReceiptVerifier(key)

            approval = issuer.issue(draft())

            self.assertTrue(verifier.verify(approval))
            self.assertEqual(approval.authorization_subject.id, str(os.getuid()))
            self.assertEqual(approval.granted_at, "2026-07-20T21:46:00Z")
            self.assertEqual(approval.valid_until, "2026-07-20T22:46:00Z")
            self.assertEqual(
                approval.grant_receipt.verifier_id,
                "mastic-local-auth:hmac-sha256:v1",
            )
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            self.assertEqual(key.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(key.parent.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(key.stat().st_size, 32)
            self.assertFalse(hasattr(verifier, "issue"))

    def test_tampered_approval_or_unsafe_key_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "private" / "planning-grant.key"
            issuer = LocalGrantReceiptIssuer(key, clock=lambda: NOW)
            verifier = LocalGrantReceiptVerifier(key)
            approval = issuer.issue(draft())

            changed = approval.to_mapping()
            changed["grant_receipt"]["proof"] += "changed"
            changed["approval_identity"] = canonical_fingerprint(
                {
                    name: value
                    for name, value in changed.items()
                    if name != "approval_identity"
                }
            )
            tampered = PlanApproval.from_mapping(changed)
            self.assertFalse(verifier.verify(tampered))
            self.assertFalse(verifier.verify(object()))

            key.chmod(0o644)
            self.assertFalse(verifier.verify(approval))

    def test_missing_key_cannot_verify_or_create_synthetic_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "private" / "planning-grant.key"
            approval = LocalGrantReceiptIssuer(key, clock=lambda: NOW).issue(draft())
            key.unlink()

            self.assertFalse(LocalGrantReceiptVerifier(key).verify(approval))
            self.assertFalse(key.exists())

    def test_caller_cannot_supply_subject_time_verifier_or_identity(self) -> None:
        fields = set(PlanApprovalDraft.__dataclass_fields__)
        self.assertNotIn("authorization_subject", fields)
        self.assertNotIn("granted_at", fields)
        self.assertNotIn("verifier_id", fields)
        self.assertNotIn("grant_receipt", fields)
        self.assertNotIn("approval_identity", fields)

    def test_invalid_draft_fails_before_a_key_is_published(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "private" / "planning-grant.key"
            issuer = LocalGrantReceiptIssuer(key, clock=lambda: NOW)

            with self.assertRaises(ValueError):
                issuer.issue(replace(draft(), valid_for=timedelta(0)))
            with self.assertRaises(ValueError):
                issuer.issue(replace(draft(), plan_identity="not-a-digest"))

            self.assertFalse(key.exists())


if __name__ == "__main__":
    unittest.main()
