"""Load the versioned mlxd TOML configuration."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .server_options import OPTION_TYPES_BY_SERVER_TYPE


_ALIAS_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class ConfigError(ValueError):
    """The configuration does not satisfy the mlxctl contract."""


@dataclass(frozen=True)
class DaemonSettings:
    readiness_timeout_seconds: int | float = 120
    stop_timeout_seconds: int | float = 10
    metrics_interval_seconds: int | float = 5


@dataclass(frozen=True)
class MetricsSettings:
    retention_days: int = 30


@dataclass(frozen=True)
class ModelDefinition:
    alias: str
    reference: str


@dataclass(frozen=True)
class ServerDefinition:
    name: str
    type: str
    model: str
    host: str
    port: int
    environment: Mapping[str, str]
    options: Mapping[str, object]


@dataclass(frozen=True)
class Config:
    schema_version: int
    daemon: DaemonSettings
    metrics: MetricsSettings
    models: Mapping[str, ModelDefinition]
    servers: Mapping[str, ServerDefinition]


def load_config(path: str | Path) -> Config:
    """Load and validate a config.toml file."""
    try:
        with Path(path).open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot load config {path}: {error}") from error

    _reject_unknown(
        "root", raw, {"schema_version", "daemon", "metrics", "models", "servers"}
    )
    schema_version = raw.get("schema_version")
    if schema_version is None:
        raise ConfigError("schema_version is required")
    if type(schema_version) is not int or schema_version != 1:
        raise ConfigError("schema_version must be 1")

    daemon_raw = _table(raw.get("daemon", {}), "daemon")
    _reject_unknown(
        "daemon",
        daemon_raw,
        {
            "readiness_timeout_seconds",
            "stop_timeout_seconds",
            "metrics_interval_seconds",
        },
    )
    daemon_values = {}
    for key, default in (
        ("readiness_timeout_seconds", 120),
        ("stop_timeout_seconds", 10),
        ("metrics_interval_seconds", 5),
    ):
        value = daemon_raw.get(key, default)
        if not _is_number(value) or value <= 0:
            raise ConfigError(f"daemon {key} must be a positive number")
        daemon_values[key] = value
    daemon = DaemonSettings(**daemon_values)

    metrics_raw = _table(raw.get("metrics", {}), "metrics")
    _reject_unknown("metrics", metrics_raw, {"retention_days"})
    retention_days = metrics_raw.get("retention_days", 30)
    if type(retention_days) is not int or retention_days <= 0:
        raise ConfigError("metrics retention_days must be a positive integer")
    metrics = MetricsSettings(retention_days=retention_days)

    models_raw = _table(raw.get("models", {}), "models")
    models = {}
    for model_alias, raw_definition in models_raw.items():
        validate_alias(model_alias, "model")
        definition = _table(raw_definition, f"model '{model_alias}'")
        _reject_unknown(f"model '{model_alias}'", definition, {"reference"})
        reference = definition.get("reference")
        if not isinstance(reference, str) or not reference:
            raise ConfigError(f"model '{model_alias}' requires string 'reference'")
        models[model_alias] = ModelDefinition(alias=model_alias, reference=reference)

    servers_raw = _table(raw.get("servers", {}), "servers")
    servers = {}
    listen_addresses: dict[tuple[str, int], str] = {}
    for server_id, raw_definition in servers_raw.items():
        validate_alias(server_id, "server")
        definition = _table(raw_definition, f"server '{server_id}'")
        _reject_unknown(
            f"server '{server_id}'",
            definition,
            {"type", "model", "host", "port", "environment", "options"},
        )
        server_type = definition.get("type")
        if not isinstance(server_type, str):
            raise ConfigError(f"server '{server_id}' requires string 'type'")
        if server_type not in OPTION_TYPES_BY_SERVER_TYPE:
            raise ConfigError(
                f"server '{server_id}' type '{server_type}' is not supported"
            )
        model_alias = definition.get("model")
        if not isinstance(model_alias, str):
            raise ConfigError(f"server '{server_id}' requires string 'model'")
        if model_alias not in models:
            raise ConfigError(
                f"server '{server_id}' model alias '{model_alias}' is not defined"
            )
        host = definition.get("host", "127.0.0.1")
        normalized_host = _loopback_host(server_id, host)
        port = definition.get("port")
        if type(port) is not int:
            raise ConfigError(f"server '{server_id}' requires integer 'port'")
        if not 1 <= port <= 65535:
            raise ConfigError(f"server '{server_id}' port must be in 1..65535")
        listen_address = (normalized_host, port)
        if listen_address in listen_addresses:
            first_server = listen_addresses[listen_address]
            raise ConfigError(
                f"servers '{first_server}' and '{server_id}' share listen address {host}:{port}"
            )
        listen_addresses[listen_address] = server_id

        environment = _table(
            definition.get("environment", {}), f"server '{server_id}' environment"
        )
        for key, value in environment.items():
            if not isinstance(value, str):
                raise ConfigError(
                    f"server '{server_id}' environment value '{key}' must be a string"
                )

        options = _table(definition.get("options", {}), f"server '{server_id}' options")
        option_types = OPTION_TYPES_BY_SERVER_TYPE[server_type]
        _reject_unknown(
            f"server '{server_id}'",
            options,
            set(option_types),
            noun="option",
        )
        _validate_options(server_id, options, option_types)
        servers[server_id] = ServerDefinition(
            name=server_id,
            type=server_type,
            model=model_alias,
            host=host,
            port=port,
            environment=MappingProxyType(dict(environment)),
            options=MappingProxyType(
                {
                    key: tuple(value) if isinstance(value, list) else value
                    for key, value in options.items()
                }
            ),
        )
    return Config(
        schema_version=schema_version,
        daemon=daemon,
        metrics=metrics,
        models=MappingProxyType(models),
        servers=MappingProxyType(servers),
    )


def _table(value: object, scope: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{scope} must be a table")
    return value


def validate_alias(alias: str, noun: str = "alias") -> None:
    """Reject aliases that are unsafe for filesystem-backed runtime identity."""
    if not isinstance(alias, str) or _ALIAS_PATTERN.fullmatch(alias) is None:
        raise ConfigError(
            f"{noun} alias '{alias}' must match [A-Za-z0-9][A-Za-z0-9._-]*"
        )


def _loopback_host(server_id: str, host: object) -> str:
    if not isinstance(host, str):
        raise ConfigError(f"server '{server_id}' host must be a string")
    if host == "localhost":
        return "127.0.0.1"
    try:
        address = ip_address(host)
    except ValueError:
        address = None
    if address is None or not address.is_loopback:
        raise ConfigError(f"server '{server_id}' host '{host}' is not loopback")
    return address.compressed


def _validate_options(
    server_id: str,
    options: Mapping[str, object],
    option_types: Mapping[str, str],
) -> None:
    for key, value in options.items():
        expected = option_types[key]
        valid = {
            "string": lambda item: isinstance(item, str),
            "integer": lambda item: type(item) is int,
            "number": _is_number,
            "boolean": lambda item: type(item) is bool,
            "string array": lambda item: (
                isinstance(item, list) and all(isinstance(entry, str) for entry in item)
            ),
        }[expected](value)
        if not valid:
            article = "an" if expected == "integer" else "a"
            raise ConfigError(
                f"server '{server_id}' option '{key}' must be {article} {expected}"
            )


def _is_number(value: object) -> bool:
    return type(value) in {int, float}


def _reject_unknown(
    scope: str,
    values: Mapping[str, object],
    allowed: set[str] | frozenset[str],
    *,
    noun: str = "key",
) -> None:
    unknown = values.keys() - allowed
    if unknown:
        key = sorted(unknown)[0]
        raise ConfigError(f"{scope} {noun} '{key}' is not supported")
