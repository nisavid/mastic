"""Stable loopback Gateway for named mlxctl Inference Services."""

from __future__ import annotations

import inspect
import ipaddress
import json
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

RouteState = Literal["ready", "stopped", "unavailable"]
_REQUEST_HEADER_ALLOWLIST = frozenset(
    {"accept", "authorization", "content-type", "user-agent", "x-request-id"}
)
_RESPONSE_HEADER_ALLOWLIST = frozenset(
    {"cache-control", "content-encoding", "content-type", "x-request-id"}
)
DEFAULT_MAX_REQUEST_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class GatewayRoute:
    """A public service identity and its current private routing state."""

    service: str
    state: RouteState
    endpoint: str | None = None
    model: str | None = None
    runtime: str | None = None


class GatewayRouteResolver(Protocol):
    """Resolve current Gateway Routes without exposing arbitrary destinations."""

    def list_routes(self) -> Iterable[GatewayRoute] | Any: ...

    def resolve(self, service: str) -> GatewayRoute | None | Any: ...


class _UpstreamStreamingResponse(StreamingResponse):
    """Streaming response that closes upstream even when downstream send fails."""

    def __init__(self, upstream: httpx.Response, **kwargs: Any) -> None:
        self._upstream = upstream
        super().__init__(upstream.aiter_raw(), **kwargs)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._upstream.aclose()


def validate_loopback_bind(host: str) -> str:
    """Validate a literal IP address as loopback-only Gateway bind state."""

    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError(
            "Gateway bind host must be a literal loopback IP address."
        ) from error
    if not address.is_loopback:
        raise ValueError("Gateway bind host must be a loopback IP address.")
    return host


def create_gateway(
    route_resolver: GatewayRouteResolver,
    *,
    bind_host: str = "127.0.0.1",
    client_factory: Callable[[], Any] | None = None,
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
) -> Starlette:
    """Build the ASGI Gateway using injected route and HTTP client boundaries."""

    validate_loopback_bind(bind_host)
    if max_request_bytes <= 0:
        raise ValueError("Gateway request limit must be positive.")
    make_client = client_factory or (
        lambda: httpx.AsyncClient(timeout=None, trust_env=False)
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[Mapping[str, Any]]:
        async with make_client() as client:
            yield {"http_client": client}

    async def models(request: Request) -> JSONResponse:
        routes = await _await_if_needed(route_resolver.list_routes())
        data = []
        for route in sorted(routes, key=lambda item: item.service):
            item: dict[str, Any] = {
                "id": route.service,
                "object": "model",
                "created": 0,
                "owned_by": "mlxctl",
                "status": route.state,
            }
            if route.model is not None:
                item["model"] = route.model
            if route.runtime is not None:
                item["runtime"] = route.runtime
            data.append(item)
        return JSONResponse({"object": "list", "data": data})

    async def proxy(request: Request) -> JSONResponse | StreamingResponse:
        try:
            body = await _read_limited_body(request, max_request_bytes)
            payload = json.loads(body)
        except RequestTooLarge:
            return _error_response(
                413,
                "request_too_large",
                f"The request exceeds the {max_request_bytes}-byte Gateway limit.",
                action="Reduce the request size and retry.",
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _error_response(
                400,
                "invalid_json",
                "The request body must be a JSON object.",
                action="Send a valid OpenAI-compatible JSON request.",
            )
        if not isinstance(payload, dict):
            return _error_response(
                400,
                "invalid_json",
                "The request body must be a JSON object.",
                action="Send a valid OpenAI-compatible JSON request.",
            )
        service = payload.get("model")
        if not isinstance(service, str) or not service:
            return _error_response(
                400,
                "model_required",
                "The model field must name an Inference Service.",
                action="Set model to a service shown by mlxctl service list.",
                parameter="model",
            )

        route = await _await_if_needed(route_resolver.resolve(service))
        if route is None:
            return _error_response(
                404,
                "service_not_found",
                f"Inference Service {service!r} is not configured.",
                action="Run mlxctl service list to choose a configured service.",
                parameter="model",
            )
        if route.state == "stopped":
            return _error_response(
                409,
                "service_stopped",
                f"Inference Service {service!r} is stopped; requests never start services implicitly.",
                action=f"Run mlxctl service start {service} and retry the request.",
                parameter="model",
            )
        if route.state != "ready" or route.endpoint is None:
            return _error_response(
                503,
                "service_unavailable",
                f"Inference Service {service!r} is not ready.",
                action=f"Run mlxctl service inspect {service} for diagnostics.",
                parameter="model",
                retryable=True,
            )
        try:
            origin = _validated_upstream_origin(route.endpoint)
        except ValueError:
            return _error_response(
                502,
                "invalid_upstream_endpoint",
                f"Inference Service {service!r} has an invalid private Upstream Endpoint.",
                action=f"Run mlxctl service inspect {service} and restart the service.",
                parameter="model",
            )

        client = request.state.http_client
        upstream_request = client.build_request(
            request.method,
            f"{origin}{request.url.path}",
            content=body,
            headers={
                name: value
                for name, value in request.headers.items()
                if name.lower() in _REQUEST_HEADER_ALLOWLIST
            },
            params=request.query_params,
        )
        try:
            upstream = await client.send(upstream_request, stream=True)
        except (httpx.HTTPError, OSError):
            return _error_response(
                502,
                "upstream_unavailable",
                f"Inference Service {service!r} could not accept the request.",
                action=f"Run mlxctl service inspect {service} and retry.",
                parameter="model",
                retryable=True,
            )

        return _UpstreamStreamingResponse(
            upstream,
            status_code=upstream.status_code,
            headers={
                name: value
                for name, value in upstream.headers.items()
                if name.lower() in _RESPONSE_HEADER_ALLOWLIST
            },
        )

    return Starlette(
        routes=[
            Route("/v1/models", models, methods=["GET"]),
            Route("/v1/chat/completions", proxy, methods=["POST"]),
            Route("/v1/responses", proxy, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


class RequestTooLarge(ValueError):
    """The Gateway request exceeded its configured body limit."""


async def _read_limited_body(request: Request, limit: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > limit:
                raise RequestTooLarge
        except ValueError as error:
            raise RequestTooLarge from error
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise RequestTooLarge
        body.extend(chunk)
    return bytes(body)


def _validated_upstream_origin(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Upstream Endpoint must be a loopback HTTP origin.")
    try:
        address = ipaddress.ip_address(parsed.hostname)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Upstream Endpoint must be a loopback HTTP origin.") from error
    if not address.is_loopback or port is None:
        raise ValueError("Upstream Endpoint must be a loopback HTTP origin.")
    return endpoint.rstrip("/")


async def _await_if_needed(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    action: str,
    parameter: str | None = None,
    retryable: bool = False,
) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "message": message,
                "type": "mlxctl_gateway_error",
                "param": parameter,
                "code": code,
                "action": action,
                "retryable": retryable,
            }
        },
        status_code=status_code,
    )
