"""Stable loopback Gateway for named mastic Inference Services."""

from __future__ import annotations

import asyncio
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

from mastic.infrastructure.responses_adapter import (
    CodexNamespaceAdapter,
    ReconstructionMap,
    TransformationError,
)

RouteState = Literal["ready", "stopped", "unavailable"]
_REQUEST_HEADER_ALLOWLIST = frozenset(
    {"accept", "content-type", "user-agent", "x-request-id"}
)
_RESPONSE_HEADER_ALLOWLIST = frozenset(
    {"cache-control", "content-encoding", "content-type", "x-request-id"}
)
DEFAULT_MAX_REQUEST_BYTES = 4 * 1024 * 1024
DEFAULT_UPSTREAM_RESPONSE_TIMEOUT = 30.0
_SSE_INSPECTION_LIMIT = 1024 * 1024
_TERMINAL_RESPONSE_EVENTS = frozenset(
    {"response.completed", "response.failed", "response.incomplete"}
)


@dataclass(frozen=True, slots=True)
class GatewayRoute:
    """A public service identity and its current private routing state."""

    service: str
    state: RouteState
    endpoint: str | None = None
    model: str | None = None
    runtime: str | None = None


@dataclass(frozen=True, slots=True)
class GatewayRequestProfile:
    """A client workload profile enforced before an upstream request."""

    service: str
    parameters: Mapping[str, object]


class GatewayRouteResolver(Protocol):
    """Resolve current Gateway Routes without exposing arbitrary destinations."""

    def list_routes(self) -> Iterable[GatewayRoute] | Any: ...

    def resolve(self, service: str) -> GatewayRoute | None | Any: ...


class GatewayActivity(Protocol):
    """Track in-flight work so pressure policy never evicts a busy service."""

    def begin(self, service: str) -> bool: ...

    def end(self, service: str) -> None: ...


