"""Strict, exact-revision generation-profile knowledge."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from importlib.resources import files
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit


class ModelProfileDefinitionError(ValueError):
    """Bundled model-profile knowledge violates its fail-closed schema."""


@dataclass(frozen=True, slots=True)
class GenerationProfile:
    """One upstream generation recommendation for an exact model revision."""

    repository: str
    revision: str
    name: str
    source_url: str
    source_revision: str
    parameters: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True, slots=True)
class ModelProfileCatalogue:
    """Immutable model profiles indexed only by exact repository identity."""

    profiles: tuple[GenerationProfile, ...]

    @classmethod
    def load_builtin(cls) -> ModelProfileCatalogue:
        resource = files("mlxctl.model_definitions").joinpath("definitions.json")
        try:
            payload = json.loads(resource.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ModelProfileDefinitionError(
                "model profile definitions are unavailable or malformed"
            ) from error
        return cls.from_mapping(payload)

    @classmethod
    def from_mapping(cls, payload: object) -> ModelProfileCatalogue:
        root = _mapping(payload, "model profile catalogue")
        _exact_keys(root, {"models"}, "model profile catalogue")
        models = root["models"]
        if not isinstance(models, list):
            raise ModelProfileDefinitionError("models must be an array")

        profiles: list[GenerationProfile] = []
        model_keys: set[tuple[str, str]] = set()
        profile_keys: set[tuple[str, str, str]] = set()
        for model_value in models:
            model = _mapping(model_value, "model definition")
            _exact_keys(
                model,
                {"repository", "revision", "source", "profiles"},
                "model definition",
            )
            repository = _repository(model["repository"])
            revision = _commit(model["revision"], "model revision")
            model_key = (repository, revision)
            if model_key in model_keys:
                raise ModelProfileDefinitionError(
                    "model repository and revision must be unique"
                )
            model_keys.add(model_key)

            source = _mapping(model["source"], "profile source")
            _exact_keys(source, {"url", "revision"}, "profile source")
            source_url = _source_url(source["url"])
            source_revision = _commit(source["revision"], "source revision")
            profile_values = model["profiles"]
            if not isinstance(profile_values, list) or not profile_values:
                raise ModelProfileDefinitionError(
                    "model profiles must be a nonempty array"
                )
            for profile_value in profile_values:
                profile = _mapping(profile_value, "generation profile")
                _exact_keys(
                    profile,
                    {
                        "name",
                        "temperature",
                        "top_p",
                        "top_k",
                        "min_p",
                        "presence_penalty",
                        "repetition_penalty",
                        "enable_thinking",
                    },
                    "generation profile",
                )
                name = _profile_name(profile["name"])
                profile_key = (repository, revision, name)
                if profile_key in profile_keys:
                    raise ModelProfileDefinitionError(
                        "profile names must be unique for a model revision"
                    )
                profile_keys.add(profile_key)
                parameters = {
                    "temperature": _number(
                        profile["temperature"], "temperature", minimum=0, maximum=2
                    ),
                    "top_p": _number(
                        profile["top_p"],
                        "top_p",
                        minimum=0,
                        maximum=1,
                        minimum_exclusive=True,
                    ),
                    "top_k": _integer(
                        profile["top_k"], "top_k", minimum=1, maximum=100_000
                    ),
                    "min_p": _number(profile["min_p"], "min_p", minimum=0, maximum=1),
                    "presence_penalty": _number(
                        profile["presence_penalty"],
                        "presence_penalty",
                        minimum=-2,
                        maximum=2,
                    ),
                    "repetition_penalty": _number(
                        profile["repetition_penalty"],
                        "repetition_penalty",
                        minimum=0,
                        maximum=2,
                        minimum_exclusive=True,
                    ),
                    "enable_thinking": _boolean(
                        profile["enable_thinking"], "enable_thinking"
                    ),
                }
                profiles.append(
                    GenerationProfile(
                        repository=repository,
                        revision=revision,
                        name=name,
                        source_url=source_url,
                        source_revision=source_revision,
                        parameters=parameters,
                    )
                )
        return cls(tuple(profiles))

    def profile(self, repository: str, revision: str, name: str) -> GenerationProfile:
        for profile in self.profiles:
            if (
                profile.repository == repository
                and profile.revision == revision
                and profile.name == name
            ):
                return profile
        raise KeyError(
            f"unknown model generation profile: {repository}@{revision}#{name}"
        )


def _mapping(value: object, scope: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ModelProfileDefinitionError(f"{scope} must be an object")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], scope: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if unknown:
            detail.append("unknown " + ", ".join(unknown))
        raise ModelProfileDefinitionError(f"{scope} has " + "; ".join(detail))


def _repository(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*", value
    ):
        raise ModelProfileDefinitionError("repository must be a Hub repository ID")
    return value


def _commit(value: object, scope: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value
    ):
        raise ModelProfileDefinitionError(f"{scope} must be an exact commit SHA")
    return value


def _source_url(value: object) -> str:
    if not isinstance(value, str):
        raise ModelProfileDefinitionError("profile source URL must be HTTPS")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ModelProfileDefinitionError("profile source URL must be HTTPS")
    return value


def _profile_name(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,63}", value
    ):
        raise ModelProfileDefinitionError("profile name is invalid")
    return value


def _number(
    value: object,
    name: str,
    *,
    minimum: float,
    maximum: float,
    minimum_exclusive: bool = False,
) -> float:
    if type(value) not in {int, float}:
        raise ModelProfileDefinitionError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ModelProfileDefinitionError(f"{name} must be finite")
    below_minimum = number <= minimum if minimum_exclusive else number < minimum
    if below_minimum or number > maximum:
        interval = "(" if minimum_exclusive else "["
        raise ModelProfileDefinitionError(
            f"{name} must be in {interval}{minimum}, {maximum}]"
        )
    return number


def _integer(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ModelProfileDefinitionError(
            f"{name} must be an integer in [{minimum}, {maximum}]"
        )
    return value


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ModelProfileDefinitionError(f"{name} must be a boolean")
    return value
