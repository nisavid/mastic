import copy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from mastic.domain.canonical import canonical_fingerprint
from mastic.domain.planning_records import (
    CurrentPlanPointer,
    Plan,
    PlanApproval,
    PlanAssessment,
)
from mastic.infrastructure.planning_record_repository import (
    PlanningRecordConflictError,
    PlanningRecordRepository,
)
from mastic.infrastructure.state_store import OperationalStateStore


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64
SHA_E = "sha256:" + "e" * 64
SHA_F = "sha256:" + "f" * 64


def identified(record: dict[str, object], field: str) -> dict[str, object]:
    result = copy.deepcopy(record)
    result[field] = canonical_fingerprint(result)
    return result


def scope() -> dict[str, object]:
    return {
        "kind": "declared_scope",
        "id": "user:501/default",
        "fingerprint": SHA_A,
    }


def policy_record() -> dict[str, object]:
    value: dict[str, object] = {
        "id": "phase1-online-current",
        "version": 1,
        "input_requirements": [],
        "rules": [
            {
                "rule_id": "owner-upgrade-review",
                "result": "approval_required",
                "overridable": False,
            }
        ],
        "approval_authority": {
            "subject_fingerprints": [SHA_B],
            "rule_ids": ["owner-upgrade-review"],
            "override_rule_ids": [],
        },
        "candidate_selection": {
            "rule_id": "prefer-policy-ranked-eligible",
            "version": 1,
        },
    }
    value["fingerprint"] = canonical_fingerprint(value)
    return value


def policy_selection(policy_fingerprint: object) -> dict[str, object]:
    selected_scope = scope()
    value: dict[str, object] = {
        "kind": "policy_selection",
        "id": "user:501/default:reconciliation",
        "scope_identity": canonical_fingerprint(
            {"kind": selected_scope["kind"], "id": selected_scope["id"]}
        ),
        "purpose": "reconciliation",
        "policy_fingerprint": policy_fingerprint,
        "authority_ref": {
            "kind": "local_policy_authority",
            "id": "user:501",
            "fingerprint": SHA_F,
        },
    }
    value["fingerprint"] = canonical_fingerprint(value)
    return value


def target() -> dict[str, object]:
    value: dict[str, object] = {
        "target_id": "application-installation:codex:vite",
        "target_kind": "external_application_installation",
        "subject_fingerprint": SHA_B,
        "lifecycle_applicability": "applicable",
        "expected_lifecycle_state": "active",
        "operational_contract_ref": {
            "kind": "operational_contract",
            "id": "codex-native-canary",
            "fingerprint": SHA_C,
        },
    }
    value["plan_target_fingerprint"] = canonical_fingerprint(
        {"purpose": "reconciliation", "target": value}
    )
    return value


def step(plan_target: dict[str, object]) -> dict[str, object]:
    value: dict[str, object] = {
        "step_id": "application.codex.upgrade",
        "target_id": plan_target["target_id"],
        "plan_target_fingerprint": plan_target["plan_target_fingerprint"],
        "execution_kind": "mutation",
        "mutation_ids": ["application.codex.owner-upgrade"],
        "depends_on": [],
        "skip_rule_id": None,
        "reuse_rule_id": "exact-purpose-step-material",
    }
    value["step_fingerprint"] = canonical_fingerprint(
        {"purpose": "reconciliation", "step": value}
    )
    return value


def plan_record() -> dict[str, object]:
    selected_target = target()
    required_step = step(selected_target)
    return identified(
        {
            "schema_version": 2,
            "kind": "mastic.plan",
            "blueprint_identity": SHA_D,
            "scope": scope(),
            "purpose": "reconciliation",
            "created_at": "2026-07-20T21:45:00Z",
            "targets": [selected_target],
            "required_steps": [required_step],
            "proposed_declarations": [],
            "mutations": [
                {
                    "mutation_id": "application.codex.owner-upgrade",
                    "step_id": required_step["step_id"],
                    "target_id": selected_target["target_id"],
                    "plan_target_fingerprint": selected_target[
                        "plan_target_fingerprint"
                    ],
                    "capability_port": ("external_application_installation_lifecycle"),
                    "operation": "upgrade",
                    "request": {
                        "installation_identity": selected_target["target_id"],
                        "release_artifact_identity": SHA_E,
                        "source_state_fingerprint": SHA_F,
                        "preview_fingerprint": SHA_A,
                        "owner_action_fingerprint": SHA_B,
                        "artifact_closure_fingerprint": SHA_E,
                    },
                    "expected_current": {"fingerprint": SHA_F},
                    "recovery": {
                        "mode": "restore",
                        "capability_port": (
                            "external_application_installation_lifecycle"
                        ),
                        "operation": "restore",
                        "request": {
                            "installation_identity": selected_target["target_id"],
                            "release_artifact_identity": SHA_C,
                        },
                    },
                }
            ],
        },
        "plan_identity",
    )


