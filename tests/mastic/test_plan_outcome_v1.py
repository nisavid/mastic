from __future__ import annotations

import json
import unittest

from mastic.application.plan_outcome import (
    ApplicationTargetObservation,
    PlanAssessmentInput,
    PlanOutcomePolicy,
    PlanStep,
)
from mastic.application.setup import (
    Completion,
    Readiness,
    SetupEvidence,
    StepState,
)
from mastic.infrastructure.application_target_canaries import (
    application_canary_evidence_sha256,
)


class PlanOutcomePolicyTests(unittest.TestCase):
    def test_performance_policy_must_be_complete_at_composition(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "performance profile id must be a nonempty string"
        ):
            PlanOutcomePolicy({})

    def test_missing_selected_target_evidence_is_pending_and_partial(self) -> None:
        policy = PlanOutcomePolicy(_provisional_performance_profile())
        plan = PlanAssessmentInput(
            steps=(
                PlanStep("preflight", "preflight-v1"),
                PlanStep("application.canary.codex", "canary-v1"),
            ),
            application_targets=("codex",),
        )
        preflight = SetupEvidence("preflight", "preflight-v1", StepState.COMPLETE, "{}")

        outcome = policy.assess(plan, (preflight,))

        self.assertIs(outcome.completion, Completion.PARTIAL)
        self.assertIs(outcome.readiness, Readiness.PENDING)
        self.assertEqual(
            outcome.application_target_readiness,
            {"codex": Readiness.PENDING},
        )
        self.assertEqual(outcome.reusable_evidence, (preflight,))

    def test_malformed_canary_evidence_fails_closed(self) -> None:
        policy = PlanOutcomePolicy(_provisional_performance_profile())
        plan = PlanAssessmentInput(
            steps=(PlanStep("application.canary.codex", "canary-v1"),),
            application_targets=("codex",),
        )
        malformed = SetupEvidence(
            "application.canary.codex",
            "canary-v1",
            StepState.COMPLETE,
            '{"result":{"ok":false}}',
        )

        outcome = policy.assess(plan, (malformed,))

        self.assertIs(outcome.completion, Completion.PARTIAL)
        self.assertIs(outcome.readiness, Readiness.UNVERIFIED)
        self.assertEqual(
            outcome.application_target_readiness,
            {"codex": Readiness.UNVERIFIED},
        )
        self.assertEqual(outcome.reusable_evidence, ())

    def test_non_string_canary_band_fails_closed(self) -> None:
        policy = PlanOutcomePolicy(_provisional_performance_profile())
        step = PlanStep("application.canary.codex", "canary-v1")
        plan = PlanAssessmentInput(
            steps=(step,),
            application_targets=("codex",),
            performance_binding=_performance_binding(),
        )
        malformed = SetupEvidence(
            step.id,
            step.fingerprint,
            StepState.COMPLETE,
            json.dumps(
                {
                    "result": {
                        "profile": "coding",
                        "service": "coding",
                        "ok": True,
                        "exact_contract": True,
                        "phases": ("codex.exec", "responses.exact"),
                        "evidence_sha256": application_canary_evidence_sha256(
                            target="codex",
                            profile="coding",
                            service="coding",
                            phases=("codex.exec", "responses.exact"),
                            exact_contract=True,
                        ),
                        "performance": {
                            "metric": "codex.native_canary.duration_seconds",
                            "value": 12.0,
                            "unit": "seconds",
                            "band": ["unverified"],
                            "profile_id": "phase1-qwen36-optiq-apple-silicon",
                            "profile_version": 1,
                        },
                    }
                }
            ),
        )

        outcome = policy.assess(plan, (malformed,))

        self.assertIs(outcome.completion, Completion.PARTIAL)
        self.assertIs(outcome.readiness, Readiness.UNVERIFIED)
        self.assertEqual(outcome.reusable_evidence, ())

    def test_targetless_verification_evidence_is_ready(self) -> None:
        policy = PlanOutcomePolicy(_provisional_performance_profile())
        plan = PlanAssessmentInput(steps=(PlanStep("verify.request", "verify-v1"),))
        verified = SetupEvidence(
            "verify.request",
            "verify-v1",
            StepState.COMPLETE,
            (
                '{"result":{"ok":true,"response_sha256":'
                '"8d3b1f10b22a30a4a9d48bff9d603d8742e527d8a34dbe5a69413b6e49919d7d"}}'
            ),
        )

        outcome = policy.assess(plan, (verified,))

        self.assertIs(outcome.completion, Completion.COMPLETE)
        self.assertIs(outcome.readiness, Readiness.READY)
        self.assertEqual(outcome.application_target_readiness, {})
        self.assertEqual(outcome.reusable_evidence, (verified,))

    def test_mismatched_runtime_material_is_not_reusable_or_complete(self) -> None:
        policy = PlanOutcomePolicy(_provisional_performance_profile())
        plan = PlanAssessmentInput(
            steps=(
                PlanStep(
                    "runtime.install",
                    "runtime-v1",
                    expected_result={
                        "runtime": "optiq",
                        "version": "0.3.3",
                        "provenance": "tested",
                        "lock_sha256": "a" * 64,
                    },
                ),
            ),
        )
        wrong_runtime = SetupEvidence(
            "runtime.install",
            "runtime-v1",
            StepState.COMPLETE,
            json.dumps(
                {
                    "result": {
                        "installation_id": "optiq-0.3.2",
                        "runtime": "optiq",
                        "version": "0.3.2",
                        "provenance": "tested",
                        "bundle_id": "old",
                        "lock_sha256": "b" * 64,
                    }
                }
            ),
        )

        outcome = policy.assess(plan, (wrong_runtime,))

        self.assertIs(outcome.completion, Completion.PARTIAL)
        self.assertEqual(outcome.reusable_evidence, ())

    def test_target_drift_changes_readiness_without_changing_completion(self) -> None:
        policy = PlanOutcomePolicy(_provisional_performance_profile())
        skipped = PlanStep("application.canary.codex", "canary-v1", StepState.SKIPPED)
        plan = PlanAssessmentInput(
            steps=(skipped,),
            application_targets=("codex",),
            performance_binding=_performance_binding(),
        )
        evidence = SetupEvidence(
            skipped.id,
            skipped.fingerprint,
            StepState.SKIPPED,
        )
        drift = ApplicationTargetObservation(
            "codex",
            "drifted",
            "managed state changed",
            ("mastic application-target configure codex --help",),
        )

        outcome = policy.assess(plan, (evidence,), (drift,))

        self.assertIs(outcome.completion, Completion.COMPLETE)
        self.assertIs(outcome.readiness, Readiness.UNVERIFIED)
        self.assertEqual(
            outcome.application_target_readiness,
            {"codex": Readiness.UNVERIFIED},
        )
        self.assertEqual(len(outcome.application_target_issues), 1)
        issue = outcome.application_target_issues[0]
        self.assertEqual(issue.code, "application_target_drifted")
        self.assertEqual(issue.application_target, "codex")
        self.assertEqual(issue.message, "managed state changed")
        self.assertEqual(
            issue.next_actions,
            ("mastic application-target configure codex --help",),
        )

    def test_canary_requires_the_profile_application_versions(self) -> None:
        policy = PlanOutcomePolicy(_provisional_performance_profile())
        skipped = PlanStep("application.canary.codex", "canary-v1", StepState.SKIPPED)
        binding = _performance_binding()
        binding["application_versions"] = {"codex": "future"}
        plan = PlanAssessmentInput(
            steps=(skipped,),
            application_targets=("codex",),
            performance_binding=binding,
        )
        evidence = SetupEvidence(
            skipped.id,
            skipped.fingerprint,
            StepState.SKIPPED,
        )

        outcome = policy.assess(plan, (evidence,))

        self.assertIs(outcome.completion, Completion.PARTIAL)
        self.assertIs(outcome.readiness, Readiness.UNVERIFIED)
        self.assertEqual(outcome.reusable_evidence, ())

    def test_provisional_policy_builds_unverified_canary_performance(self) -> None:
        policy = PlanOutcomePolicy(_provisional_performance_profile())
        plan = PlanAssessmentInput(steps=(), performance_binding=_performance_binding())

        performance = policy.canary_performance(plan, "codex", 12.5)

        self.assertEqual(
            performance,
            {
                "metric": "codex.native_canary.duration_seconds",
                "value": 12.5,
                "unit": "seconds",
                "band": "unverified",
                "profile_id": "phase1-qwen36-optiq-apple-silicon",
                "profile_version": 1,
            },
        )


def _provisional_performance_profile() -> dict[str, object]:
    return {
        "id": "phase1-qwen36-optiq-apple-silicon",
        "version": 1,
        "status": "provisional",
        "host": {
            "platform": "darwin",
            "machine": "arm64",
            "minimum_memory_bytes": 48 * 1024**3,
            "macos_major_versions": (15, 26),
        },
        "plan": {
            "selection_sha256": "a" * 64,
            "application_versions": {
                "codex": "0.144.1",
                "hindsight": "0.8.4",
            },
        },
        "metrics": {
            "codex.native_canary.duration_seconds": {
                "unit": "seconds",
                "expected": {"maximum": 60.0},
            },
            "hindsight.native_canary.duration_seconds": {
                "unit": "seconds",
                "expected": {"maximum": 180.0},
            },
        },
    }


def _performance_binding() -> dict[str, object]:
    return {
        "selection_sha256": "a" * 64,
        "application_versions": {
            "codex": "0.144.1",
            "hindsight": "0.8.4",
        },
        "platform": "darwin",
        "machine": "arm64",
        "memory_bytes": 96 * 1024**3,
        "macos_major": 26,
        "service": "coding",
    }


if __name__ == "__main__":
    unittest.main()