class _UpstreamStreamingResponse(StreamingResponse):
    """Streaming response that closes upstream even when downstream send fails."""

    def __init__(
        self,
        upstream: httpx.Response,
        *,
        responses_adapter: CodexNamespaceAdapter | None = None,
        reconstruction: ReconstructionMap | None = None,
        on_close: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> None:
        self._upstream = upstream
        self._on_close = on_close
        content_type = upstream.headers.get("content-type", "").partition(";")[0]
        content_encoding = upstream.headers.get("content-encoding", "identity")
        if responses_adapter is not None:
            body = _iter_adapted_responses(
                upstream,
                responses_adapter,
                reconstruction or {},
                content_type=content_type.strip().lower(),
            )
        else:
            body = (
                _iter_sse_until_terminal(upstream)
                if content_type.strip().lower() == "text/event-stream"
                and content_encoding.strip().lower() in {"", "identity"}
                else upstream.aiter_raw()
            )
        super().__init__(body, **kwargs)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self._upstream.aclose()
            finally:
                if self._on_close is not None:
                    self._on_close()
                    self._on_close = None


async def _iter_sse_until_terminal(
    upstream: httpx.Response,
) -> AsyncIterator[bytes]:
    """Stop an SSE transport after its protocol-level terminal event."""

    pending = bytearray()
    async for chunk in upstream.aiter_raw():
        yield chunk
        pending.extend(chunk)
        while True:
            event = _pop_sse_event(pending)
            if event is None:
                break
            if _is_terminal_sse_event(event):
                return
        if len(pending) > _SSE_INSPECTION_LIMIT:
            del pending[:-_SSE_INSPECTION_LIMIT]


async def _iter_adapted_responses(
    upstream: httpx.Response,
    adapter: CodexNamespaceAdapter,
    reconstruction: ReconstructionMap,
    *,
    content_type: str,
) -> AsyncIterator[bytes]:
    if content_type != "text/event-stream":
        raw = b"".join([chunk async for chunk in upstream.aiter_bytes()])
        if "json" not in content_type or not raw:
            yield raw
            return
        try:
            original = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            yield raw
            return
        transformed = adapter.transform_response(original, reconstruction)
        yield (
            raw
            if transformed == original
            else json.dumps(transformed, separators=(",", ":")).encode()
        )
        return

    pending = bytearray()
    async for chunk in upstream.aiter_bytes():
        pending.extend(chunk)
        while True:
            frame = _pop_sse_frame(pending)
            if frame is None:
                break
            transformed = adapter.transform_sse_frame(frame, reconstruction)
            yield transformed
            if _is_terminal_sse_event(frame):
                return
    if pending:
        yield bytes(pending)


def _pop_sse_frame(pending: bytearray) -> bytes | None:
    boundaries = tuple(
        (index, separator)
        for separator in (b"\r\n\r\n", b"\n\n", b"\r\r")
        if (index := pending.find(separator)) >= 0
    )
    if not boundaries:
        return None
    index, separator = min(boundaries, key=lambda item: item[0])
    end = index + len(separator)
    frame = bytes(pending[:end])
    del pending[:end]
    return frame


def _pop_sse_event(pending: bytearray) -> bytes | None:
    boundaries = tuple(
        (index, separator)
        for separator in (b"\r\n\r\n", b"\n\n", b"\r\r")
        if (index := pending.find(separator)) >= 0
    )
    if not boundaries:
        return None
    index, separator = min(boundaries, key=lambda item: item[0])
    event = bytes(pending[:index])
    del pending[: index + len(separator)]
    return event


def _is_terminal_sse_event(event: bytes) -> bool:
    event_name = ""
    data_lines: list[bytes] = []
    for line in event.splitlines():
        field, separator, value = line.partition(b":")
        if not separator:
            continue
        value = value.lstrip(b" ")
        if field == b"event":
            event_name = value.decode("utf-8", errors="replace")
        elif field == b"data":
            data_lines.append(value)
    if event_name in _TERMINAL_RESPONSE_EVENTS:
        return True
    data = b"\n".join(data_lines).strip()
    if data == b"[DONE]":
        return True
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict) and payload.get("type") in _TERMINAL_RESPONSE_EVENTS
    )


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
    upstream_response_timeout: float = DEFAULT_UPSTREAM_RESPONSE_TIMEOUT,
    activity: GatewayActivity | None = None,
    authenticate: Callable[[str | None], bool] | None = None,
    profile_resolver: Callable[[str, str], GatewayRequestProfile | None | Any]
    | None = None,
) -> Starlette:
    """Build the ASGI Gateway using injected route and HTTP client boundaries."""

    validate_loopback_bind(bind_host)
    if max_request_bytes <= 0 or upstream_response_timeout <= 0:
        raise ValueError("Gateway request limits must be positive.")
    make_client = client_factory or (
        lambda: httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=None,
                write=30.0,
                pool=5.0,
            ),
            trust_env=False,
        )
    )
    responses_adapters: dict[str, CodexNamespaceAdapter] = {}

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[Mapping[str, Any]]:
        async with make_client() as client:
            yield {"http_client": client}

    async def models(request: Request) -> JSONResponse:
        denied = _authenticate(request, authenticate)
        if denied is not None:
            return denied
        routes = await _await_if_needed(route_resolver.list_routes())
        data = []
        for route in sorted(routes, key=lambda item: item.service):
            item: dict[str, Any] = {
                "id": route.service,
                "object": "model",
                "created": 0,
                "owned_by": "mastic",
                "status": route.state,
            }
            if route.model is not None:
                item["model"] = route.model
            if route.runtime is not None:
                item["runtime"] = route.runtime
            data.append(item)
        return JSONResponse({"object": "list", "data": data})

    async def proxy(request: Request) -> JSONResponse | StreamingResponse:
        denied = _authenticate(request, authenticate)
        if denied is not None:
            return denied
        media_type = request.headers.get("content-type", "").partition(";")[0]
        if media_type.strip().lower() != "application/json":
            return _error_response(
                415,
                "unsupported_media_type",
                "The Gateway accepts application/json request bodies only.",
                action="Set Content-Type to application/json and retry.",
            )
        if not _origin_is_allowed(request.headers.get("origin")):
            return _error_response(
                403,
                "origin_not_allowed",
                "Browser requests must originate from a loopback HTTP origin.",
                action="Use a native local client or a loopback-hosted application.",
            )
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
                action="Set model to a service shown by mastic service list.",
                parameter="model",
            )

        route = await _await_if_needed(route_resolver.resolve(service))
        if route is None:
            return _error_response(
                404,
                "service_not_found",
                f"Inference Service {service!r} is not configured.",
                action="Run mastic service list to choose a configured service.",
                parameter="model",
            )
        if route.state == "stopped":
            return _error_response(
                409,
                "service_stopped",
                f"Inference Service {service!r} is stopped; requests never start services implicitly.",
                action=f"Run mastic service start {service} and retry the request.",
                parameter="model",
            )
        if route.state != "ready" or route.endpoint is None:
            return _error_response(
                503,
                "service_unavailable",
                f"Inference Service {service!r} is not ready.",
                action=f"Run mastic service inspect {service} for diagnostics.",
                parameter="model",
                retryable=True,
            )
        is_responses = request.url.path.endswith("/responses")
        responses_adapter = (
            responses_adapters.setdefault(service, CodexNamespaceAdapter())
            if is_responses
            else None
        )
        reconstruction: ReconstructionMap = {}
        client_name = request.path_params.get("client")
        profile_name = request.path_params.get("profile")
        if client_name is not None and profile_name is not None:
            profile = (
                None
                if profile_resolver is None
                else await _await_if_needed(
                    profile_resolver(str(client_name), str(profile_name))
                )
            )
            if profile is None:
                return _error_response(
                    404,
                    "profile_not_found",
                    f"Client generation profile {client_name}/{profile_name} is not configured.",
                    action="Reconfigure the client with mastic and retry.",
                    parameter="profile",
                )
            if profile.service != service:
                return _error_response(
                    400,
                    "profile_service_mismatch",
                    f"Client generation profile {client_name}/{profile_name} targets {profile.service!r}, not {service!r}.",
                    action=f"Set model to {profile.service!r} and retry.",
                    parameter="model",
                )
            payload = _apply_request_profile(
                payload,
                profile.parameters,
                responses=is_responses,
            )
        if is_responses:
            assert responses_adapter is not None
            try:
                payload, reconstruction = responses_adapter.transform_request(payload)
            except TransformationError as error:
                return _error_response(
                    400,
                    "invalid_namespace",
                    str(error),
                    action="Correct the Responses namespace tools or continuation and retry.",
                )
        body = json.dumps(payload, separators=(",", ":")).encode()
        try:
            origin = _validated_upstream_origin(route.endpoint)
        except ValueError:
            return _error_response(
                502,
                "invalid_upstream_endpoint",
                f"Inference Service {service!r} has an invalid private Upstream Endpoint.",
                action=f"Run mastic service inspect {service} and restart the service.",
                parameter="model",
            )

        admitted = activity is None or activity.begin(service)
        if not admitted:
            return _error_response(
                429,
                "service_busy",
                f"Inference Service {service!r} has reached its concurrent request limit.",
                action="Wait for an active request to finish and retry.",
                parameter="model",
                retryable=True,
            )

        client = request.state.http_client
        upstream_request = client.build_request(
            request.method,
            f"{origin}{_upstream_path(request.url.path)}",
            content=body,
            headers={
                name: value
                for name, value in request.headers.items()
                if name.lower() in _REQUEST_HEADER_ALLOWLIST
            }
            | ({"accept-encoding": "identity"} if is_responses else {}),
            params=request.query_params,
        )
        try:
            upstream = await asyncio.wait_for(
                client.send(upstream_request, stream=True),
                timeout=upstream_response_timeout,
            )
        except (TimeoutError, httpx.HTTPError, OSError):
            if activity is not None:
                activity.end(service)
            return _error_response(
                502,
                "upstream_unavailable",
                f"Inference Service {service!r} could not accept the request.",
                action=f"Run mastic service inspect {service} and retry.",
                parameter="model",
                retryable=True,
            )
        except BaseException:
            if activity is not None:
                activity.end(service)
            raise

        return _UpstreamStreamingResponse(
            upstream,
            responses_adapter=responses_adapter,
            reconstruction=reconstruction,
            on_close=(lambda: activity.end(service)) if activity is not None else None,
            status_code=upstream.status_code,
            headers={
                name: value
                for name, value in upstream.headers.items()
                if name.lower() in _RESPONSE_HEADER_ALLOWLIST
                and (not is_responses or name.lower() != "content-encoding")
            },
        )

    return Starlette(
        routes=[
            Route("/v1/models", models, methods=["GET"]),
            Route("/v1/chat/completions", proxy, methods=["POST"]),
            Route("/v1/responses", proxy, methods=["POST"]),
            Route(
                "/clients/{client}/profiles/{profile}/v1/models",
                models,
                methods=["GET"],
            ),
            Route(
                "/clients/{client}/profiles/{profile}/v1/chat/completions",
                proxy,
                methods=["POST"],
            ),
            Route(
                "/clients/{client}/profiles/{profile}/v1/responses",
                proxy,
                methods=["POST"],
            ),
        ],
        lifespan=lifespan,
    )


