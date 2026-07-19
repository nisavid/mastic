"""Resource-fit admission and critical-pressure policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FitClass(StrEnum):
    LIKELY = "likely"
    BORDERLINE = "borderline"
    NO_FIT = "no_fit"
    UNKNOWN = "unknown"


class AdmissionDecision(StrEnum):
    START = "start"
    CONFIRM = "confirm"
    TRANSITION_SEQUENCE = "transition_sequence"


class PressureLevel(StrEnum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"


class PressureAction(StrEnum):
    SHED_NEW_WORK = "shed_new_work"
    STOP_LRU_IDLE = "stop_lru_idle"
    PRESENT_STOP_SEQUENCE = "present_stop_sequence"


@dataclass(frozen=True, slots=True)
class FitAssessment:
    classification: FitClass
    projected_gib: int | float | None
    available_gib: int | float
    assumptions: tuple[str, ...]

    @property
    def decision(self) -> AdmissionDecision:
        if self.classification is FitClass.LIKELY:
            return AdmissionDecision.START
        if self.classification in {FitClass.BORDERLINE, FitClass.UNKNOWN}:
            return AdmissionDecision.CONFIRM
        return AdmissionDecision.TRANSITION_SEQUENCE

    def approve_transition(self, transitions: tuple[str, ...]) -> tuple[str, ...]:
        if self.classification is FitClass.NO_FIT and not transitions:
            raise ValueError("no-fit admission requires a named transition sequence")
        return transitions


@dataclass(frozen=True, slots=True)
class RunningService:
    name: str
    pinned: bool
    busy: bool
    last_used_ns: int


@dataclass(frozen=True, slots=True)
class PressureResult:
    actions: tuple[PressureAction, ...]
    stop_services: tuple[str, ...] = ()
    operator_stop_sequence: tuple[str, ...] = ()


class PressurePolicy:
    """Choose only reversible, explainable actions under memory pressure."""

    def evaluate(
        self, level: PressureLevel, services: tuple[RunningService, ...]
    ) -> PressureResult:
        if level is not PressureLevel.CRITICAL:
            return PressureResult(())
        idle_unpinned = sorted(
            (item for item in services if not item.pinned and not item.busy),
            key=lambda item: item.last_used_ns,
        )
        if idle_unpinned:
            return PressureResult(
                (PressureAction.SHED_NEW_WORK, PressureAction.STOP_LRU_IDLE),
                tuple(item.name for item in idle_unpinned),
            )
        if not services:
            return PressureResult((PressureAction.SHED_NEW_WORK,))
        sequence = tuple(
            item.name
            for item in sorted(
                services, key=lambda item: (item.pinned, item.last_used_ns)
            )
        )
        return PressureResult(
            (PressureAction.SHED_NEW_WORK, PressureAction.PRESENT_STOP_SEQUENCE),
            operator_stop_sequence=sequence,
        )
