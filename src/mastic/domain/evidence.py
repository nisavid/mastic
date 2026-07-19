"""Evidence-qualified compatibility and revision-scoped trust."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .resources import ModelRevision


class EvidenceState(StrEnum):
    UNKNOWN = "unknown"
    REPORTED = "reported"
    DECLARED = "declared"
    OBSERVED = "observed"
    DERIVED = "derived"
    VALIDATED = "validated"
    CONFLICTING = "conflicting"


class TrustDecision(StrEnum):
    GRANTED = "granted"
    NOT_GRANTED = "not_granted"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True, slots=True)
class Evidence:
    source: str
    state: EvidenceState
    detail: str

    def __post_init__(self) -> None:
        if not self.source or not self.detail:
            raise ValueError("evidence source and detail are required")


@dataclass(frozen=True, slots=True)
class CompatibilityAssessment:
    model_revision: ModelRevision
    runtime_installation: str
    option_fingerprint: str
    machine_fingerprint: str
    evidence: tuple[Evidence, ...] = ()

    @property
    def state(self) -> EvidenceState:
        if not self.evidence:
            return EvidenceState.UNKNOWN
        order = {
            EvidenceState.UNKNOWN: 0,
            EvidenceState.REPORTED: 1,
            EvidenceState.DECLARED: 2,
            EvidenceState.OBSERVED: 3,
            EvidenceState.DERIVED: 4,
            EvidenceState.VALIDATED: 5,
            EvidenceState.CONFLICTING: 6,
        }
        return max((item.state for item in self.evidence), key=order.__getitem__)


@dataclass(frozen=True, slots=True)
class TrustGrant:
    model_revision: ModelRevision
    runtime_installation: str
    accepted_risks: frozenset[str]

    _FORBIDDEN = frozenset({"known_security_finding", "integrity_mismatch"})

    def __post_init__(self) -> None:
        object.__setattr__(self, "accepted_risks", frozenset(self.accepted_risks))

    def decide(
        self,
        *,
        revision: ModelRevision,
        runtime_installation: str,
        requested_risks: frozenset[str],
    ) -> TrustDecision:
        if requested_risks & self._FORBIDDEN:
            return TrustDecision.FORBIDDEN
        if (
            revision != self.model_revision
            or runtime_installation != self.runtime_installation
            or not requested_risks.issubset(self.accepted_risks)
        ):
            return TrustDecision.NOT_GRANTED
        return TrustDecision.GRANTED
