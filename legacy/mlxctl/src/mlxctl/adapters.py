"""Prepare server processes behind one registry interface."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from types import MappingProxyType
from typing import Mapping, Protocol

from .config import ModelDefinition, ServerDefinition


class AdapterError(ValueError):
    """A server definition cannot be prepared by its adapter."""


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int

    def __post_init__(self) -> None:
        if not isinstance(self.host, str):
            raise TypeError("endpoint host must be a string")
        host = "127.0.0.1" if self.host == "localhost" else self.host
        try:
            address = ip_address(host)
        except ValueError:
            address = None
        if address is None or not address.is_loopback:
            raise ValueError(f"endpoint host '{self.host}' is not loopback")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("endpoint port must be in 1..65535")
        object.__setattr__(self, "host", address.compressed)


@dataclass(frozen=True)
class PreparedServer:
    argv: tuple[str, ...]
    environment: Mapping[str, str]


class _Adapter(Protocol):
    def prepare(
        self,
        definition: ServerDefinition,
        model: ModelDefinition,
        upstream: Endpoint,
    ) -> PreparedServer: ...


class MlxLmAdapter:
    """Prepare an ``mlx_lm.server`` child process."""

    def prepare(
        self,
        definition: ServerDefinition,
        model: ModelDefinition,
        upstream: Endpoint,
    ) -> PreparedServer:
        argv = [
            "mlx_lm.server",
            "--model",
            model.reference,
            "--host",
            upstream.host,
            "--port",
            str(upstream.port),
        ]
        _append_mlx_lm_options(argv, definition.options)
        return _prepared(argv, definition.environment)


class OptiqAdapter:
    """Prepare an ``optiq serve`` child process."""

    def prepare(
        self,
        definition: ServerDefinition,
        model: ModelDefinition,
        upstream: Endpoint,
    ) -> PreparedServer:
        argv = [
            "optiq",
            "serve",
            "--model",
            model.reference,
            "--host",
            upstream.host,
            "--port",
            str(upstream.port),
        ]
        _append_mlx_lm_options(argv, definition.options)
        for key in ("kv_bits", "kv_group_size", "quantized_kv_start", "kv_config"):
            if key in definition.options:
                _append_value(argv, key, definition.options[key])
        for adapter in definition.options.get("adapter", []):
            _append_value(argv, "adapter", adapter)
        _append_choice(
            argv, definition.options, "anthropic", "--anthropic", "--no-anthropic"
        )
        _append_choice(
            argv,
            definition.options,
            "allow_model_switch",
            "--allow-model-switch",
            "--single-model",
        )
        for key in ("idle_timeout", "max_context"):
            if key in definition.options:
                _append_value(argv, key, definition.options[key])
        return _prepared(argv, definition.environment)


class AdapterRegistry:
    """Resolve a server type and prepare its child process invocation."""

    def __init__(self) -> None:
        self._adapters: Mapping[str, _Adapter] = {
            "mlx_lm": MlxLmAdapter(),
            "optiq": OptiqAdapter(),
        }

    def prepare(
        self,
        definition: ServerDefinition,
        model: ModelDefinition,
        upstream: Endpoint,
    ) -> PreparedServer:
        if definition.model != model.alias:
            raise AdapterError(
                f"server '{definition.name}' expects model alias '{definition.model}', "
                f"not '{model.alias}'"
            )
        try:
            adapter = self._adapters[definition.type]
        except KeyError as error:
            raise AdapterError(
                f"no adapter for server type '{definition.type}'"
            ) from error
        return adapter.prepare(definition, model, upstream)


def _prepared(argv: list[str], environment: Mapping[str, str]) -> PreparedServer:
    return PreparedServer(
        argv=tuple(argv),
        environment=MappingProxyType(dict(environment)),
    )


def _append_mlx_lm_options(argv: list[str], options: Mapping[str, object]) -> None:
    for key in ("draft_model", "prompt_cache_size", "prompt_concurrency"):
        if key in options:
            _append_value(argv, key, options[key])
    if options.get("pipeline") is True:
        argv.append("--pipeline")
    for key in ("temp", "top_p", "top_k"):
        if key in options:
            _append_value(argv, key, options[key])


def _append_value(argv: list[str], key: str, value: object) -> None:
    argv.extend((f"--{key.replace('_', '-')}", str(value)))


def _append_choice(
    argv: list[str],
    options: Mapping[str, object],
    key: str,
    true_flag: str,
    false_flag: str,
) -> None:
    if key in options:
        argv.append(true_flag if options[key] is True else false_flag)
