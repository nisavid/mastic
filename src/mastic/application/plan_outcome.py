"""Canonical Completion and Readiness reduction for an exact Plan."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Sequence

from mastic.application.application_targets import (
    APPLICATION_CANARY_CONTRACTS,
    application_canary_evidence_sha256,
)
from mastic.application.setup import (
    Completion,
    Readiness,
    SetupEvidence,
    StepState,
)


@dataclass(frozen=True, slots=True)
class PlanStep:
    id: str
    fingerprint: str
    state: StepState = StepState.READY
    expected_result: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class PlanAssessmentInput:
    """The exact Plan projection needed to assess its current outcome."""

    steps: tuple[PlanStep, ...]
    application_targets: tuple[str, ...] = ()
    performance_binding: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ApplicationTargetObservation:
    application_target: str
    state: str
    detail: str | None = None
    next_actions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApplicationTargetIssue:
    code: str
    application_target: str
    state: str
    message: str
    next_actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlanOutcome:
    completion: Completion
    readiness: Readiness
    application_target_readiness: Mapping[str, Readiness] = field(
        default_factory=lambda: MappingProxyType({})
    )
    reusable_evidence: tuple[SetupEvidence, ...] = ()
    application_target_issues: tuple[ApplicationTargetIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "application_target_readiness",
            MappingProxyType(dict(self.application_target_readiness)),
        )


class PlanOutcomePolicy:
    """Reduce one exact Plan and its evidence without performing I/O."""

    def __init__(self, performance_profile: Mapping[str, object]) -> None:
        _validate_performance_profile(performance_profile)
        self._performance_profile = performance_profile

    def assess(
        self,
        plan: PlanAssessmentInput,
        evidence: Sequence[SetupEvidence],
        observations: Sequence[ApplicationTargetObservation] = (),
    ) -> PlanOutcome:
        steps = {(step.id, step.fingerprint): step for step in plan.steps}
        matched = {
            (item.step_id, item.fingerprint): item
            for item in evidence
            if (item.step_id, item.fingerprint) in steps
            and item.state in {StepState.COMPLETE, StepState.SKIPPED}
        }
        reusable = tuple(
            item
            for item in evidence
            if (step := steps.get((item.step_id, item.fingerprint))) is not None
            and self._terminal_evidence_valid(plan, step, item)
        )
        current = {(item.step_id, item.fingerprint): item for item in reusable}
        completion = (
            Completion.COMPLETE
            if all((step.id, step.fingerprint) in current for step in plan.steps)
            else Completion.PARTIAL
        )
        target_readiness: dict[str, Readiness] = {}
        for target in plan.application_targets:
            step = next(
                step for step in plan.steps if step.id == f"application.canary.{target}"
            )
            evidence_item = matched.get((step.id, step.fingerprint))
            if evidence_item is None:
                target_readiness[target] = Readiness.PENDING
            elif evidence_item.state is StepState.SKIPPED:
                target_readiness[target] = Readiness.UNVERIFIED
            else:
                band = self._canary_band(plan, target, evidence_item)
                target_readiness[target] = (
                    Readiness.READY
                    if _performance_binding_matches(
                        plan.performance_binding, self._performance_profile
                    )
                    and band == "expected"
                    else (
                        Readiness.DEGRADED
                        if _performance_binding_matches(
                            plan.performance_binding, self._performance_profile
                        )
                        and band == Readiness.DEGRADED.value
                        else Readiness.UNVERIFIED
                    )
                )
        issues: list[ApplicationTargetIssue] = []
        for observation in observations:
            target = observation.application_target
            if target not in target_readiness or observation.state == "healthy":
                continue
            target_readiness[target] = Readiness.UNVERIFIED
            if observation.state in {
                "missing",
                "drifted",
                "incompatible",
                "malformed",
                "unmanaged",
            }:
                state = observation.state
                code = f"application_target_{state}"
                message = observation.detail or (
                    f"Application Configuration Target {target!r} is {state}."
                )
            else:
                state = "unknown"
                code = "application_target_observation_failed"
                message = f"Application Configuration Target {target!r} could not be inspected."
            issues.append(
                ApplicationTargetIssue(
                    code,
                    target,
                    state,
                    message,
                    observation.next_actions
                    or (f"mastic application-target inspect {target}",),
                )
            )
        if target_readiness:
            readiness = next(
                (
                    candidate
                    for candidate in (
                        Readiness.PENDING,
                        Readiness.UNVERIFIED,
                        Readiness.DEGRADED,
                        Readiness.READY,
                    )
                    if candidate in target_readiness.values()
                ),
                Readiness.UNVERIFIED,
            )
        else:
            verification = next(
                (step for step in plan.steps if step.id == "verify.request"), None
            )
            evidence_item = (
                matched.get((verification.id, verification.fingerprint))
                if verification is not None
                else None
            )
            readiness = (
                Readiness.PENDING
                if evidence_item is None
                else (
                    Readiness.READY
                    if _verification_ready(evidence_item)
                    else Readiness.UNVERIFIED
                )
            )
        return PlanOutcome(
            completion=completion,
            readiness=readiness,
            application_target_readiness=target_readiness,
            reusable_evidence=reusable,
            application_target_issues=tuple(issues),
        )

    def canary_performance(
        self,
        plan: PlanAssessmentInput,
        application_target: str,
        duration_seconds: float,
    ) -> Mapping[str, object]:
        if not math.isfinite(duration_seconds) or duration_seconds < 0:
            raise ValueError("canary duration must be finite and nonnegative")
        metric = f"{application_target}.native_canary.duration_seconds"
        return MappingProxyType(
            {
                "metric": metric,
                "value": duration_seconds,
                "unit": "seconds",
                "band": self._performance_band(plan, metric, duration_seconds),
                "profile_id": self._performance_profile.get("id"),
                "profile_version": self._performance_profile.get("version"),
            }
        )

    def _terminal_evidence_valid(
        self,
        plan: PlanAssessmentInput,
        step: PlanStep,
        evidence: SetupEvidence,
    ) -> bool:
        if evidence.state not in {StepState.COMPLETE, StepState.SKIPPED}:
            return False
        if not _resumable_material_valid(step, evidence):
            return False
        if step.id.startswith("application.canary."):
            if not _performance_binding_valid(
                plan.performance_binding, self._performance_profile
            ):
                return False
            if evidence.state is StepState.SKIPPED:
                return step.state is StepState.SKIPPED
            target = step.id.removeprefix("application.canary.")
            return self._canary_band(plan, target, evidence) is not None
        if step.id == "verify.request":
            return _verification_ready(evidence)
        return evidence.state is StepState.COMPLETE

    def _canary_band(
        self,
        plan: PlanAssessmentInput,
        target: str,
        evidence: SetupEvidence,
    ) -> str | None:
        try:
            payload = json.loads(evidence.detail)
            result = payload["result"]
        except (KeyError, TypeError, json.JSONDecodeError):
            return None
        if not isinstance(result, Mapping):
            return None
        contract = APPLICATION_CANARY_CONTRACTS.get(target)
        service = result.get("service")
        expected_service = (
            plan.performance_binding.get("service")
            if isinstance(plan.performance_binding, Mapping)
            else None
        )
        raw_phases = result.get("phases")
        if (
            contract is None
            or not isinstance(service, str)
            or not service
            or service != expected_service
            or result.get("ok") is not True
            or result.get("exact_contract") is not True
            or result.get("profile") != contract.profile
            or not isinstance(raw_phases, Sequence)
            or isinstance(raw_phases, str | bytes)
            or tuple(raw_phases) != contract.phases
            or result.get("evidence_sha256")
            != application_canary_evidence_sha256(
                target=target,
                profile=contract.profile,
                service=service,
                phases=contract.phases,
                exact_contract=True,
            )
        ):
            return None
        performance = result.get("performance")
        if not isinstance(performance, Mapping):
            return None
        value = performance.get("value")
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            return None
        metric = f"{target}.native_canary.duration_seconds"
        profile_band = self._profile_band(metric, float(value))
        plan_band = self._performance_band(plan, metric, float(value))
        claimed_band = performance.get("band")
        if (
            performance.get("metric") != metric
            or performance.get("unit") != "seconds"
            or performance.get("value") != float(value)
            or performance.get("profile_id") != self._performance_profile.get("id")
            or performance.get("profile_version")
            != self._performance_profile.get("version")
            or not isinstance(claimed_band, str)
            or claimed_band not in {profile_band, plan_band}
        ):
            return None
        return claimed_band

    def _performance_band(
        self, plan: PlanAssessmentInput, metric: str, value: float
    ) -> str:
        if self._performance_profile.get(
            "status"
        ) != "validated" or not _performance_binding_matches(
            plan.performance_binding, self._performance_profile
        ):
            return Readiness.UNVERIFIED.value
        return self._profile_band(metric, value)

    def _profile_band(self, metric: str, value: float) -> str:
        if self._performance_profile.get("status") != "validated":
            return Readiness.UNVERIFIED.value
        metrics = self._performance_profile.get("metrics")
        if not isinstance(metrics, Mapping):
            return Readiness.UNVERIFIED.value
        raw_band = metrics.get(metric)
        expected = raw_band.get("expected") if isinstance(raw_band, Mapping) else None
        maximum = expected.get("maximum") if isinstance(expected, Mapping) else None
        if type(maximum) not in {int, float}:
            return Readiness.UNVERIFIED.value
        return "expected" if value <= float(maximum) else Readiness.DEGRADED.value


def _verification_ready(evidence: SetupEvidence) -> bool:
    if evidence.state is not StepState.COMPLETE:
        return False
    try:
        payload = json.loads(evidence.detail)
        result = payload["result"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(result, Mapping)
        and result.get("ok") is True
        and result.get("response_sha256")
        == "8d3b1f10b22a30a4a9d48bff9d603d8742e527d8a34dbe5a69413b6e49919d7d"
    )


def _resumable_material_valid(step: PlanStep, evidence: SetupEvidence) -> bool:
    if step.id not in {"runtime.install", "model.install"}:
        return True
    expected = step.expected_result
    if not isinstance(expected, Mapping):
        return False
    try:
        payload = json.loads(evidence.detail)
        result = payload["result"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return False
    if not isinstance(result, Mapping):
        return False
    installation_id = result.get("installation_id")
    if not isinstance(installation_id, str) or not installation_id:
        return False
    if step.id == "model.install":
        revision = expected.get("revision")
        return bool(
            set(expected) == {"revision"}
            and isinstance(revision, str)
            and len(revision) in {40, 64}
            and all(character in "0123456789abcdef" for character in revision)
            and result.get("revision") == revision
        )
    required = {"runtime", "version", "provenance", "lock_sha256"}
    lock_sha256 = expected.get("lock_sha256")
    return bool(
        set(expected) == required
        and all(result.get(field) == value for field, value in expected.items())
        and expected.get("provenance") == "tested"
        and isinstance(expected.get("runtime"), str)
        and bool(expected.get("runtime"))
        and isinstance(expected.get("version"), str)
        and bool(expected.get("version"))
        and isinstance(lock_sha256, str)
        and len(lock_sha256) == 64
        and all(character in "0123456789abcdef" for character in lock_sha256)
        and isinstance(result.get("bundle_id"), str)
        and bool(result.get("bundle_id"))
    )


def _performance_binding_valid(
    value: object, performance_profile: Mapping[str, object]
) -> bool:
    if not isinstance(value, Mapping):
        return False
    expected = {
        "selection_sha256",
        "application_versions",
        "platform",
        "machine",
        "memory_bytes",
        "macos_major",
        "service",
    }
    digest = value.get("selection_sha256")
    profile_plan = performance_profile.get("plan")
    return bool(
        set(value) == expected
        and isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
        and isinstance(profile_plan, Mapping)
        and value.get("application_versions")
        == profile_plan.get("application_versions")
        and isinstance(value.get("platform"), str)
        and bool(value.get("platform"))
        and isinstance(value.get("machine"), str)
        and bool(value.get("machine"))
        and type(value.get("memory_bytes")) is int
        and value["memory_bytes"] > 0
        and type(value.get("macos_major")) is int
        and value["macos_major"] > 0
        and isinstance(value.get("service"), str)
        and bool(value.get("service"))
    )


def _performance_binding_matches(
    value: object, performance_profile: Mapping[str, object]
) -> bool:
    if not _performance_binding_valid(value, performance_profile):
        return False
    assert isinstance(value, Mapping)
    host = performance_profile.get("host")
    profile_plan = performance_profile.get("plan")
    if not isinstance(host, Mapping) or not isinstance(profile_plan, Mapping):
        return False
    memory_bytes = value["memory_bytes"]
    assert type(memory_bytes) is int
    return bool(
        value["selection_sha256"] == profile_plan.get("selection_sha256")
        and value["application_versions"] == profile_plan.get("application_versions")
        and value["platform"] == host.get("platform")
        and value["machine"] == host.get("machine")
        and memory_bytes >= host.get("minimum_memory_bytes", math.inf)
        and value["macos_major"] in host.get("macos_major_versions", ())
    )


def _validate_performance_profile(profile: Mapping[str, object]) -> None:
    if not isinstance(profile.get("id"), str) or not profile["id"]:
        raise ValueError("performance profile id must be a nonempty string")
    if type(profile.get("version")) is not int or profile["version"] <= 0:
        raise ValueError("performance profile version must be a positive integer")
    if profile.get("status") not in {"provisional", "validated"}:
        raise ValueError("performance profile status must be provisional or validated")
    host = profile.get("host")
    plan = profile.get("plan")
    metrics = profile.get("metrics")
    if not isinstance(host, Mapping):
        raise ValueError("performance profile host must be an object")
    if not isinstance(plan, Mapping):
        raise ValueError("performance profile plan must be an object")
    if not isinstance(metrics, Mapping):
        raise ValueError("performance profile metrics must be an object")
    if host.get("platform") != "darwin" or host.get("machine") != "arm64":
        raise ValueError("performance profile host must be darwin arm64")
    minimum_memory = host.get("minimum_memory_bytes")
    if type(minimum_memory) is not int or minimum_memory <= 0:
        raise ValueError(
            "performance profile host minimum_memory_bytes must be positive"
        )
    macos_versions = host.get("macos_major_versions")
    if (
        not isinstance(macos_versions, Sequence)
        or isinstance(macos_versions, str | bytes)
        or not macos_versions
        or any(type(item) is not int or item <= 0 for item in macos_versions)
    ):
        raise ValueError(
            "performance profile host macos_major_versions must be positive integers"
        )
    selection_sha256 = plan.get("selection_sha256")
    if (
        not isinstance(selection_sha256, str)
        or len(selection_sha256) != 64
        or any(character not in "0123456789abcdef" for character in selection_sha256)
    ):
        raise ValueError("performance profile plan requires an exact selection sha256")
    application_versions = plan.get("application_versions")
    if not isinstance(application_versions, Mapping) or any(
        not isinstance(name, str)
        or not name
        or not isinstance(version, str)
        or not version
        for name, version in (
            application_versions.items()
            if isinstance(application_versions, Mapping)
            else ()
        )
    ):
        raise ValueError(
            "performance profile plan application_versions must be nonempty strings"
        )
    for metric in (
        "codex.native_canary.duration_seconds",
        "hindsight.native_canary.duration_seconds",
    ):
        band = metrics.get(metric)
        if not isinstance(band, Mapping):
            raise ValueError(f"performance profile requires metric {metric}")
        expected = band.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"performance profile metric {metric} requires expected")
        maximum = expected.get("maximum")
        if (
            type(maximum) not in {int, float}
            or not math.isfinite(maximum)
            or maximum < 0
        ):
            raise ValueError(
                f"performance profile metric {metric} maximum must be nonnegative"
            )