def approval_record(plan_identity: object) -> dict[str, object]:
    policy_fingerprint = policy_record()["fingerprint"]
    statement: dict[str, object] = {
        "schema_version": 2,
        "kind": "mastic.plan-approval",
        "authorization_subject": {
            "kind": "local_user",
            "id": "501",
            "fingerprint": SHA_B,
        },
        "plan_identity": plan_identity,
        "plan_purpose": "reconciliation",
        "policy_fingerprint": policy_fingerprint,
        "evidence_set_fingerprint": canonical_fingerprint([]),
        "applicable_claim_ids": [],
        "rule_ids": ["owner-upgrade-review"],
        "override_rule_ids": [],
        "granted_at": "2026-07-20T21:46:00Z",
        "valid_until": "2026-07-20T22:46:00Z",
    }
    return identified(
        {
            **statement,
            "grant_receipt": {
                "kind": "authenticated_grant_receipt",
                "verifier_id": "mastic-daemon-peer:v1",
                "statement_fingerprint": canonical_fingerprint(statement),
                "proof": "base64url:verified-by-test-authority",
            },
        },
        "approval_identity",
    )


def assessment_record(
    plan: dict[str, object], approval: dict[str, object]
) -> dict[str, object]:
    policy = policy_record()
    targets = plan["targets"]
    required_steps = plan["required_steps"]
    if not isinstance(targets, list) or not isinstance(required_steps, list):
        raise AssertionError("test Plan fixture arrays are malformed")
    selected_target = targets[0]
    required_step = required_steps[0]
    if not isinstance(selected_target, dict) or not isinstance(required_step, dict):
        raise AssertionError("test Plan fixture objects are malformed")
    issues = sorted(
        (
            identified(
                {
                    "category": "observation",
                    "code": code,
                    "scope": {"kind": "target", "id": selected_target["target_id"]},
                    "message": message,
                    "evidence_ids": [],
                    "next_actions": [action],
                },
                "issue_identity",
            )
            for code, message, action in (
                (
                    "lifecycle_not_observed",
                    "Target lifecycle has not been observed.",
                    "inspect target lifecycle",
                ),
                (
                    "condition_not_observed",
                    "Target condition has not been observed.",
                    "run the target canary",
                ),
            )
        ),
        key=lambda item: item["issue_identity"],
    )
    lifecycle_issue = next(
        item for item in issues if item["code"] == "lifecycle_not_observed"
    )
    condition_issue = next(
        item for item in issues if item["code"] == "condition_not_observed"
    )
    target_id = selected_target["target_id"]
    return identified(
        {
            "schema_version": 2,
            "kind": "mastic.plan-assessment",
            "plan": {
                "plan_identity": plan["plan_identity"],
                "blueprint_identity": plan["blueprint_identity"],
                "scope": plan["scope"],
                "purpose": plan["purpose"],
                "target_ids": [selected_target["target_id"]],
            },
            "evaluation": {
                "evaluated_at": "2026-07-20T21:47:00Z",
                "policy_selection": policy_selection(policy["fingerprint"]),
                "policy": policy,
                "evidence_set_fingerprint": canonical_fingerprint([]),
            },
            "evidence_ids": [],
            "policy_inputs": {
                "fingerprint": canonical_fingerprint(
                    {
                        "claim_ids": [],
                        "claim_conflict_ids": [],
                        "evidence_ids": [],
                        "discovery_evidence_ids": [],
                    }
                ),
                "claim_ids": [],
                "claim_conflict_ids": [],
                "evidence_ids": [],
                "discovery_evidence_ids": [],
            },
            "claims": [],
            "claim_qualifications": [],
            "claim_applicability": [],
            "claim_conflicts": [],
            "positions": {"support": [], "permission": [], "searches": []},
            "approvals": [
                {
                    "identity": approval["approval_identity"],
                    "kind": "mastic.plan-approval",
                }
            ],
            "policy_assessment": {
                "disposition": "eligible",
                "applicable_claim_ids": [],
                "claim_conflict_ids": [],
                "rule_evaluations": [
                    {
                        "rule_id": "owner-upgrade-review",
                        "result": "satisfied",
                        "claim_ids": [],
                        "claim_conflict_ids": [],
                        "evidence_ids": [],
                        "approval_identity": approval["approval_identity"],
                    }
                ],
                "approval_evaluation": {
                    "requirement": "evaluated",
                    "evaluations": [
                        {
                            "approval_identity": approval["approval_identity"],
                            "value": "applicable",
                        }
                    ],
                },
            },
            "operational_assessment": {
                "completion": {
                    "value": "partial",
                    "required_step_ids": [required_step["step_id"]],
                    "step_satisfactions": [],
                },
                "targets": [
                    {
                        "target_id": target_id,
                        "target_kind": selected_target["target_kind"],
                        "subject_fingerprint": selected_target["subject_fingerprint"],
                        "plan_target_fingerprint": selected_target[
                            "plan_target_fingerprint"
                        ],
                        "completion": "partial",
                        "lifecycle": {
                            "observation": "not_observed",
                            "issue_ids": [lifecycle_issue["issue_identity"]],
                        },
                        "operational_condition": {
                            "observation": "not_observed",
                            "issue_ids": [condition_issue["issue_identity"]],
                        },
                        "issue_ids": sorted(item["issue_identity"] for item in issues),
                    }
                ],
                "summary": {
                    "by_completion": {"partial": [target_id], "complete": []},
                    "by_lifecycle_state": {
                        "absent": [],
                        "present": [],
                        "transitioning": [],
                        "active": [],
                    },
                    "by_operational_condition": {
                        "functional": [],
                        "degraded": [],
                        "nonfunctional": [],
                    },
                    "lifecycle_not_observed": [target_id],
                    "lifecycle_not_applicable": [],
                    "condition_not_observed": [target_id],
                    "condition_not_applicable": [],
                },
            },
            "issues": issues,
        },
        "assessment_identity",
    )


