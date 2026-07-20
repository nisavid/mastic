import copy
import unittest

from mastic.domain.canonical import canonical_fingerprint
from mastic.domain.planning_records import (
    CurrentPlanPointer,
    Plan,
    PlanApproval,
    PlanAssessment,
)


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64
SHA_E = "sha256:" + "e" * 64
SHA_F = "sha256:" + "f" * 64


def identified(record, field):
    result = copy.deepcopy(record)
    result[field] = canonical_fingerprint(result)
    return result


def scope():
    return {
        "kind": "declared_scope",
        "id": "user:501/default",
        "fingerprint": SHA_A,
    }


def policy_record():
    value = {
        "id": "phase1-online-current",
        "version": 1,
        "input_requirements": [],
        "rules": [
            {
                "rule_id": "owner-upgrade-review",
                "result": "approval_required",
            }
        ],
        "candidate_selection": {
            "rule_id": "prefer-policy-ranked-eligible",
            "version": 1,
        },
    }
    value["fingerprint"] = canonical_fingerprint(value)
    return value


def policy_selection(policy_fingerprint):
    value = {
        "kind": "policy_selection",
        "id": "user:501/default:reconciliation",
        "scope_identity": canonical_fingerprint(
            {"kind": scope()["kind"], "id": scope()["id"]}
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


def target():
    value = {
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


def step(plan_target):
    value = {
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


def plan_record():
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
                    "capability_port": "external_application_installation_lifecycle",
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


def approval_record(plan_identity):
    policy_fingerprint = policy_record()["fingerprint"]
    statement = {
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
    statement_fingerprint = canonical_fingerprint(statement)
    return identified(
        {
            **statement,
            "grant_receipt": {
                "kind": "authenticated_grant_receipt",
                "verifier_id": "mastic-daemon-peer:v1",
                "statement_fingerprint": statement_fingerprint,
                "proof": "base64url:verified-by-test-authority",
            },
        },
        "approval_identity",
    )


def assessment_record(plan, approval):
    policy = policy_record()
    return identified(
        {
            "schema_version": 2,
            "kind": "mastic.plan-assessment",
            "plan": {
                "plan_identity": plan["plan_identity"],
                "blueprint_identity": plan["blueprint_identity"],
                "scope": plan["scope"],
                "purpose": plan["purpose"],
                "target_ids": [plan["targets"][0]["target_id"]],
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
                    "required_step_ids": [plan["required_steps"][0]["step_id"]],
                    "step_satisfactions": [],
                },
                "targets": [],
                "summary": {},
            },
            "issues": [],
        },
        "assessment_identity",
    )


def pointer_record(plan, assessment):
    return identified(
        {
            "schema_version": 2,
            "kind": "mastic.current-plan-pointer",
            "scope": plan["scope"],
            "scope_identity": canonical_fingerprint(
                {"kind": plan["scope"]["kind"], "id": plan["scope"]["id"]}
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


class PlanningRecordTests(unittest.TestCase):
    def test_exact_version_two_records_round_trip(self):
        plan_payload = plan_record()
        approval_payload = approval_record(plan_payload["plan_identity"])
        assessment_payload = assessment_record(plan_payload, approval_payload)
        pointer_payload = pointer_record(plan_payload, assessment_payload)

        plan = Plan.from_mapping(plan_payload)
        approval = PlanApproval.from_mapping(approval_payload)
        assessment = PlanAssessment.from_mapping(assessment_payload)
        pointer = CurrentPlanPointer.from_mapping(pointer_payload)

        self.assertEqual(plan.to_mapping(), plan_payload)
        self.assertEqual(plan.owner_upgrade_mutation.request.preview_fingerprint, SHA_A)
        self.assertEqual(
            approval.statement_fingerprint,
            approval_payload["grant_receipt"]["statement_fingerprint"],
        )
        self.assertEqual(
            assessment.applicable_approval_identities, (approval.approval_identity,)
        )
        self.assertEqual(pointer.plan_identity, plan.plan_identity)

    def test_every_record_rejects_version_one_wrong_kind_and_tampered_identity(self):
        plan_payload = plan_record()
        approval_payload = approval_record(plan_payload["plan_identity"])
        assessment_payload = assessment_record(plan_payload, approval_payload)
        pointer_payload = pointer_record(plan_payload, assessment_payload)

        for record_type, payload, identity_field in (
            (Plan, plan_payload, "plan_identity"),
            (PlanApproval, approval_payload, "approval_identity"),
            (PlanAssessment, assessment_payload, "assessment_identity"),
            (CurrentPlanPointer, pointer_payload, "pointer_identity"),
        ):
            with self.subTest(record=record_type.__name__, fault="version"):
                invalid = {**payload, "schema_version": 1}
                with self.assertRaisesRegex(ValueError, "version 2"):
                    record_type.from_mapping(invalid)
            with self.subTest(record=record_type.__name__, fault="kind"):
                invalid = {**payload, "kind": "setup_plan"}
                with self.assertRaisesRegex(ValueError, "kind"):
                    record_type.from_mapping(invalid)
            with self.subTest(record=record_type.__name__, fault="identity"):
                invalid = {**payload, identity_field: SHA_F}
                with self.assertRaisesRegex(ValueError, "identity"):
                    record_type.from_mapping(invalid)

    def test_plan_rejects_inexact_owner_upgrade_mutation_and_relationships(self):
        original = plan_record()
        cases = []
        extra_request = copy.deepcopy(original)
        extra_request["mutations"][0]["request"]["force"] = True
        cases.append(extra_request)
        wrong_target = copy.deepcopy(original)
        wrong_target["mutations"][0]["target_id"] = "application-installation:other"
        cases.append(wrong_target)
        missing_binding = copy.deepcopy(original)
        del missing_binding["mutations"][0]["request"]["preview_fingerprint"]
        cases.append(missing_binding)
        wrong_target_fingerprint = copy.deepcopy(original)
        wrong_target_fingerprint["targets"][0]["plan_target_fingerprint"] = SHA_F
        cases.append(wrong_target_fingerprint)

        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                payload["plan_identity"] = canonical_fingerprint(
                    {
                        key: value
                        for key, value in payload.items()
                        if key != "plan_identity"
                    }
                )
                with self.assertRaises(ValueError):
                    Plan.from_mapping(payload)

    def test_approval_rejects_malformed_statement_receipt_and_rule_sets(self):
        payload = approval_record(plan_record()["plan_identity"])
        cases = []
        wrong_statement = copy.deepcopy(payload)
        wrong_statement["grant_receipt"]["statement_fingerprint"] = SHA_F
        cases.append(wrong_statement)
        overlapping_rules = copy.deepcopy(payload)
        overlapping_rules["override_rule_ids"] = ["owner-upgrade-review"]
        cases.append(overlapping_rules)
        invalid_window = copy.deepcopy(payload)
        invalid_window["valid_until"] = invalid_window["granted_at"]
        cases.append(invalid_window)

        for index, candidate in enumerate(cases):
            with self.subTest(index=index):
                candidate["approval_identity"] = canonical_fingerprint(
                    {
                        key: value
                        for key, value in candidate.items()
                        if key != "approval_identity"
                    }
                )
                with self.assertRaises(ValueError):
                    PlanApproval.from_mapping(candidate)

    def test_assessment_requires_eligible_exact_applicable_approval(self):
        plan = plan_record()
        approval = approval_record(plan["plan_identity"])
        original = assessment_record(plan, approval)
        cases = []
        ineligible = copy.deepcopy(original)
        ineligible["policy_assessment"]["disposition"] = "approval_required"
        cases.append(ineligible)
        inapplicable = copy.deepcopy(original)
        inapplicable["policy_assessment"]["approval_evaluation"]["evaluations"][0][
            "value"
        ] = "inapplicable"
        cases.append(inapplicable)
        unreferenced = copy.deepcopy(original)
        unreferenced["policy_assessment"]["rule_evaluations"][0][
            "approval_identity"
        ] = SHA_F
        cases.append(unreferenced)

        for index, candidate in enumerate(cases):
            with self.subTest(index=index):
                candidate["assessment_identity"] = canonical_fingerprint(
                    {
                        key: value
                        for key, value in candidate.items()
                        if key != "assessment_identity"
                    }
                )
                with self.assertRaises(ValueError):
                    PlanAssessment.from_mapping(candidate)

    def test_current_pointer_enforces_first_and_successor_shapes(self):
        plan = plan_record()
        approval = approval_record(plan["plan_identity"])
        assessment = assessment_record(plan, approval)
        first = pointer_record(plan, assessment)

        bad_first = {**first, "expected_predecessor_identity": SHA_A}
        bad_first["pointer_identity"] = canonical_fingerprint(
            {
                key: value
                for key, value in bad_first.items()
                if key != "pointer_identity"
            }
        )
        with self.assertRaisesRegex(ValueError, "predecessor"):
            CurrentPlanPointer.from_mapping(bad_first)

        successor = {
            **first,
            "pointer_version": 2,
            "expected_predecessor_identity": first["pointer_identity"],
            "updated_at": "2026-07-20T21:49:00Z",
        }
        successor["pointer_identity"] = canonical_fingerprint(
            {
                key: value
                for key, value in successor.items()
                if key != "pointer_identity"
            }
        )
        self.assertEqual(CurrentPlanPointer.from_mapping(successor).pointer_version, 2)

        bad_successor = {**successor, "expected_predecessor_identity": None}
        bad_successor["pointer_identity"] = canonical_fingerprint(
            {
                key: value
                for key, value in bad_successor.items()
                if key != "pointer_identity"
            }
        )
        with self.assertRaisesRegex(ValueError, "predecessor"):
            CurrentPlanPointer.from_mapping(bad_successor)


if __name__ == "__main__":
    unittest.main()