def _upstream_path(path: str) -> str:
    marker = "/v1/"
    _prefix, separator, suffix = path.rpartition(marker)
    if not separator:
        raise ValueError("Gateway route lacks an OpenAI-compatible path")
    return marker + suffix


def _apply_request_profile(
    payload: Mapping[str, object],
    parameters: Mapping[str, object],
    *,
    responses: bool,
) -> dict[str, object]:
    result = dict(payload)
    supported = (
        {"temperature", "top_p", "top_k"}
        if responses
        else {
            "temperature",
            "top_p",
            "top_k",
            "min_p",
            "presence_penalty",
            "repetition_penalty",
            "max_tokens",
        }
    )
    for key in supported:
        if key in parameters:
            result[key] = parameters[key]
    template_parameters = {
        key: parameters[key]
        for key in ("enable_thinking", "preserve_thinking")
        if key in parameters
    }
    if template_parameters:
        raw_kwargs = result.get("chat_template_kwargs", {})
        kwargs = dict(raw_kwargs) if isinstance(raw_kwargs, Mapping) else {}
        kwargs.update(template_parameters)
        result["chat_template_kwargs"] = kwargs
    return result


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


def _origin_is_allowed(origin: str | None) -> bool:
    if origin is None:
        return True
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return False
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def _authenticate(
    request: Request, authenticate: Callable[[str | None], bool] | None
) -> JSONResponse | None:
    if authenticate is None or authenticate(request.headers.get("authorization")):
        return None
    response = _error_response(
        401,
        "authentication_required",
        "The Gateway requires its private bearer credential.",
        action="Configure the client through mastic or read the credential location with mastic gateway inspect.",
    )
    response.headers["www-authenticate"] = "Bearer"
    return response


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
                "type": "mastic_gateway_error",
                "param": parameter,
                "code": code,
                "action": action,
                "retryable": retryable,
            }
        },
        status_code=status_code,
    )
