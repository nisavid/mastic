"""Stable loopback Gateway for named mastic Inference Services."""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from mastic.infrastructure.responses_adapter import (
    DELIMITER,
    CodexNamespaceAdapter,
    ReconstructionMap,
    TransformationError,
    TransformationTooLarge,
)

RouteState = Literal["ready", "stopped", "unavailable"]
_REQUEST_HEADER_ALLOWLIST = frozenset(
    {"accept", "content-type", "user-agent", "x-request-id"}
)
_RESPONSE_HEADER_ALLOWLIST = frozenset(
    {"cache-control", "content-encoding", "content-type", "x-request-id"}
)
DEFAULT_MAX_REQUEST_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_RESPONSE_ADAPTATION_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_RESPONSE_ADAPTERS = 64
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
    """An application-target workload profile enforced before an upstream request."""

    service: str
    parameters: Mapping[str, object]


@dataclass(slots=True)
class _ResponsesSSEState:
    response_id: str | None = None
    next_sequence_number: int = 0

    def observe(self, frame: bytes) -> None:
        payload = _sse_json_payload(frame)
        if not isinstance(payload, dict):
            return
        sequence_number = payload.get("sequence_number")
        if type(sequence_number) is int:
            self.next_sequence_number = max(
                self.next_sequence_number, sequence_number + 1
            )
        response = payload.get("response")
        if isinstance(response, dict):
            response_id = response.get("id")
            if isinstance(response_id, str) and response_id:
                self.response_id = response_id
        response_id = payload.get("response_id")
        if isinstance(response_id, str) and response_id:
            self.response_id = response_id


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
        body: AsyncIterator[bytes] | None = None,
        on_close: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> None:
        self._upstream = upstream
        self._on_close = on_close
        content_type = upstream.headers.get("content-type", "").partition(";")[0]
        content_encoding = upstream.headers.get("content-encoding", "identity")
        if body is None:
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
            event = _pop_sse_message(pending, include_separator=False)
            if event is None:
                break
            if _is_terminal_sse_event(event):
                return
        if len(pending) > _SSE_INSPECTION_LIMIT:
            del pending[:-_SSE_INSPECTION_LIMIT]


async def _adapt_non_sse_response(
    upstream: httpx.Response,
    adapter: CodexNamespaceAdapter,
    reconstruction: ReconstructionMap,
    *,
    content_type: str,
    limit: int,
) -> bytes:
    body = bytearray()
    async for chunk in upstream.aiter_bytes():
        if len(body) + len(chunk) > limit:
            raise ResponseAdaptationTooLarge(limit)
        body.extend(chunk)
    raw = bytes(body)
    if "json" not in content_type or not raw:
        return raw
    try:
        original = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw
    try:
        return adapter.transform_response_body(
            original,
            raw,
            reconstruction,
            max_bytes=limit,
        )
    except TransformationTooLarge as error:
        raise ResponseAdaptationTooLarge(limit) from error


async def _prefetch_adapted_sse(
    upstream: httpx.Response,
    adapter: CodexNamespaceAdapter,
    reconstruction: ReconstructionMap,
    *,
    limit: int,
) -> AsyncIterator[bytes]:
    frames = _iter_bounded_sse_frames(upstream, limit).__aiter__()
    try:
        first_raw, complete = await anext(frames)
    except StopAsyncIteration:
        return _empty_chunks()
    if not complete:
        return _single_chunk(first_raw)
    first = _adapt_sse_frame(first_raw, adapter, reconstruction, limit)
    state = _ResponsesSSEState()
    state.observe(first_raw)
    return _iter_adapted_sse_after_first(
        first,
        first_raw,
        frames,
        adapter,
        reconstruction,
        limit,
        state,
    )


async def _iter_bounded_sse_frames(
    upstream: httpx.Response, limit: int
) -> AsyncIterator[tuple[bytes, bool]]:
    pending = bytearray()
    async for chunk in upstream.aiter_bytes():
        view = memoryview(chunk)
        offset = 0
        while offset < len(view):
            available = limit - len(pending)
            if available <= 0:
                raise ResponseAdaptationTooLarge(limit)
            take = min(available, len(view) - offset)
            pending.extend(view[offset : offset + take])
            offset += take
            while (
                frame := _pop_sse_message(pending, include_separator=True)
            ) is not None:
                yield frame, True
            if len(pending) == limit and offset < len(view):
                raise ResponseAdaptationTooLarge(limit)
    if pending:
        yield bytes(pending), False


