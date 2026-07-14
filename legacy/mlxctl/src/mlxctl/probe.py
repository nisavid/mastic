"""Probe the uniform HTTP surface shared by supported servers."""

from __future__ import annotations

import http.client
import json
from typing import Any
from urllib.error import HTTPError
from urllib.request import urlopen


class ProbeError(RuntimeError):
    """A server did not satisfy the uniform probe contract."""


def probe_liveness(base_url: str, *, timeout_seconds: float = 2.0) -> bool:
    """Return true when the server satisfies the liveness contract."""
    health = _read_json(f"{base_url.rstrip('/')}/health", timeout_seconds)
    if health != {"status": "ok"}:
        raise ProbeError("/health must return exactly {'status': 'ok'}")
    return True


def probe_readiness(base_url: str, *, timeout_seconds: float = 2.0) -> tuple[str, ...]:
    """Return advertised model identifiers when the server is ready."""
    models = _read_json(f"{base_url.rstrip('/')}/v1/models", timeout_seconds)
    if not isinstance(models, dict) or not isinstance(models.get("data"), list):
        raise ProbeError("/v1/models must return an object with list 'data'")
    model_ids: list[str] = []
    for index, model in enumerate(models["data"]):
        if not isinstance(model, dict) or not isinstance(model.get("id"), str):
            raise ProbeError(f"/v1/models data[{index}] must contain string 'id'")
        model_ids.append(model["id"])
    return tuple(model_ids)


def _read_json(url: str, timeout_seconds: float) -> Any:
    try:
        with urlopen(url, timeout=timeout_seconds) as response:
            return json.loads(response.read())
    except HTTPError as error:
        error.close()
        raise ProbeError(f"GET {url} failed: {error}") from error
    except (
        OSError,
        http.client.HTTPException,
        ValueError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as error:
        raise ProbeError(f"GET {url} failed: {error}") from error
