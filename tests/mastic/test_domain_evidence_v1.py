import unittest

from mastic.domain.evidence import (
    CompatibilityAssessment,
    Evidence,
    EvidenceState,
    TrustDecision,
    TrustGrant,
)
from mastic.domain.resources import ModelRevision


class EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.revision = ModelRevision("mlx-community/Qwen", "b" * 40)

    def test_assessment_exposes_conflicting_evidence_and_provenance(self) -> None:
        assessment = CompatibilityAssessment(
            model_revision=self.revision,
            runtime_installation="optiq@0.2.15",
            option_fingerprint="mtp=true",
            machine_fingerprint="m3-max-128gb",
            evidence=(
                Evidence("model-card", EvidenceState.DECLARED, "MTP supported"),
                Evidence("runtime-probe", EvidenceState.CONFLICTING, "flag absent"),
            ),
        )

        self.assertEqual(assessment.state, EvidenceState.CONFLICTING)
        self.assertEqual(assessment.evidence[1].source, "runtime-probe")

    def test_observed_evidence_precedes_declarations_but_not_derived_results(
        self,
    ) -> None:
        observed = CompatibilityAssessment(
            model_revision=self.revision,
            runtime_installation="optiq@0.2.15",
            option_fingerprint="mtp=true",
            machine_fingerprint="m3-max-128gb",
            evidence=(
                Evidence("model-card", EvidenceState.DECLARED, "MTP supported"),
                Evidence("config", EvidenceState.OBSERVED, "MTP configured"),
            ),
        )
        derived = CompatibilityAssessment(
            model_revision=self.revision,
            runtime_installation="optiq@0.2.15",
            option_fingerprint="mtp=true",
            machine_fingerprint="m3-max-128gb",
            evidence=(
                *observed.evidence,
                Evidence("fit-policy", EvidenceState.DERIVED, "likely fits"),
            ),
        )

        self.assertEqual(observed.state, EvidenceState.OBSERVED)
        self.assertEqual(derived.state, EvidenceState.DERIVED)

    def test_trust_grant_is_exact_revision_and_runtime_scoped(self) -> None:
        grant = TrustGrant(
            model_revision=self.revision,
            runtime_installation="optiq@0.2.18",
            accepted_risks=frozenset({"remote_code"}),
        )

        self.assertEqual(
            grant.decide(
                revision=self.revision,
                runtime_installation="optiq@0.2.18",
                requested_risks=frozenset({"remote_code"}),
            ),
            TrustDecision.GRANTED,
        )
        self.assertEqual(
            grant.decide(
                revision=ModelRevision("mlx-community/Qwen", "c" * 40),
                runtime_installation="optiq@0.2.18",
                requested_risks=frozenset({"remote_code"}),
            ),
            TrustDecision.NOT_GRANTED,
        )

    def test_trust_grant_copies_accepted_risks_into_immutable_state(self) -> None:
        accepted_risks = {"remote_code"}
        grant = TrustGrant(
            model_revision=self.revision,
            runtime_installation="optiq@0.2.18",
            accepted_risks=accepted_risks,  # type: ignore[arg-type]
        )

        accepted_risks.add("repository_code")

        self.assertEqual(grant.accepted_risks, frozenset({"remote_code"}))
        hash(grant)
        self.assertEqual(
            grant.decide(
                revision=self.revision,
                runtime_installation="optiq@0.3.3",
                requested_risks=frozenset({"remote_code"}),
            ),
            TrustDecision.NOT_GRANTED,
        )

    def test_known_security_and_integrity_failures_cannot_be_granted(self) -> None:
        grant = TrustGrant(
            model_revision=self.revision,
            runtime_installation="optiq@0.2.18",
            accepted_risks=frozenset({"remote_code", "known_security_finding"}),
        )

        self.assertEqual(
            grant.decide(
                revision=self.revision,
                runtime_installation="optiq@0.2.18",
                requested_risks=frozenset({"known_security_finding"}),
            ),
            TrustDecision.FORBIDDEN,
        )
        self.assertEqual(
            grant.decide(
                revision=self.revision,
                runtime_installation="optiq@0.2.18",
                requested_risks=frozenset({"integrity_mismatch"}),
            ),
            TrustDecision.FORBIDDEN,
        )


if __name__ == "__main__":
    unittest.main()
