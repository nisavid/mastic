"""Strict supported-v1 desired-state schema."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping

from mlxctl.domain.resources import (
    ActivationPolicy,
    InferenceService,
    ModelAlias,
    ModelInstallation,
    ModelRevision,
    ResourceName,
    RuntimeFamily,
)


class ConfigSchemaError(ValueError):
    """Desired configuration does not satisfy the supported-v1 schema."""


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    host: str = "127.0.0.1"
    port: int = 8766


@dataclass(frozen=True, slots=True)
class ConfiguredRuntime:
    installation_id: str
    definition: str
    version: str
    provenance: str
    root: str
    launcher: tuple[str, ...]
    capabilities: frozenset[str]
    bundle_id: str | None = None


@dataclass(frozen=True, slots=True)
class ClientSamplingSettings:
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ClientSettings:
    name: str
    kind: str
    service: str
    profile: str | None
    context_window: int | None
    provider: str
    max_concurrent: int | None
    sampling: Mapping[str, ClientSamplingSettings]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sampling", MappingProxyType(dict(self.sampling)))


@dataclass(frozen=True, slots=True)
class MlxctlConfig:
    schema_version: int
    gateway: GatewaySettings
    runtimes: Mapping[str, ConfiguredRuntime]
    models: Mapping[str, ModelInstallation]
    aliases: Mapping[str, ModelAlias]
    services: Mapping[str, InferenceService]
    clients: Mapping[str, ClientSettings]


def validate_config(raw: Mapping[str, object]) -> MlxctlConfig:
    """Validate one parsed TOML document into immutable desired state."""
    unwrap = getattr(raw, "unwrap", None)
    if callable(unwrap):
        raw = unwrap()
    _reject_unknown(
        "root",
        raw,
        {
            "schema_version",
            "gateway",
            "runtimes",
            "models",
            "aliases",
            "services",
            "clients",
        },
    )
    if raw.get("schema_version") != 1 or type(raw.get("schema_version")) is not int:
        raise ConfigSchemaError("schema_version must be 1")
    gateway = _gateway(_table(raw.get("gateway", {}), "gateway"))
    runtimes = _runtimes(_table(raw.get("runtimes", {}), "runtimes"))
    models = _models(_table(raw.get("models", {}), "models"))
    aliases = _aliases(_table(raw.get("aliases", {}), "aliases"), models)
    services = _services(_table(raw.get("services", {}), "services"), aliases, runtimes)
    clients = _clients(_table(raw.get("clients", {}), "clients"), services)
    return MlxctlConfig(
        schema_version=1,
        gateway=gateway,
        runtimes=MappingProxyType(runtimes),
        models=MappingProxyType(models),
        aliases=MappingProxyType(aliases),
        services=MappingProxyType(services),
        clients=MappingProxyType(clients),
    )


def _gateway(raw: Mapping[str, object]) -> GatewaySettings:
    _reject_unknown("gateway", raw, {"host", "port"})
    host = raw.get("host", "127.0.0.1")
    if not isinstance(host, str):
        raise ConfigSchemaError("Gateway host must be a loopback IP address")
    try:
        address = ip_address(host)
    except ValueError as error:
        raise ConfigSchemaError("Gateway host must be a loopback IP address") from error
    if not address.is_loopback:
        raise ConfigSchemaError("Gateway host must be loopback-only")
    port = raw.get("port", 8766)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigSchemaError("Gateway port must be an integer in 1..65535")
    return GatewaySettings(address.compressed, port)


def _runtimes(raw: Mapping[str, object]) -> dict[str, ConfiguredRuntime]:
    result = {}
    for installation_id, value in raw.items():
        table = _table(value, f"runtime {installation_id!r}")
        _reject_unknown(
            f"runtime {installation_id!r}",
            table,
            {
                "definition",
                "version",
                "provenance",
                "root",
                "launcher",
                "capabilities",
                "bundle_id",
            },
        )
        definition = _string(table, "definition", f"runtime {installation_id!r}")
        try:
            RuntimeFamily(definition.replace("_", "-"))
        except ValueError as error:
            raise ConfigSchemaError(
                f"unknown Runtime Definition {definition!r}"
            ) from error
        provenance = _string(table, "provenance", f"runtime {installation_id!r}")
        if provenance not in {"tested", "custom", "adopted"}:
            raise ConfigSchemaError(
                f"runtime {installation_id!r} provenance must be tested, custom, or adopted"
            )
        root = Path(_string(table, "root", f"runtime {installation_id!r}"))
        launcher_raw = table.get("launcher")
        capabilities_raw = table.get("capabilities")
        if not root.is_absolute():
            raise ConfigSchemaError(
                f"runtime {installation_id!r} root must be absolute"
            )
        if (
            not isinstance(launcher_raw, list)
            or not launcher_raw
            or not all(isinstance(item, str) and item for item in launcher_raw)
        ):
            raise ConfigSchemaError(
                f"runtime {installation_id!r} launcher must be a nonempty string array"
            )
        if not Path(launcher_raw[0]).is_absolute():
            raise ConfigSchemaError(
                f"runtime {installation_id!r} launcher executable must be absolute"
            )
        if not isinstance(capabilities_raw, list) or not all(
            isinstance(item, str) and item for item in capabilities_raw
        ):
            raise ConfigSchemaError(
                f"runtime {installation_id!r} capabilities must be a string array"
            )
        bundle_id = table.get("bundle_id")
        if bundle_id is not None and not isinstance(bundle_id, str):
            raise ConfigSchemaError(
                f"runtime {installation_id!r} bundle_id must be a string"
            )
        result[installation_id] = ConfiguredRuntime(
            installation_id=installation_id,
            definition=definition,
            version=_string(table, "version", f"runtime {installation_id!r}"),
            provenance=provenance,
            root=str(root),
            launcher=tuple(launcher_raw),
            capabilities=frozenset(capabilities_raw),
            bundle_id=bundle_id,
        )
    return result


def _models(raw: Mapping[str, object]) -> dict[str, ModelInstallation]:
    result = {}
    for name, value in raw.items():
        try:
            ResourceName(name)
            table = _table(value, f"model {name!r}")
            _reject_unknown(f"model {name!r}", table, {"repository", "revision"})
            revision = ModelRevision(
                _string(table, "repository", f"model {name!r}"),
                _string(table, "revision", f"model {name!r}"),
            )
            result[name] = ModelInstallation(name, revision)
        except ValueError as error:
            raise ConfigSchemaError(str(error)) from error
    return result


def _aliases(
    raw: Mapping[str, object], models: Mapping[str, ModelInstallation]
) -> dict[str, ModelAlias]:
    result = {}
    for name, value in raw.items():
        table = _table(value, f"alias {name!r}")
        _reject_unknown(f"alias {name!r}", table, {"installation"})
        installation = _string(table, "installation", f"alias {name!r}")
        if installation not in models:
            raise ConfigSchemaError(
                f"Model Alias {name!r} references unknown Model Installation {installation!r}"
            )
        try:
            result[name] = ModelAlias(ResourceName(name), installation)
        except ValueError as error:
            raise ConfigSchemaError(str(error)) from error
    return result


def _services(
    raw: Mapping[str, object],
    aliases: Mapping[str, ModelAlias],
    runtimes: Mapping[str, ConfiguredRuntime],
) -> dict[str, InferenceService]:
    result = {}
    routes: dict[str, str] = {}
    for name, value in raw.items():
        table = _table(value, f"service {name!r}")
        _reject_unknown(
            f"service {name!r}",
            table,
            {"model_alias", "runtime", "route", "activation", "pinned", "options"},
        )
        alias = _string(table, "model_alias", f"service {name!r}")
        if alias not in aliases:
            raise ConfigSchemaError(
                f"service {name!r} references unknown Model Alias {alias!r}"
            )
        runtime = _string(table, "runtime", f"service {name!r}")
        if runtime not in runtimes:
            raise ConfigSchemaError(
                f"service {name!r} references unknown Runtime Installation {runtime!r}"
            )
        route = _string(table, "route", f"service {name!r}")
        if route in routes:
            raise ConfigSchemaError(
                f"Gateway route {route!r} is shared by services {routes[route]!r} and {name!r}"
            )
        routes[route] = name
        activation_raw = table.get("activation", "manual")
        try:
            activation = ActivationPolicy(activation_raw)
        except (TypeError, ValueError) as error:
            raise ConfigSchemaError(
                f"service {name!r} activation must be manual or supervisor"
            ) from error
        pinned = table.get("pinned", False)
        if type(pinned) is not bool:
            raise ConfigSchemaError(f"service {name!r} pinned must be boolean")
        options = _table(table.get("options", {}), f"service {name!r} options")
        for key, option in options.items():
            if not isinstance(key, str) or not _option_value(option):
                raise ConfigSchemaError(
                    f"service {name!r} option {key!r} has an unsupported value"
                )
        try:
            result[name] = InferenceService(
                name=ResourceName(name),
                model_alias=ResourceName(alias),
                runtime_installation=runtime,
                route=ResourceName(route),
                activation=activation,
                pinned=pinned,
                options=dict(options),
            )
        except ValueError as error:
            raise ConfigSchemaError(str(error)) from error
    return result


def _clients(
    raw: Mapping[str, object], services: Mapping[str, InferenceService]
) -> dict[str, ClientSettings]:
    result = {}
    for name, value in raw.items():
        table = _table(value, f"client {name!r}")
        _reject_unknown(
            f"client {name!r}",
            table,
            {
                "kind",
                "service",
                "profile",
                "context_window",
                "provider",
                "max_concurrent",
                "sampling",
            },
        )
        kind = _string(table, "kind", f"client {name!r}")
        if kind not in {"codex", "hindsight"}:
            raise ConfigSchemaError(f"unsupported client kind {kind!r}")
        if name != kind:
            raise ConfigSchemaError(
                f"client {name!r} name must match its supported kind {kind!r}"
            )
        service = _string(table, "service", f"client {name!r}")
        if service not in services:
            raise ConfigSchemaError(
                f"client {name!r} references unknown Inference Service {service!r}"
            )
        profile = table.get("profile")
        if profile is not None and not isinstance(profile, str):
            raise ConfigSchemaError(f"client {name!r} profile must be a string")
        if kind == "hindsight":
            try:
                profile = validate_hindsight_profile_name(profile)
            except ConfigSchemaError as error:
                raise ConfigSchemaError(
                    f"client {name!r} requires a safe Hindsight profile name"
                ) from error
        elif profile is not None:
            raise ConfigSchemaError(
                f"client {name!r} profile is only valid for Hindsight"
            )
        context_window = table.get("context_window")
        if context_window is not None and (
            type(context_window) is not int or context_window <= 0
        ):
            raise ConfigSchemaError(
                f"client {name!r} context_window must be a positive integer"
            )
        provider = table.get(
            "provider", "mlxctl-local" if kind == "codex" else "openai"
        )
        if not isinstance(provider, str) or not provider:
            raise ConfigSchemaError(f"client {name!r} provider must be a string")
        max_concurrent = table.get("max_concurrent", 1 if kind == "hindsight" else None)
        if max_concurrent is not None and (
            type(max_concurrent) is not int or max_concurrent <= 0
        ):
            raise ConfigSchemaError(
                f"client {name!r} max_concurrent must be a positive integer"
            )
        if kind == "codex" and max_concurrent is not None:
            raise ConfigSchemaError(
                f"client {name!r} max_concurrent is only valid for Hindsight"
            )
        sampling_raw = _table(table.get("sampling", {}), f"client {name!r} sampling")
        sampling: dict[str, ClientSamplingSettings] = {}
        normalized: set[str] = set()
        for sampling_name, sampling_value in sampling_raw.items():
            if not isinstance(sampling_name, str) or not _safe_sampling_name(
                sampling_name
            ):
                raise ConfigSchemaError(
                    f"client {name!r} has an invalid sampling profile name"
                )
            normalized_name = (
                re.sub(r"[^A-Za-z0-9]+", "_", sampling_name).strip("_").upper()
            )
            if normalized_name in normalized:
                raise ConfigSchemaError(
                    f"client {name!r} sampling profile names collide after normalization"
                )
            normalized.add(normalized_name)
            values = _table(
                sampling_value,
                f"client {name!r} sampling profile {sampling_name!r}",
            )
            _reject_unknown(
                f"client {name!r} sampling profile {sampling_name!r}",
                values,
                {"temperature", "top_p", "max_tokens"},
            )
            temperature = values.get("temperature")
            top_p = values.get("top_p")
            max_tokens = values.get("max_tokens")
            if temperature is not None and (
                type(temperature) not in {int, float} or temperature < 0
            ):
                raise ConfigSchemaError(
                    f"client {name!r} sampling profile {sampling_name!r} temperature must be nonnegative"
                )
            if top_p is not None and (
                type(top_p) not in {int, float} or not 0 < top_p <= 1
            ):
                raise ConfigSchemaError(
                    f"client {name!r} sampling profile {sampling_name!r} top_p must be in (0, 1]"
                )
            if max_tokens is not None and (
                type(max_tokens) is not int or max_tokens <= 0
            ):
                raise ConfigSchemaError(
                    f"client {name!r} sampling profile {sampling_name!r} max_tokens must be positive"
                )
            sampling[sampling_name] = ClientSamplingSettings(
                temperature=float(temperature) if temperature is not None else None,
                top_p=float(top_p) if top_p is not None else None,
                max_tokens=max_tokens,
            )
        result[name] = ClientSettings(
            name=name,
            kind=kind,
            service=service,
            profile=profile,
            context_window=context_window,
            provider=provider,
            max_concurrent=max_concurrent,
            sampling=MappingProxyType(sampling),
        )
    return result


def validate_hindsight_profile_name(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value
    ):
        raise ConfigSchemaError(
            "Hindsight profile must be 1-64 letters, numbers, dots, dashes, or underscores and start with a letter or number"
        )
    return value


def _safe_sampling_name(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value))


def _option_value(value: object) -> bool:
    if type(value) in {str, int, float, bool}:
        return True
    return isinstance(value, list) and all(
        type(item) in {str, int, float, bool} for item in value
    )


def _table(value: object, scope: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigSchemaError(f"{scope} must be a table")
    return value


def _string(table: Mapping[str, object], key: str, scope: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigSchemaError(f"{scope} requires non-empty string {key!r}")
    return value


def _reject_unknown(
    scope: str, values: Mapping[str, object], allowed: set[str]
) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ConfigSchemaError(f"{scope} contains unknown keys: {', '.join(unknown)}")