def pointer_record(
    plan: dict[str, object], assessment: dict[str, object]
) -> dict[str, object]:
    selected_scope = plan["scope"]
    if not isinstance(selected_scope, dict):
        raise AssertionError("test Plan scope is malformed")
    return identified(
        {
            "schema_version": 2,
            "kind": "mastic.current-plan-pointer",
            "scope": selected_scope,
            "scope_identity": canonical_fingerprint(
                {"kind": selected_scope["kind"], "id": selected_scope["id"]}
            ),
            "pointer_version": 1,
            "expected_predecessor_identity": None,
            "plan_identity": plan["plan_identity"],
            "plan_purpose": plan["purpose"],
            "assessment_identity": assessment["assessment_identity"],
            "updated_at": "2026-07-20T21:48:00Z",
        },
        "pointer_identity",
    )


class ReceiptVerifier:
    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted
        self.calls: list[PlanApproval] = []

    def verify(self, approval: PlanApproval) -> bool:
        self.calls.append(approval)
        return self.accepted


class PlanningRecordRepositoryTests(unittest.TestCase):
    def test_immutable_records_round_trip_as_exact_types_and_reverify_approvals(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            verifier = ReceiptVerifier()
            repository = PlanningRecordRepository(
                OperationalStateStore(Path(directory) / "state.sqlite3"), verifier
            )
            plan_payload = plan_record()
            approval_payload = approval_record(plan_payload["plan_identity"])
            assessment_payload = assessment_record(plan_payload, approval_payload)

            stored_plan = repository.put_plan(plan_payload)
            stored_approval = repository.put_approval(approval_payload)
            stored_assessment = repository.put_assessment(assessment_payload)

            self.assertIsInstance(stored_plan, Plan)
            self.assertIsInstance(stored_approval, PlanApproval)
            self.assertIsInstance(stored_assessment, PlanAssessment)
            self.assertEqual(repository.plan(stored_plan.plan_identity), stored_plan)
            self.assertEqual(
                repository.approval(stored_approval.approval_identity), stored_approval
            )
            self.assertEqual(
                repository.assessment(stored_assessment.assessment_identity),
                stored_assessment,
            )
            self.assertEqual(len(verifier.calls), 4)

            verifier.accepted = False
            with self.assertRaisesRegex(ValueError, "receipt"):
                repository.approval(stored_approval.approval_identity)

            self.assertIsNone(repository.plan("sha256:" + "f" * 64))
            self.assertIsNone(repository.approval("sha256:" + "f" * 64))
            self.assertIsNone(repository.assessment("sha256:" + "f" * 64))

    def test_first_current_pointer_resolves_exact_plan_assessment_and_approval(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = PlanningRecordRepository(
                OperationalStateStore(Path(directory) / "state.sqlite3"),
                ReceiptVerifier(),
            )
            plan_payload = plan_record()
            approval_payload = approval_record(plan_payload["plan_identity"])
            assessment_payload = assessment_record(plan_payload, approval_payload)
            pointer_payload = pointer_record(plan_payload, assessment_payload)
            repository.put_plan(plan_payload)
            repository.put_approval(approval_payload)
            repository.put_assessment(assessment_payload)

            stored = repository.compare_and_put_current(pointer_payload)

            self.assertIsInstance(stored, CurrentPlanPointer)
            self.assertEqual(repository.current(stored.scope_identity), stored)

    def test_assessment_is_plan_validated_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = PlanningRecordRepository(
                OperationalStateStore(Path(directory) / "state.sqlite3"),
                ReceiptVerifier(),
            )
            plan_payload = plan_record()
            approval_payload = approval_record(plan_payload["plan_identity"])
            assessment_payload = assessment_record(plan_payload, approval_payload)

            with self.assertRaisesRegex(ValueError, "missing Plan"):
                repository.put_assessment(assessment_payload)

            repository.put_plan(plan_payload)
            drifted = copy.deepcopy(assessment_payload)
            drifted["operational_assessment"]["completion"]["required_step_ids"] = [
                "different-step"
            ]
            drifted = identified(
                {
                    key: value
                    for key, value in drifted.items()
                    if key != "assessment_identity"
                },
                "assessment_identity",
            )
            with self.assertRaisesRegex(ValueError, "required steps"):
                repository.put_assessment(drifted)

            self.assertIsNone(repository.assessment(drifted["assessment_identity"]))

            subject_drift = copy.deepcopy(assessment_payload)
            subject_drift["operational_assessment"]["targets"][0][
                "subject_fingerprint"
            ] = "sha256:" + "f" * 64
            subject_drift = identified(
                {
                    key: value
                    for key, value in subject_drift.items()
                    if key != "assessment_identity"
                },
                "assessment_identity",
            )
            with self.assertRaisesRegex(ValueError, "operational targets"):
                repository.put_assessment(subject_drift)

    def test_malformed_legacy_tampered_and_unauthenticated_records_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = OperationalStateStore(Path(directory) / "state.sqlite3")
            verifier = ReceiptVerifier(accepted=False)
            repository = PlanningRecordRepository(state, verifier)
            plan_payload = plan_record()
            approval_payload = approval_record(plan_payload["plan_identity"])
            assessment_payload = assessment_record(plan_payload, approval_payload)

            with self.assertRaisesRegex(ValueError, "version 2"):
                repository.put_plan({**plan_payload, "schema_version": 1})
            with self.assertRaisesRegex(ValueError, "receipt"):
                repository.put_approval(approval_payload)
            with self.assertRaisesRegex(ValueError, "identity"):
                repository.put_assessment(
                    {**assessment_payload, "assessment_identity": "sha256:" + "f" * 64}
                )

            tampered = copy.deepcopy(plan_payload)
            tampered["mutations"][0]["request"]["preview_fingerprint"] = (
                "sha256:" + "f" * 64
            )
            state.put_snapshot(
                {
                    "kind": "mastic.plan",
                    "id": plan_payload["plan_identity"],
                    "version": plan_payload["plan_identity"],
                    "record": tampered,
                }
            )
            with self.assertRaisesRegex(ValueError, "identity"):
                repository.plan(plan_payload["plan_identity"])

    def test_current_pointer_requires_every_exact_reference_before_selection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = PlanningRecordRepository(
                OperationalStateStore(Path(directory) / "state.sqlite3"),
                ReceiptVerifier(),
            )
            plan_payload = plan_record()
            approval_payload = approval_record(plan_payload["plan_identity"])
            assessment_payload = assessment_record(plan_payload, approval_payload)
            pointer_payload = pointer_record(plan_payload, assessment_payload)

            with self.assertRaisesRegex(ValueError, "missing Plan$"):
                repository.compare_and_put_current(pointer_payload)
            repository.put_plan(plan_payload)
            with self.assertRaisesRegex(ValueError, "missing Plan Assessment"):
                repository.compare_and_put_current(pointer_payload)
            with self.assertRaisesRegex(ValueError, "missing Plan Approval"):
                repository.put_assessment(assessment_payload)
            repository.put_approval(approval_payload)
            repository.put_assessment(assessment_payload)
            self.assertEqual(
                repository.compare_and_put_current(pointer_payload).plan_identity,
                plan_payload["plan_identity"],
            )

    def test_current_pointer_rejects_cross_record_drift_marked_applicable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = PlanningRecordRepository(
                OperationalStateStore(Path(directory) / "state.sqlite3"),
                ReceiptVerifier(),
            )
            plan_payload = plan_record()
            approval_payload = approval_record(plan_payload["plan_identity"])
            drifted_approval = copy.deepcopy(approval_payload)
            drifted_approval["policy_fingerprint"] = "sha256:" + "f" * 64
            statement = {
                key: value
                for key, value in drifted_approval.items()
                if key not in {"approval_identity", "grant_receipt"}
            }
            drifted_approval["grant_receipt"]["statement_fingerprint"] = (
                canonical_fingerprint(statement)
            )
            drifted_approval = identified(
                {
                    key: value
                    for key, value in drifted_approval.items()
                    if key != "approval_identity"
                },
                "approval_identity",
            )
            assessment_payload = assessment_record(plan_payload, drifted_approval)
            repository.put_plan(plan_payload)
            repository.put_approval(drifted_approval)
            with self.assertRaisesRegex(ValueError, "inapplicable"):
                repository.put_assessment(assessment_payload)
            self.assertIsNone(
                repository.assessment(assessment_payload["assessment_identity"])
            )

    def test_assessment_rejects_applicable_approval_marked_inapplicable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = PlanningRecordRepository(
                OperationalStateStore(Path(directory) / "state.sqlite3"),
                ReceiptVerifier(),
            )
            plan_payload = plan_record()
            approval_payload = approval_record(plan_payload["plan_identity"])
            assessment_payload = assessment_record(plan_payload, approval_payload)
            policy_assessment = assessment_payload["policy_assessment"]
            policy_assessment["disposition"] = "approval_required"
            rule_evaluation = policy_assessment["rule_evaluations"][0]
            rule_evaluation["result"] = "approval_required"
            rule_evaluation["approval_identity"] = None
            policy_assessment["approval_evaluation"]["evaluations"][0]["value"] = (
                "inapplicable"
            )
            assessment_payload = identified(
                {
                    key: value
                    for key, value in assessment_payload.items()
                    if key != "assessment_identity"
                },
                "assessment_identity",
            )
            repository.put_plan(plan_payload)
            repository.put_approval(approval_payload)

            with self.assertRaisesRegex(
                ValueError, "applicable Plan Approval inapplicable"
            ):
                repository.put_assessment(assessment_payload)
            self.assertIsNone(
                repository.assessment(assessment_payload["assessment_identity"])
            )

    def test_assessment_preserves_submicrosecond_approval_ordering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = PlanningRecordRepository(
                OperationalStateStore(Path(directory) / "state.sqlite3"),
                ReceiptVerifier(),
            )
            plan_payload = plan_record()
            approval_payload = approval_record(plan_payload["plan_identity"])
            approval_payload["granted_at"] = "2026-07-20T21:46:00.1234568Z"
            approval_payload["valid_until"] = "2026-07-20T22:46:00Z"
            statement = {
                key: value
                for key, value in approval_payload.items()
                if key not in {"approval_identity", "grant_receipt"}
            }
            approval_payload["grant_receipt"]["statement_fingerprint"] = (
                canonical_fingerprint(statement)
            )
            approval_payload["approval_identity"] = canonical_fingerprint(
                {
                    key: value
                    for key, value in approval_payload.items()
                    if key != "approval_identity"
                }
            )
            assessment_payload = assessment_record(plan_payload, approval_payload)
            assessment_payload["evaluation"]["evaluated_at"] = (
                "2026-07-20T21:46:00.1234567Z"
            )
            assessment_payload["assessment_identity"] = canonical_fingerprint(
                {
                    key: value
                    for key, value in assessment_payload.items()
                    if key != "assessment_identity"
                }
            )
            repository.put_plan(plan_payload)
            repository.put_approval(approval_payload)

            with self.assertRaisesRegex(
                ValueError, "inapplicable Plan Approval applicable"
            ):
                repository.put_assessment(assessment_payload)

    def test_current_pointer_allows_one_exact_concurrent_successor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            repository = PlanningRecordRepository(
                OperationalStateStore(state_path), ReceiptVerifier()
            )
            plan_payload = plan_record()
            approval_payload = approval_record(plan_payload["plan_identity"])
            assessment_payload = assessment_record(plan_payload, approval_payload)
            first_payload = pointer_record(plan_payload, assessment_payload)
            repository.put_plan(plan_payload)
            repository.put_approval(approval_payload)
            repository.put_assessment(assessment_payload)
            first = repository.compare_and_put_current(first_payload)
            barrier = Barrier(2)

            def replace(index: int) -> str:
                candidate = {
                    **first_payload,
                    "pointer_version": 2,
                    "expected_predecessor_identity": first.pointer_identity,
                    "updated_at": f"2026-07-20T21:49:0{index}Z",
                }
                candidate = identified(
                    {
                        key: value
                        for key, value in candidate.items()
                        if key != "pointer_identity"
                    },
                    "pointer_identity",
                )
                contender = PlanningRecordRepository(
                    OperationalStateStore(state_path), ReceiptVerifier()
                )
                barrier.wait()
                try:
                    contender.compare_and_put_current(candidate)
                except PlanningRecordConflictError:
                    return "rejected"
                return "stored"

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = tuple(pool.map(replace, range(2)))

            self.assertEqual(outcomes.count("stored"), 1)
            self.assertEqual(outcomes.count("rejected"), 1)

            stale = {
                **first_payload,
                "pointer_version": 2,
                "expected_predecessor_identity": first.pointer_identity,
                "updated_at": "2026-07-20T21:49:09Z",
            }
            stale = identified(
                {
                    key: value
                    for key, value in stale.items()
                    if key != "pointer_identity"
                },
                "pointer_identity",
            )
            with self.assertRaises(PlanningRecordConflictError):
                repository.compare_and_put_current(stale)

    def test_current_pointer_allows_scope_version_change_in_same_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = PlanningRecordRepository(
                OperationalStateStore(Path(directory) / "state.sqlite3"),
                ReceiptVerifier(),
            )
            first_plan = plan_record()
            first_approval = approval_record(first_plan["plan_identity"])
            first_assessment = assessment_record(first_plan, first_approval)
            first_pointer = pointer_record(first_plan, first_assessment)
            repository.put_plan(first_plan)
            repository.put_approval(first_approval)
            repository.put_assessment(first_assessment)
            first = repository.compare_and_put_current(first_pointer)

            successor_plan = copy.deepcopy(first_plan)
            successor_plan["scope"]["fingerprint"] = "sha256:" + "f" * 64
            successor_plan = identified(
                {
                    key: value
                    for key, value in successor_plan.items()
                    if key != "plan_identity"
                },
                "plan_identity",
            )
            successor_approval = approval_record(successor_plan["plan_identity"])
            successor_assessment = assessment_record(successor_plan, successor_approval)
            successor_pointer = pointer_record(successor_plan, successor_assessment)
            successor_pointer.update(
                {
                    "pointer_version": 2,
                    "expected_predecessor_identity": first.pointer_identity,
                    "updated_at": "2026-07-20T21:49:10Z",
                }
            )
            successor_pointer = identified(
                {
                    key: value
                    for key, value in successor_pointer.items()
                    if key != "pointer_identity"
                },
                "pointer_identity",
            )
            repository.put_plan(successor_plan)
            repository.put_approval(successor_approval)
            repository.put_assessment(successor_assessment)

            successor = repository.compare_and_put_current(successor_pointer)

            self.assertEqual(successor.scope_identity, first.scope_identity)
            self.assertNotEqual(successor.scope.fingerprint, first.scope.fingerprint)
            self.assertEqual(repository.current(first.scope_identity), successor)


if __name__ == "__main__":
    unittest.main()