async def _iter_adapted_sse_after_first(
    first: bytes,
    first_raw: bytes,
    frames: AsyncIterator[tuple[bytes, bool]],
    adapter: CodexNamespaceAdapter,
    reconstruction: ReconstructionMap,
    limit: int,
    state: _ResponsesSSEState,
) -> AsyncIterator[bytes]:
    yield first
    if _is_terminal_sse_event(first_raw):
        return

    try:
        async for frame, complete in frames:
            if not complete:
                yield frame
                return
            try:
                transformed = _adapt_sse_frame(frame, adapter, reconstruction, limit)
            except ResponseAdaptationTooLarge:
                yield _response_adaptation_failed_sse(state)
                return
            state.observe(frame)
            yield transformed
            if _is_terminal_sse_event(frame):
                return
    except ResponseAdaptationTooLarge:
        yield _response_adaptation_failed_sse(state)


def _adapt_sse_frame(
    frame: bytes,
    adapter: CodexNamespaceAdapter,
    reconstruction: ReconstructionMap,
    limit: int,
) -> bytes:
    if len(frame) > limit:
        raise ResponseAdaptationTooLarge(limit)
    try:
        return adapter.transform_sse_frame(frame, reconstruction, max_bytes=limit)
    except TransformationTooLarge as error:
        raise ResponseAdaptationTooLarge(limit) from error


async def _single_chunk(chunk: bytes) -> AsyncIterator[bytes]:
    yield chunk


async def _empty_chunks() -> AsyncIterator[bytes]:
    if False:
        yield b""


def _response_adaptation_failed_sse(state: _ResponsesSSEState) -> bytes:
    if state.response_id is None:
        event_name = "error"
        payload: dict[str, Any] = {
            "type": "error",
            "code": "response_adaptation_too_large",
            "message": "MASTIC could not safely adapt the upstream response.",
            "param": None,
            "sequence_number": state.next_sequence_number,
        }
    else:
        event_name = "response.failed"
        payload = {
            "type": "response.failed",
            "response": {
                "id": state.response_id,
                "object": "response",
                "created_at": 0,
                "status": "failed",
                "completed_at": None,
                "error": {
                    "code": "server_error",
                    "message": "MASTIC could not safely adapt the upstream response.",
                },
                "incomplete_details": None,
                "instructions": None,
                "max_output_tokens": None,
                "model": "mastic-gateway",
                "output": [],
                "previous_response_id": None,
                "reasoning_effort": None,
                "store": False,
                "temperature": 1,
                "text": {"format": {"type": "text"}},
                "tool_choice": "auto",
                "tools": [],
                "top_p": 1,
                "truncation": "disabled",
                "usage": None,
                "user": None,
                "metadata": {},
            },
            "sequence_number": state.next_sequence_number,
        }
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    return f"event: {event_name}\n".encode() + b"data: " + encoded + b"\n\n"


def _pop_sse_message(pending: bytearray, *, include_separator: bool) -> bytes | None:
    boundaries = tuple(
        (index, separator)
        for separator in (b"\r\n\r\n", b"\n\n", b"\r\r")
        if (index := pending.find(separator)) >= 0
    )
    if not boundaries:
        return None
    index, separator = min(boundaries, key=lambda item: item[0])
    end = index + len(separator)
    message = bytes(pending[: end if include_separator else index])
    del pending[:end]
    return message


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


