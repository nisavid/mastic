import copy
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mastic.application.external_application_lifecycle import (
    AuthorizedOwnerUpgrade,
    OwnerUpgradePreview,
    VerifiedArtifact,
    VerifiedArtifactClosure,
)
from mastic.domain.canonical import canonical_fingerprint
from mastic.domain.external_applications import (
    ExternalApplicationInstallation,
    InstallationObservation,
    ReleaseIntent,
)
from mastic.domain.planning_records import (
    CurrentPlanPointer,
    Plan,
    PlanApproval,
    PlanAssessment,
)
from mastic.infrastructure.planning_owner_upgrade_authorization import (
    PlanningRecordOwnerUpgradeAuthorizationVerifier,
    StaticPlanningPolicyRegistry,
    TrustedPlanningPolicy,
)
from mastic.infrastructure.codex_vite_lifecycle import CodexViteOwnerLifecycle
from mastic.infrastructure.planning_record_repository import PlanningRecordRepository
from mastic.infrastructure.state_store import OperationalStateStore


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


def assessment_issues(plan):
    target_id = plan["targets"][0]["target_id"]
    return sorted(
        (
            identified(
                {
                    "category": "observation",
                    "code": code,
                    "scope": {"kind": "target", "id": target_id},
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


def operational_assessment(plan, issues):
    target = plan["targets"][0]
    lifecycle_issue = next(
        item for item in issues if item["code"] == "lifecycle_not_observed"
    )
    condition_issue = next(
        item for item in issues if item["code"] == "condition_not_observed"
    )
    return {
        "completion": {
            "value": "partial",
            "required_step_ids": [plan["required_steps"][0]["step_id"]],
            "step_satisfactions": [],
        },
        "targets": [
            {
                "target_id": target["target_id"],
                "target_kind": target["target_kind"],
                "subject_fingerprint": target["subject_fingerprint"],
                "plan_target_fingerprint": target["plan_target_fingerprint"],
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
            "by_completion": {"partial": [target["target_id"]], "complete": []},
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
            "lifecycle_not_observed": [target["target_id"]],
            "lifecycle_not_applicable": [],
            "condition_not_observed": [target["target_id"]],
            "condition_not_applicable": [],
        },
    }


def assessment_record(plan, approval):
    policy = policy_record()
    issues = assessment_issues(plan)
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
            "operational_assessment": operational_assessment(plan, issues),
            "issues": issues,
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

    def test_plan_rejects_one_mutation_claimed_by_multiple_steps(self):
        payload = plan_record()
        duplicate_claim = copy.deepcopy(payload["required_steps"][0])
        duplicate_claim["step_id"] = "application.codex.verify-upgrade"
        duplicate_claim["step_fingerprint"] = canonical_fingerprint(
            {
                "purpose": payload["purpose"],
                "step": {
                    key: value
                    for key, value in duplicate_claim.items()
                    if key != "step_fingerprint"
                },
            }
        )
        payload["required_steps"].append(duplicate_claim)
        payload["plan_identity"] = canonical_fingerprint(
            {key: value for key, value in payload.items() if key != "plan_identity"}
        )

        with self.assertRaisesRegex(ValueError, "exactly one Plan step"):
            Plan.from_mapping(payload)

    def test_planning_records_accept_canonical_fractional_timestamp_precision(self):
        for fraction in ("1", "123456", "1234567", "123456789"):
            with self.subTest(fraction=fraction):
                payload = plan_record()
                payload["created_at"] = f"2026-07-20T21:45:00.{fraction}Z"
                payload["plan_identity"] = canonical_fingerprint(
                    {
                        key: value
                        for key, value in payload.items()
                        if key != "plan_identity"
                    }
                )

                self.assertEqual(
                    Plan.from_mapping(payload).created_at, payload["created_at"]
                )

    def test_approval_compares_submicrosecond_instants_without_losing_precision(self):
        payload = approval_record(plan_record()["plan_identity"])
        payload["granted_at"] = "2026-07-20T21:46:00.1234567Z"
        payload["valid_until"] = "2026-07-20T21:46:00.1234568Z"
        statement = {
            key: value
            for key, value in payload.items()
            if key not in {"approval_identity", "grant_receipt"}
        }
        payload["grant_receipt"]["statement_fingerprint"] = canonical_fingerprint(
            statement
        )
        payload["approval_identity"] = canonical_fingerprint(
            {key: value for key, value in payload.items() if key != "approval_identity"}
        )

        approval = PlanApproval.from_mapping(payload)

        self.assertEqual(approval.granted_at, payload["granted_at"])
        self.assertEqual(approval.valid_until, payload["valid_until"])

    def test_plan_rejects_noncanonical_fractional_timestamps(self):
        for created_at in (
            "2026-07-20T21:45:00.0Z",
            "2026-07-20T21:45:00.10Z",
            "2026-07-20T21:45:00.1234567890Z",
            "2026-07-20T21:45:00+00:00",
            "2026-07-20T21:45:00z",
        ):
            with self.subTest(created_at=created_at):
                payload = plan_record()
                payload["created_at"] = created_at
                payload["plan_identity"] = canonical_fingerprint(
                    {
                        key: value
                        for key, value in payload.items()
                        if key != "plan_identity"
                    }
                )

                with self.assertRaisesRegex(ValueError, "canonical UTC timestamp"):
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

    def test_assessment_rejects_incomplete_operational_and_evidence_closure(self):
        plan = plan_record()
        approval = approval_record(plan["plan_identity"])
        original = assessment_record(plan, approval)
        cases = []

        complete_without_evidence = copy.deepcopy(original)
        complete_without_evidence["operational_assessment"]["completion"]["value"] = (
            "complete"
        )
        cases.append(complete_without_evidence)

        missing_target = copy.deepcopy(original)
        missing_target["operational_assessment"]["targets"] = []
        cases.append(missing_target)

        contradictory_summary = copy.deepcopy(original)
        contradictory_summary["operational_assessment"]["summary"][
            "lifecycle_not_observed"
        ] = []
        cases.append(contradictory_summary)

        missing_issue = copy.deepcopy(original)
        missing_issue["operational_assessment"]["targets"][0]["lifecycle"][
            "issue_ids"
        ] = [SHA_F]
        cases.append(missing_issue)

        swapped_issues = copy.deepcopy(original)
        target_projection = swapped_issues["operational_assessment"]["targets"][0]
        lifecycle_ids = target_projection["lifecycle"]["issue_ids"]
        condition_ids = target_projection["operational_condition"]["issue_ids"]
        target_projection["lifecycle"]["issue_ids"] = condition_ids
        target_projection["operational_condition"]["issue_ids"] = lifecycle_ids
        cases.append(swapped_issues)

        nested_evidence = copy.deepcopy(original)
        nested_evidence["policy_assessment"]["rule_evaluations"][0]["evidence_ids"] = [
            SHA_A
        ]
        cases.append(nested_evidence)

        unsorted_issues = copy.deepcopy(original)
        unsorted_issues["issues"] = list(reversed(unsorted_issues["issues"]))
        cases.append(unsorted_issues)

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


class AcceptedReceiptVerifier:
    def verify(self, approval):
        return isinstance(approval, PlanApproval)


class InertDiscovery:
    def discover(self, *, selected_installation_identity, selected_release_channel):
        raise AssertionError("authorization material verification must not discover")


class InertArtifactVerifier:
    def prepare(self, closure, owner_runtime_identity):
        raise AssertionError("authorization material verification must not prepare")

    def verify_staged(self, closure):
        raise AssertionError("authorization material verification must not stage")

    def verify_installed(self, closure, observation):
        raise AssertionError("authorization material verification must not inspect")


def owner_upgrade_authorization_fixture(directory):
    selected = ExternalApplicationInstallation(
        application_identity="external-application:codex",
        installation_identity="application-installation:codex:vite",
        owner_identity="vite-plus/npm-global",
        release_intent=ReleaseIntent.current(channel="npm:latest"),
        platform="darwin",
        architecture="arm64",
    )
    artifacts = VerifiedArtifactClosure(
        application_identity=selected.application_identity,
        exact_release="0.150.0",
        artifacts=(
            VerifiedArtifact(
                role="primary",
                package_identity="@openai/codex",
                exact_release="0.150.0",
                coordinate="npm:@openai/codex@0.150.0",
                archive_digest="sha512:" + "a" * 128,
                installed_payload_digest=SHA_C,
                staged_path=Path("/private/tmp/staged/codex.tgz"),
            ),
            VerifiedArtifact(
                role="platform",
                package_identity="@openai/codex-darwin-arm64",
                exact_release="0.150.0-darwin-arm64",
                coordinate="npm:@openai/codex-darwin-arm64@0.150.0",
                archive_digest="sha512:" + "b" * 128,
                installed_payload_digest=SHA_D,
                staged_path=Path("/private/tmp/staged/platform.tgz"),
            ),
        ),
        staging_directory=Path("/private/tmp/staged"),
        cache_directory=Path("/private/tmp/staged/cache"),
    )
    owner_material_verifier = CodexViteOwnerLifecycle(
        vp_home=Path("/Users/test/.vite-plus"),
        discovery=InertDiscovery(),
        artifact_verifier=InertArtifactVerifier(),
        base_environment={"HOME": "/Users/test"},
    )
    observation = InstallationObservation(
        application_identity=selected.application_identity,
        installation_identity=selected.installation_identity,
        owner_identity=selected.owner_identity,
        owner_installation_identity=SHA_B,
        owner_runtime_identity="node:24.18.0",
        release_channel=selected.release_intent.channel,
        platform=selected.platform,
        architecture=selected.architecture,
        installed_release="0.144.5",
        installed_artifact_digest=SHA_F,
        active_invocation="/Users/test/.vite-plus/bin/codex",
        reachable_invocations=("/Users/test/.vite-plus/bin/codex",),
        observed_at=datetime(2026, 7, 20, 21, 45, tzinfo=UTC),
    )
    action = owner_material_verifier.preview_action(observation, artifacts)
    preview = OwnerUpgradePreview(
        application_identity=selected.application_identity,
        installation_identity=selected.installation_identity,
        plan_purpose="reconciliation",
        source_observation_fingerprint=SHA_A,
        source_state_fingerprint=SHA_F,
        owner_identity=selected.owner_identity,
        owner_installation_identity=SHA_B,
        owner_runtime_identity="node:24.18.0",
        release_channel=selected.release_intent.channel,
        platform=selected.platform,
        architecture=selected.architecture,
        source_release="0.144.5",
        target_release=artifacts.exact_release,
        target_artifact_digest="sha512:" + "a" * 128,
        resolved_target_fingerprint=SHA_C,
        candidate_fingerprint=SHA_D,
        policy_assessment_fingerprint=SHA_E,
        artifact_closure_fingerprint=artifacts.fingerprint,
        rollback_source_release="0.144.5",
        action=action,
    )
    plan_payload = plan_record()
    selected_target = plan_payload["targets"][0]
    selected_target["subject_fingerprint"] = selected.fingerprint
    selected_target["plan_target_fingerprint"] = canonical_fingerprint(
        {
            "purpose": "reconciliation",
            "target": {
                key: value
                for key, value in selected_target.items()
                if key != "plan_target_fingerprint"
            },
        }
    )
    selected_step = plan_payload["required_steps"][0]
    selected_step["plan_target_fingerprint"] = selected_target[
        "plan_target_fingerprint"
    ]
    selected_step["step_fingerprint"] = canonical_fingerprint(
        {
            "purpose": "reconciliation",
            "step": {
                key: value
                for key, value in selected_step.items()
                if key != "step_fingerprint"
            },
        }
    )
    mutation = plan_payload["mutations"][0]
    mutation["plan_target_fingerprint"] = selected_target["plan_target_fingerprint"]
    mutation["request"] = {
        "installation_identity": selected.installation_identity,
        "release_artifact_identity": artifacts.artifact("primary").fingerprint,
        "source_state_fingerprint": preview.source_state_fingerprint,
        "preview_fingerprint": preview.fingerprint,
        "owner_action_fingerprint": action.fingerprint,
        "artifact_closure_fingerprint": artifacts.fingerprint,
    }
    mutation["expected_current"] = {"fingerprint": preview.source_state_fingerprint}
    plan_payload["plan_identity"] = canonical_fingerprint(
        {key: value for key, value in plan_payload.items() if key != "plan_identity"}
    )
    approval_payload = approval_record(plan_payload["plan_identity"])
    assessment_payload = assessment_record(plan_payload, approval_payload)
    pointer_payload = pointer_record(plan_payload, assessment_payload)
    repository = PlanningRecordRepository(
        OperationalStateStore(Path(directory) / "state.sqlite3"),
        AcceptedReceiptVerifier(),
    )
    repository.put_plan(plan_payload)
    repository.put_approval(approval_payload)
    assessment = repository.put_assessment(assessment_payload)
    repository.compare_and_put_current(pointer_payload)
    evaluation = assessment.to_mapping()["evaluation"]
    policy_registry = StaticPlanningPolicyRegistry(
        (
            TrustedPlanningPolicy(
                scope_identity=assessment.scope.identity,
                purpose=assessment.purpose,
                policy_selection=evaluation["policy_selection"],
                policy=evaluation["policy"],
            ),
        )
    )
    authorization = AuthorizedOwnerUpgrade(
        plan_identity=plan_payload["plan_identity"],
        approval_identity=approval_payload["approval_identity"],
        assessment_identity=assessment_payload["assessment_identity"],
        preview_fingerprint=preview.fingerprint,
    )
    return (
        selected,
        preview,
        authorization,
        artifacts,
        repository,
        policy_registry,
        owner_material_verifier,
    )


class PlanningOwnerUpgradeAuthorizationTests(unittest.TestCase):
    def test_repository_failure_fails_authorization_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                selected,
                preview,
                authorization,
                artifacts,
                _repository,
                policies,
                owner_material_verifier,
            ) = owner_upgrade_authorization_fixture(directory)

            class FailingRepository:
                @staticmethod
                def plan(_identity):
                    raise RuntimeError("planning repository unavailable")

            verifier = PlanningRecordOwnerUpgradeAuthorizationVerifier(
                FailingRepository(),  # type: ignore[arg-type]
                policies,
                owner_material_verifier,
                maximum_assessment_age=timedelta(hours=1),
            )

            with self.assertLogs(
                "mastic.infrastructure.planning_owner_upgrade_authorization",
                level="WARNING",
            ) as captured:
                self.assertFalse(
                    verifier.verify(selected, preview, authorization, artifacts)
                )
                with verifier.hold_authorization(
                    selected, preview, authorization, artifacts
                ) as verified:
                    self.assertFalse(verified)
            self.assertEqual(len(captured.output), 2)
            self.assertTrue(all("RuntimeError" in line for line in captured.output))
            self.assertTrue(
                all(
                    "planning repository unavailable" not in line
                    for line in captured.output
                )
            )

    def test_authorization_lease_blocks_current_pointer_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                selected,
                preview,
                authorization,
                artifacts,
                repository,
                policies,
                owner_material_verifier,
            ) = owner_upgrade_authorization_fixture(directory)
            verifier = PlanningRecordOwnerUpgradeAuthorizationVerifier(
                repository,
                policies,
                owner_material_verifier,
                maximum_assessment_age=timedelta(hours=1),
                clock=lambda: datetime(2026, 7, 20, 21, 50, tzinfo=UTC),
            )
            plan = repository.plan(authorization.plan_identity)
            self.assertIsNotNone(plan)
            assert plan is not None
            current = repository.current(plan.scope.identity)
            self.assertIsNotNone(current)
            assert current is not None
            replacement = current.to_mapping()
            replacement.update(
                {
                    "pointer_version": current.pointer_version + 1,
                    "expected_predecessor_identity": current.pointer_identity,
                    "updated_at": "2026-07-20T21:51:00Z",
                }
            )
            replacement["pointer_identity"] = canonical_fingerprint(
                {
                    name: value
                    for name, value in replacement.items()
                    if name != "pointer_identity"
                }
            )
            attempting = threading.Event()
            finished = threading.Event()

            def replace_current() -> None:
                attempting.set()
                repository.compare_and_put_current(replacement)
                finished.set()

            with verifier.hold_authorization(
                selected, preview, authorization, artifacts
            ) as verified:
                self.assertTrue(verified)
                worker = threading.Thread(target=replace_current)
                worker.start()
                self.assertTrue(attempting.wait(1))
                self.assertFalse(finished.wait(0.1))

            self.assertTrue(finished.wait(1))
            worker.join(1)
            self.assertFalse(worker.is_alive())

    def test_current_exact_records_and_trusted_policy_authorize(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                selected,
                preview,
                authorization,
                artifacts,
                repository,
                policies,
                owner_material_verifier,
            ) = owner_upgrade_authorization_fixture(directory)
            verifier = PlanningRecordOwnerUpgradeAuthorizationVerifier(
                repository,
                policies,
                owner_material_verifier,
                maximum_assessment_age=timedelta(hours=1),
                clock=lambda: datetime(2026, 7, 20, 21, 50, tzinfo=UTC),
            )

            self.assertTrue(
                verifier.verify(selected, preview, authorization, artifacts)
            )
            self.assertFalse(
                owner_material_verifier.verify_authorization_material(
                    selected,
                    replace(
                        preview,
                        action=replace(
                            preview.action,
                            argv=preview.action.argv + ("--force",),
                        ),
                    ),
                    artifacts,
                )
            )
            self.assertFalse(
                owner_material_verifier.verify_authorization_material(
                    selected,
                    preview,
                    replace(
                        artifacts,
                        artifacts=(artifacts.artifact("primary"),),
                    ),
                )
            )
            for changed in (
                replace(preview, application_identity="external-application:other"),
                replace(
                    preview, installation_identity="application-installation:other"
                ),
                replace(preview, release_channel="npm:next"),
                replace(preview, platform="linux"),
                replace(preview, architecture="x86_64"),
            ):
                with self.subTest(changed=changed.fingerprint):
                    self.assertFalse(
                        owner_material_verifier.verify_authorization_material(
                            selected, changed, artifacts
                        )
                    )
            self.assertFalse(
                owner_material_verifier.verify_authorization_material(  # type: ignore[arg-type]
                    None, preview, artifacts
                )
            )

    def test_missing_stale_wrong_selection_or_untrusted_policy_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                selected,
                preview,
                authorization,
                artifacts,
                repository,
                policies,
                owner_material_verifier,
            ) = owner_upgrade_authorization_fixture(directory)
            current = PlanningRecordOwnerUpgradeAuthorizationVerifier(
                repository,
                policies,
                owner_material_verifier,
                maximum_assessment_age=timedelta(hours=1),
                clock=lambda: datetime(2026, 7, 20, 21, 50, tzinfo=UTC),
            )
            stale = PlanningRecordOwnerUpgradeAuthorizationVerifier(
                repository,
                policies,
                owner_material_verifier,
                maximum_assessment_age=timedelta(minutes=1),
                clock=lambda: datetime(2026, 7, 20, 21, 50, tzinfo=UTC),
            )
            future = PlanningRecordOwnerUpgradeAuthorizationVerifier(
                repository,
                policies,
                owner_material_verifier,
                maximum_assessment_age=timedelta(hours=1),
                clock=lambda: datetime(2026, 7, 20, 21, 46, 30, tzinfo=UTC),
            )
            untrusted = PlanningRecordOwnerUpgradeAuthorizationVerifier(
                repository,
                StaticPlanningPolicyRegistry(()),
                owner_material_verifier,
                maximum_assessment_age=timedelta(hours=1),
                clock=lambda: datetime(2026, 7, 20, 21, 50, tzinfo=UTC),
            )

            cases = (
                (
                    current,
                    selected,
                    preview,
                    replace(authorization, plan_identity=SHA_F),
                    artifacts,
                ),
                (
                    current,
                    replace(selected, owner_identity="vite-plus/global-package"),
                    preview,
                    authorization,
                    artifacts,
                ),
                (stale, selected, preview, authorization, artifacts),
                (future, selected, preview, authorization, artifacts),
                (untrusted, selected, preview, authorization, artifacts),
            )
            for verifier, selected_case, preview_case, auth_case, closure_case in cases:
                with self.subTest(verifier=type(verifier).__name__):
                    self.assertFalse(
                        verifier.verify(
                            selected_case, preview_case, auth_case, closure_case
                        )
                    )


if __name__ == "__main__":
    unittest.main()
