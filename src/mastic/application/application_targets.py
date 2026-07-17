"""Application-layer contracts for Application Configuration Targets."""

from __future__ import annotations

from dataclasses import dataclass, fields
import math
import re
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit


_REQUIRED_SAMPLING_PROFILES = MappingProxyType(
    {
        "codex": frozenset({"coding"}),
        "hindsight": frozenset({"verification", "retain", "reflect", "consolidation"}),
    }
)


@dataclass(frozen=True, slots=True)
class SamplingProfile:
    """One validated sampling profile shared by desired state and integrations."""

    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    presence_penalty: float | None = None
    repetition_penalty: float | None = None
    max_tokens: int | None = None
    enable_thinking: bool | None = None
    preserve_thinking: bool | None = None
    upstream_profile: str | None = None
    source_url: str | None = None
    source_revision: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "temperature",
            "top_p",
            "min_p",
            "presence_penalty",
            "repetition_penalty",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            if type(value) not in {int, float}:
                raise ValueError(f"{name} must be numeric")
            normalized = float(value)
            if not math.isfinite(normalized):
                raise ValueError("sampling values must be finite")
            object.__setattr__(self, name, normalized)
        if self.temperature is not None and self.temperature < 0:
            raise ValueError("temperature must be nonnegative")
        if self.top_p is not None and not 0 < self.top_p <= 1:
            raise ValueError("top_p must be greater than zero and at most one")
        if self.top_k is not None and (type(self.top_k) is not int or self.top_k < 0):
            raise ValueError("top_k must be a nonnegative integer")
        if self.min_p is not None and not 0 <= self.min_p <= 1:
            raise ValueError("min_p must be between zero and one")
        if self.presence_penalty is not None and not -2 <= self.presence_penalty <= 2:
            raise ValueError("presence_penalty must be between -2 and 2")
        if self.repetition_penalty is not None and self.repetition_penalty <= 0:
            raise ValueError("repetition_penalty must be positive")
        if self.max_tokens is not None and (
            type(self.max_tokens) is not int or self.max_tokens <= 0
        ):
            raise ValueError("max_tokens must be a positive integer")
        if self.enable_thinking is not None and type(self.enable_thinking) is not bool:
            raise ValueError("enable_thinking must be boolean")
        if (
            self.preserve_thinking is not None
            and type(self.preserve_thinking) is not bool
        ):
            raise ValueError("preserve_thinking must be boolean")
        provenance = (self.upstream_profile, self.source_url, self.source_revision)
        if any(value is not None for value in provenance) and not all(
            value is not None for value in provenance
        ):
            raise ValueError("profile provenance must be complete")
        if self.upstream_profile is not None and (
            not isinstance(self.upstream_profile, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", self.upstream_profile)
        ):
            raise ValueError("upstream_profile is invalid")
        if self.source_url is not None:
            if not isinstance(self.source_url, str):
                raise ValueError("source_url must be HTTPS")
            source = urlsplit(self.source_url)
            if (
                source.scheme != "https"
                or not source.hostname
                or source.username is not None
                or source.password is not None
            ):
                raise ValueError("source_url must be HTTPS")
        if self.source_revision is not None and (
            not isinstance(self.source_revision, str)
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.source_revision)
        ):
            raise ValueError("source_revision must be an exact commit SHA")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SamplingProfile:
        """Validate recognized fields from an untyped operation payload."""
        names = {field.name for field in fields(cls)}
        unknown = sorted(set(value) - names)
        if unknown:
            raise ValueError("unknown sampling profile fields: " + ", ".join(unknown))
        return cls(**dict(value))  # type: ignore[arg-type]

    def values(self) -> Mapping[str, object]:
        """Return runtime sampling values without provenance metadata."""
        provenance = {"upstream_profile", "source_url", "source_revision"}
        return MappingProxyType(
            {
                field.name: value
                for field in fields(self)
                if field.name not in provenance
                and (value := getattr(self, field.name)) is not None
            }
        )

    def definition(self) -> Mapping[str, object]:
        """Return sampling values plus complete upstream provenance."""
        values = dict(self.values())
        if self.upstream_profile is not None:
            values.update(
                upstream_profile=self.upstream_profile,
                source_url=self.source_url,
                source_revision=self.source_revision,
            )
        return MappingProxyType(values)


def validate_application_target_sampling_profiles(
    application_target: str,
    profiles: Mapping[str, SamplingProfile],
) -> None:
    """Validate the exact sampling-profile contract for one managed target."""
    try:
        required = _REQUIRED_SAMPLING_PROFILES[application_target]
    except KeyError as error:
        raise ValueError(
            f"unsupported Application Configuration Target: {application_target}"
        ) from error
    if set(profiles) != required:
        raise ValueError(
            f"{application_target} requires sampling profiles: {', '.join(sorted(required))}"
        )
    if application_target != "codex":
        return
    coding = profiles["coding"]
    if (
        coding.min_p not in {None, 0.0}
        or coding.presence_penalty not in {None, 0.0}
        or coding.repetition_penalty not in {None, 1.0}
        or coding.max_tokens is not None
    ):
        raise ValueError(
            "Codex coding profile contains values OptiQ Responses cannot represent"
        )