def _sse_json_payload(event: bytes) -> Any:
    data_lines = []
    for line in event.splitlines():
        field, separator, value = line.partition(b":")
        if separator and field == b"data":
            data_lines.append(value.lstrip(b" "))
    data = b"\n".join(data_lines).strip()
    if not data or data == b"[DONE]":
        return None
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


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
    max_response_adaptation_bytes: int = DEFAULT_MAX_RESPONSE_ADAPTATION_BYTES,
    max_response_adapters: int = DEFAULT_MAX_RESPONSE_ADAPTERS,
    upstream_response_timeout: float = DEFAULT_UPSTREAM_RESPONSE_TIMEOUT,
    activity: GatewayActivity | None = None,
    authenticate: Callable[[str | None], bool] | None = None,
    profile_resolver: Callable[[str, str], GatewayRequestProfile | None | Any]
    | None = None,
) -> Starlette:
    """Build the ASGI Gateway using injected route and HTTP client boundaries."""

    validate_loopback_bind(bind_host)
    if (
        max_request_bytes <= 0
        or max_response_adaptation_bytes <= 0
        or max_response_adapters <= 0
        or upstream_response_timeout <= 0
    ):
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
    responses_adapters: OrderedDict[str, CodexNamespaceAdapter] = OrderedDict()

    def response_adapter(service: str) -> CodexNamespaceAdapter:
        adapter = responses_adapters.get(service)
        if adapter is None:
            adapter = CodexNamespaceAdapter()
            responses_adapters[service] = adapter
            if len(responses_adapters) > max_response_adapters:
                responses_adapters.popitem(last=False)
        else:
            responses_adapters.move_to_end(service)
        return adapter

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

    async def proxy(request: Request) -> Response:
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
                action="Use a native local application or a loopback-hosted application.",
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
        responses_adapter = response_adapter(service) if is_responses else None
        reconstruction: ReconstructionMap = {}
        application_target_name = request.path_params.get("application_target")
        profile_name = request.path_params.get("profile")
        if application_target_name is not None and profile_name is not None:
            profile = (
                None
                if profile_resolver is None
                else await _await_if_needed(
                    profile_resolver(str(application_target_name), str(profile_name))
                )
            )
            if profile is None:
                return _error_response(
                    404,
                    "profile_not_found",
                    f"Application Configuration Target profile {application_target_name}/{profile_name} is not configured.",
                    action="Reconfigure the Application Configuration Target with MASTIC and retry.",
                    parameter="profile",
                )
            if profile.service != service:
                return _error_response(
                    400,
                    "profile_service_mismatch",
                    f"Application Configuration Target profile {application_target_name}/{profile_name} targets {profile.service!r}, not {service!r}.",
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
                _enforce_responses_transform_budget(payload, max_request_bytes)
                payload, reconstruction = responses_adapter.transform_request(payload)
            except RequestTooLarge:
                return _error_response(
                    413,
                    "request_too_large",
                    f"The request exceeds the {max_request_bytes}-byte Gateway limit.",
                    action="Reduce the request size and retry.",
                )
            except TransformationError as error:
                return _error_response(
                    400,
                    "invalid_namespace",
                    str(error),
                    action="Correct the Responses namespace tools or continuation and retry.",
                )
        try:
            body = _encode_limited_json(payload, max_request_bytes)
        except RequestTooLarge:
            return _error_response(
                413,
                "request_too_large",
                f"The request exceeds the {max_request_bytes}-byte Gateway limit.",
                action="Reduce the request size and retry.",
            )
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

        ownership_transferred = False
        try:
            if is_responses and not _has_identity_content_encoding(upstream):
                return _error_response(
                    502,
                    "unsupported_upstream_response_encoding",
                    f"Inference Service {service!r} returned a compressed Responses body that MASTIC cannot adapt safely.",
                    action="Configure the Inference Service to return Content-Encoding: identity and retry.",
                    parameter="model",
                )

            response_headers = {
                name: value
                for name, value in upstream.headers.items()
                if name.lower() in _RESPONSE_HEADER_ALLOWLIST
                and (not is_responses or name.lower() != "content-encoding")
            }
            if is_responses:
                assert responses_adapter is not None
                content_type = (
                    upstream.headers.get("content-type", "")
                    .partition(";")[0]
                    .strip()
                    .lower()
                )
                if content_type == "text/event-stream":
                    adapted_stream = await _prefetch_adapted_sse(
                        upstream,
                        responses_adapter,
                        reconstruction,
                        limit=max_response_adaptation_bytes,
                    )
                    response = _UpstreamStreamingResponse(
                        upstream,
                        body=adapted_stream,
                        on_close=(
                            (lambda: activity.end(service))
                            if activity is not None
                            else None
                        ),
                        status_code=upstream.status_code,
                        headers=response_headers,
                    )
                    ownership_transferred = True
                    return response

                adapted_body = await _adapt_non_sse_response(
                    upstream,
                    responses_adapter,
                    reconstruction,
                    content_type=content_type,
                    limit=max_response_adaptation_bytes,
                )
                return Response(
                    adapted_body,
                    status_code=upstream.status_code,
                    headers=response_headers,
                )

            response = _UpstreamStreamingResponse(
                upstream,
                on_close=(
                    (lambda: activity.end(service)) if activity is not None else None
                ),
                status_code=upstream.status_code,
                headers=response_headers,
            )
            ownership_transferred = True
            return response
        except ResponseAdaptationTooLarge:
            return _response_adaptation_error(max_response_adaptation_bytes)
        except (httpx.HTTPError, OSError):
            return _error_response(
                502,
                "upstream_unavailable",
                f"Inference Service {service!r} stopped while returning a response.",
                action=f"Run mastic service inspect {service} and retry.",
                parameter="model",
                retryable=True,
            )
        finally:
            if not ownership_transferred:
                await _close_upstream_and_release(
                    upstream, activity, service, suppress_errors=True
                )

    return Starlette(
        routes=[
            Route("/v1/models", models, methods=["GET"]),
            Route("/v1/chat/completions", proxy, methods=["POST"]),
            Route("/v1/responses", proxy, methods=["POST"]),
            Route(
                "/application-targets/{application_target}/profiles/{profile}/v1/models",
                models,
                methods=["GET"],
            ),
            Route(
                "/application-targets/{application_target}/profiles/{profile}/v1/chat/completions",
                proxy,
                methods=["POST"],
            ),
            Route(
                "/application-targets/{application_target}/profiles/{profile}/v1/responses",
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


class ResponseAdaptationTooLarge(RuntimeError):
    """A Responses body exceeded the memory allowed for safe adaptation."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"response adaptation exceeded the {limit}-byte limit")


async def _close_upstream_and_release(
    upstream: httpx.Response,
    activity: GatewayActivity | None,
    service: str,
    *,
    suppress_errors: bool,
) -> None:
    try:
        await upstream.aclose()
    except Exception:
        if not suppress_errors:
            raise
    finally:
        if activity is not None:
            activity.end(service)


def _response_adaptation_error(limit: int) -> JSONResponse:
    return _error_response(
        502,
        "response_adaptation_too_large",
        f"The upstream response adaptation exceeded the {limit}-byte limit.",
        action="Reduce the upstream response size and retry.",
    )


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


def _encode_limited_json(payload: Any, limit: int) -> bytes:
    body = bytearray()
    encoder = json.JSONEncoder(separators=(",", ":"))
    for chunk in encoder.iterencode(payload):
        encoded = chunk.encode()
        if len(body) + len(encoded) > limit:
            raise RequestTooLarge
        body.extend(encoded)
    return bytes(body)


def _enforce_responses_transform_budget(payload: dict[str, Any], limit: int) -> None:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return

    namespaces: list[tuple[str, str, list[Any]]] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "namespace":
            continue
        namespace = tool.get("name")
        description = tool.get("description", "")
        children = tool.get("tools")
        if (
            not isinstance(namespace, str)
            or not namespace
            or not isinstance(description, str)
            or not isinstance(children, list)
            or any(
                not isinstance(child, dict)
                or child.get("type") != "function"
                or not isinstance(child.get("name"), str)
                or not child.get("name")
                or not isinstance(child.get("description", ""), str)
                for child in children
            )
        ):
            return
        namespaces.append((namespace, description, children))

    transformed_string_bytes = 0
    for namespace, namespace_description, children in namespaces:
        namespace_bytes = len(namespace.encode())
        namespace_description_bytes = len(namespace_description.encode())
        for child in children:
            child_name = child["name"]
            child_description = child.get("description", "")
            transformed_string_bytes += (
                namespace_bytes + len(DELIMITER) + len(child_name.encode())
            )
            if namespace_description:
                transformed_string_bytes += (
                    namespace_bytes
                    + namespace_description_bytes
                    + len(child_description.encode())
                    + (5 if child_description else 3)
                )
            if transformed_string_bytes > limit:
                raise RequestTooLarge


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


def _has_identity_content_encoding(upstream: httpx.Response) -> bool:
    return upstream.headers.get("content-encoding", "").strip().lower() in {
        "",
        "identity",
    }


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
        action="Configure the Application Configuration Target through MASTIC or read the credential location with mastic gateway inspect.",
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
