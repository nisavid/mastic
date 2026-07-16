#!/usr/bin/env python3
"""Authenticated loopback Responses gateway for Codex-compatible providers."""

from __future__ import annotations

import copy
import hmac
import http.client
import http.server
import ipaddress
import json
import math
import os
import queue
import re
import socket
import socketserver
import ssl
import sys
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


DELIMITER = "__"
ALLOWED_ROUTES = {
    ("GET", "/v1/models"): "/models",
    ("POST", "/v1/responses"): "/responses",
}
HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class ConfigurationError(ValueError):
    """Raised when the gateway configuration is unsafe or incomplete."""


class TransformationError(ValueError):
    """Raised when namespace flattening would be ambiguous or lossy."""


def _env_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError("boolean configuration must be true or false")


def _valid_bearer_value(value: str) -> bool:
    return bool(value) and all(33 <= ord(character) <= 126 for character in value)


def _is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass

    try:
        addresses = {
            result[4][0]
            for result in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        }
    except socket.gaierror:
        return False
    return bool(addresses) and all(ipaddress.ip_address(addr).is_loopback for addr in addresses)


@dataclass(frozen=True)
class ProxyConfig:
    """Validated process-local configuration for one gateway run."""

    upstream_url: str
    inbound_token: str
    upstream_token: str | None = None
    listen_host: str = "127.0.0.1"
    listen_port: int = 18999
    max_body_bytes: int = 8 * 1024 * 1024
    upstream_timeout_seconds: float = 30.0
    inbound_timeout_seconds: float = 30.0
    sse_heartbeat_seconds: float = 15.0
    adapter: str = "identity"
    namespace_map_capacity: int = 256
    debug: bool = False

    def __post_init__(self) -> None:
        parsed = urlsplit(self.upstream_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigurationError("NS_PROXY_UPSTREAM must be an explicit HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConfigurationError(
                "NS_PROXY_UPSTREAM must not contain credentials, a query, or a fragment"
            )
        if not _is_loopback_host(self.listen_host):
            raise ConfigurationError("NS_PROXY_HOST must resolve only to loopback addresses")
        if not (0 <= self.listen_port <= 65535):
            raise ConfigurationError("NS_PROXY_PORT must be between 0 and 65535")
        if len(self.inbound_token) < 32:
            raise ConfigurationError("NS_PROXY_INBOUND_TOKEN must be at least 32 characters")
        if not _valid_bearer_value(self.inbound_token):
            raise ConfigurationError("NS_PROXY_INBOUND_TOKEN contains invalid characters")
        if self.upstream_token is not None and not _valid_bearer_value(self.upstream_token):
            raise ConfigurationError("NS_PROXY_UPSTREAM_TOKEN contains invalid characters")
        if self.upstream_token is not None and hmac.compare_digest(
            self.inbound_token, self.upstream_token
        ):
            raise ConfigurationError("inbound and upstream tokens must be distinct")
        if self.max_body_bytes <= 0:
            raise ConfigurationError("NS_PROXY_MAX_BODY_BYTES must be positive")
        if self.upstream_timeout_seconds <= 0 or self.inbound_timeout_seconds <= 0:
            raise ConfigurationError("proxy timeouts must be positive")
        if not math.isfinite(self.sse_heartbeat_seconds) or self.sse_heartbeat_seconds <= 0:
            raise ConfigurationError("NS_PROXY_SSE_HEARTBEAT must be positive")
        if self.adapter not in {"identity", "codex-namespace"}:
            raise ConfigurationError("NS_PROXY_ADAPTER must be identity or codex-namespace")
        if self.namespace_map_capacity <= 0:
            raise ConfigurationError("NS_PROXY_NAMESPACE_MAP_CAPACITY must be positive")

    @classmethod
    def from_env(cls, env: dict[str, str] | os._Environ[str] = os.environ) -> "ProxyConfig":
        upstream = env.get("NS_PROXY_UPSTREAM")
        if not upstream:
            raise ConfigurationError("NS_PROXY_UPSTREAM is required")
        inbound_token = env.get("NS_PROXY_INBOUND_TOKEN")
        if not inbound_token:
            raise ConfigurationError("NS_PROXY_INBOUND_TOKEN is required")
        return cls(
            upstream_url=upstream,
            inbound_token=inbound_token,
            upstream_token=env.get("NS_PROXY_UPSTREAM_TOKEN") or None,
            listen_host=env.get("NS_PROXY_HOST", "127.0.0.1"),
            listen_port=int(env.get("NS_PROXY_PORT", "18999")),
            max_body_bytes=int(env.get("NS_PROXY_MAX_BODY_BYTES", str(8 * 1024 * 1024))),
            upstream_timeout_seconds=float(env.get("NS_PROXY_UPSTREAM_TIMEOUT", "30")),
            inbound_timeout_seconds=float(env.get("NS_PROXY_INBOUND_TIMEOUT", "30")),
            sse_heartbeat_seconds=float(env.get("NS_PROXY_SSE_HEARTBEAT", "15")),
            adapter=env.get("NS_PROXY_ADAPTER", "identity"),
            namespace_map_capacity=int(env.get("NS_PROXY_NAMESPACE_MAP_CAPACITY", "256")),
            debug=_env_bool(env.get("NS_PROXY_DEBUG")),
        )

    @property
    def upstream(self):
        return urlsplit(self.upstream_url)

    def upstream_path(self, suffix: str) -> str:
        base = self.upstream.path.rstrip("/")
        return f"{base}{suffix}" if base else f"/v1{suffix}"


def flatten_request(
    data: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, tuple[str, str]]]:
    """Return a transformed copy and its exact flattened-name reconstruction map."""
    transformed = copy.deepcopy(data)
    reconstruction: dict[str, tuple[str, str]] = {}
    ordinary_names = _ordinary_names(transformed)
    tools = transformed.get("tools")
    if isinstance(tools, list):
        flat: list[Any] = []
        for tool in tools:
            if isinstance(tool, dict) and tool.get("type") == "namespace":
                namespace = tool.get("name", "")
                if not isinstance(namespace, str) or not namespace:
                    raise TransformationError("namespace tools require a non-empty name")
                namespace_description = tool.get("description", "")
                for nested in tool.get("tools", []):
                    if isinstance(nested, dict) and nested.get("type") == "function":
                        nested = dict(nested)
                        call_name = nested.get("name", "")
                        if not isinstance(call_name, str) or not call_name:
                            raise TransformationError("namespace functions require a non-empty name")
                        flat_name = f"{namespace}{DELIMITER}{call_name}"
                        _register_flat_name(
                            reconstruction,
                            ordinary_names,
                            flat_name,
                            namespace,
                            call_name,
                            allow_existing=False,
                        )
                        nested["name"] = flat_name
                        description = nested.get("description", "")
                        if namespace_description and description:
                            nested["description"] = (
                                f"[{namespace}] {namespace_description}\n\n{description}"
                            )
                        elif namespace_description:
                            nested["description"] = f"[{namespace}] {namespace_description}"
                        flat.append(nested)
                    else:
                        raise TransformationError(
                            "namespace tools may contain only function children"
                        )
            else:
                flat.append(tool)
        transformed["tools"] = flat

    inputs = transformed.get("input")
    if isinstance(inputs, list):
        transformed["input"] = [
            _flatten_input_item(item, reconstruction, ordinary_names) for item in inputs
        ]
    return transformed, reconstruction


def _register_flat_name(
    reconstruction: dict[str, tuple[str, str]],
    ordinary_names: set[Any],
    flat_name: str,
    namespace: str,
    call_name: str,
    *,
    allow_existing: bool,
) -> None:
    expected = (namespace, call_name)
    if flat_name in ordinary_names:
        raise TransformationError(f"flattened tool name collision: {flat_name}")
    existing = reconstruction.get(flat_name)
    if existing is not None and (existing != expected or not allow_existing):
        raise TransformationError(f"flattened tool name collision: {flat_name}")
    reconstruction[flat_name] = expected


def _flatten_input_item(
    item: Any,
    reconstruction: dict[str, tuple[str, str]],
    ordinary_names: set[Any],
) -> Any:
    if not isinstance(item, dict):
        return item
    transformed = dict(item)
    item_type = transformed.get("type", "")

    if item_type == "custom_tool_call" and "namespace" in transformed:
        namespace = transformed.get("namespace")
        name = transformed.get("name")
        if not isinstance(namespace, str) or not namespace:
            raise TransformationError("history namespace must be a non-empty string")
        if not isinstance(name, str) or not name:
            raise TransformationError("history function name must be a non-empty string")
        raise TransformationError("namespaced custom tool history is not supported")

    if item_type == "function_call" and "namespace" in transformed:
        namespace = transformed.get("namespace")
        call_name = transformed.get("name")
        if not isinstance(namespace, str) or not namespace:
            raise TransformationError("history namespace must be a non-empty string")
        if not isinstance(call_name, str) or not call_name:
            raise TransformationError("history function name must be a non-empty string")
        transformed.pop("namespace")
        flat_name = f"{namespace}{DELIMITER}{call_name}"
        _register_flat_name(
            reconstruction,
            ordinary_names,
            flat_name,
            namespace,
            call_name,
            allow_existing=True,
        )
        transformed["name"] = flat_name

    for key, value in list(transformed.items()):
        if isinstance(value, list):
            transformed[key] = [
                _flatten_input_item(child, reconstruction, ordinary_names) for child in value
            ]
    return transformed


def transform_response(
    data: Any, reconstruction: dict[str, tuple[str, str]]
) -> Any:
    """Return a response copy with flattened call names reconstructed."""
    if not isinstance(data, dict):
        return data
    transformed = copy.deepcopy(data)
    item = transformed.get("item")
    if isinstance(item, dict):
        _split_call_name(item, reconstruction)
    response = transformed.get("response")
    if isinstance(response, dict):
        output = response.get("output")
        if isinstance(output, list):
            for output_item in output:
                _split_call_name(output_item, reconstruction)
    output = transformed.get("output")
    if isinstance(output, list):
        for output_item in output:
            _split_call_name(output_item, reconstruction)
    return transformed


def _split_call_name(item: Any, reconstruction: dict[str, tuple[str, str]]) -> None:
    if not isinstance(item, dict) or item.get("type") != "function_call":
        return
    name = item.get("name", "")
    if isinstance(name, str) and name in reconstruction:
        namespace, call_name = reconstruction[name]
        item["name"] = call_name
        item["namespace"] = namespace


def _safe_log(message: str) -> None:
    sys.stderr.write(f"[codex-ns-proxy] {message}\n")
    sys.stderr.flush()


def _bearer_token(header: str | None) -> str:
    if not header:
        return ""
    scheme, separator, token = header.partition(" ")
    if separator and scheme.lower() == "bearer":
        return token
    return ""


class _NamespaceState:
    """Bounded process-local reconstruction state for Responses continuation IDs."""

    def __init__(self, capacity: int):
        self._capacity = capacity
        self._maps: OrderedDict[str, dict[str, tuple[str, str]]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, response_id: str) -> dict[str, tuple[str, str]] | None:
        with self._lock:
            mapping = self._maps.get(response_id)
            if mapping is None:
                return None
            self._maps.move_to_end(response_id)
            return dict(mapping)

    def remember(self, response_id: str, mapping: dict[str, tuple[str, str]]) -> None:
        if not response_id:
            return
        with self._lock:
            self._maps[response_id] = dict(mapping)
            self._maps.move_to_end(response_id)
            while len(self._maps) > self._capacity:
                self._maps.popitem(last=False)


class _UpstreamLifecycle:
    """Own one upstream transport and its eventual response during shutdown."""

    def __init__(self, connection: http.client.HTTPConnection):
        self._connection = connection
        self._transport: socket.socket | None = None
        self._response: http.client.HTTPResponse | None = None
        self._closing = False
        self._lock = threading.Lock()

    def attach_response(self, response: http.client.HTTPResponse) -> bool:
        with self._lock:
            if not self._closing:
                self._response = response
                return True
        response.close()
        return False

    def capture_transport(self) -> None:
        """Retain the connected socket before HTTPResponse may detach it."""
        with self._lock:
            if not self._closing:
                self._transport = self._connection.sock

    def close(self, *, close_response: bool = True) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
            response = self._response
            transport = self._transport or getattr(self._connection, "sock", None)
        # Shut down the retained transport before closing its wrappers. A
        # terminal SSE reader closes its own response wrapper when its blocked
        # readline eventually returns; cross-thread response.close() can block.
        if transport is not None:
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            self._connection.close()
        except Exception:
            pass
        finally:
            if close_response and response is not None:
                try:
                    response.close()
                except Exception:
                    pass


class _SSELineReader:
    """Read one upstream SSE response without blocking downstream keep-alives."""

    def __init__(
        self,
        response: http.client.HTTPResponse,
        lifecycle: _UpstreamLifecycle,
    ) -> None:
        self._response = response
        self._lifecycle = lifecycle
        self._items: queue.Queue[tuple[str, bytes | Exception]] = queue.Queue(
            maxsize=64
        )
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._read,
            name="codex-ns-proxy-sse-reader",
            daemon=True,
        )
        self._thread.start()

    def next_item(
        self, timeout: float
    ) -> tuple[str, bytes | Exception] | None:
        try:
            return self._items.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self, *, wait_for_reader: bool = True) -> None:
        self._stopping.set()
        self._lifecycle.close(close_response=wait_for_reader)
        if wait_for_reader:
            self._thread.join(timeout=1.0)

    def _read(self) -> None:
        try:
            while not self._stopping.is_set():
                line = self._response.readline()
                if not line:
                    self._put(("eof", b""))
                    return
                if not self._put(("line", line)):
                    return
        except Exception as error:
            self._put(("error", error))
        finally:
            try:
                self._response.close()
            except Exception:
                pass

    def _put(self, item: tuple[str, bytes | Exception]) -> bool:
        while not self._stopping.is_set():
            try:
                self._items.put(item, timeout=0.1)
                return True
            except queue.Full:
                pass
        return False


def _merge_reconstruction(
    inherited: dict[str, tuple[str, str]], current: dict[str, tuple[str, str]]
) -> dict[str, tuple[str, str]]:
    merged = dict(inherited)
    for flat_name, target in current.items():
        existing = merged.get(flat_name)
        if existing is not None and existing != target:
            raise TransformationError(f"flattened tool name collision: {flat_name}")
        merged[flat_name] = target
    return merged


def _ordinary_names(data: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    tools = data.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type") not in {"function", "custom"}:
                continue
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                raise TransformationError("ordinary tools require a non-empty name")
            names.add(name)

    def collect_history_calls(items: list[Any]) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call" and "namespace" not in item:
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    raise TransformationError("ordinary history calls require a non-empty name")
                names.add(name)
            for value in item.values():
                if isinstance(value, list):
                    collect_history_calls(value)

    inputs = data.get("input")
    if isinstance(inputs, list):
        collect_history_calls(inputs)
    return names


def _response_ids(data: Any) -> set[str]:
    ids: set[str] = set()
    if not isinstance(data, dict):
        return ids
    if isinstance(data.get("id"), str) and data.get("id"):
        ids.add(data["id"])
    response = data.get("response")
    if isinstance(response, dict) and isinstance(response.get("id"), str) and response.get("id"):
        ids.add(response["id"])
    return ids


def _handler_for(config: ProxyConfig, namespace_state: _NamespaceState):
    class ProxyHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "codex-ns-proxy"
        sys_version = ""

        def setup(self) -> None:
            super().setup()
            self._response_started = False
            self.connection.settimeout(config.inbound_timeout_seconds)

        def do_GET(self) -> None:
            self._run_dispatch()

        def do_POST(self) -> None:
            self._run_dispatch()

        def do_PUT(self) -> None:
            self._run_dispatch()

        def do_PATCH(self) -> None:
            self._run_dispatch()

        def do_DELETE(self) -> None:
            self._run_dispatch()

        def do_OPTIONS(self) -> None:
            self._run_dispatch()

        def do_HEAD(self) -> None:
            self._run_dispatch()

        def do_TRACE(self) -> None:
            self._run_dispatch()

        def do_CONNECT(self) -> None:
            self._run_dispatch()

        def _run_dispatch(self) -> None:
            try:
                self._dispatch()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
                if config.debug:
                    _safe_log("downstream disconnected")
            except Exception as error:
                self.close_connection = True
                if config.debug:
                    _safe_log(f"internal error type={type(error).__name__}")
                if not self._response_started:
                    try:
                        self._send_json(500, {"error": {"message": "gateway error"}})
                    except (BrokenPipeError, ConnectionResetError):
                        pass

        def handle_expect_100(self) -> bool:
            """Reject unsafe requests without granting permission to send a body."""
            if not self._authenticated():
                self._send_json(401, {"error": {"message": "unauthorized"}})
                return False
            if (self.command, self.path) not in ALLOWED_ROUTES:
                self._send_route_error()
                return False
            if self.command == "POST" and self._validate_body_framing() is None:
                return False
            self.send_response_only(100)
            self.end_headers()
            return True

        def _dispatch(self) -> None:
            if not self._authenticated():
                self._send_json(401, {"error": {"message": "unauthorized"}})
                return

            suffix = ALLOWED_ROUTES.get((self.command, self.path))
            if suffix is None:
                self._send_route_error()
                return

            body = None
            reconstruction: dict[str, tuple[str, str]] = {}
            if self.command == "POST":
                body = self._read_json_body()
                if body is None:
                    return
                if config.adapter == "codex-namespace":
                    try:
                        previous_id = body.get("previous_response_id")
                        inherited: dict[str, tuple[str, str]] = {}
                        if previous_id is not None:
                            if not isinstance(previous_id, str) or not previous_id:
                                raise TransformationError(
                                    "previous_response_id must be a non-empty string"
                                )
                            inherited_mapping = namespace_state.get(previous_id)
                            if inherited_mapping is None:
                                raise TransformationError(
                                    "previous_response_id namespace state is unavailable"
                                )
                            inherited = inherited_mapping
                        current_ordinary_names = _ordinary_names(body)
                        body, current = flatten_request(body)
                        inherited_collisions = inherited.keys() & current_ordinary_names
                        if inherited_collisions:
                            raise TransformationError(
                                "inherited namespace mapping collides with current ordinary tool"
                            )
                        reconstruction = _merge_reconstruction(inherited, current)
                    except TransformationError as error:
                        self._send_json(400, {"error": {"message": str(error)}})
                        return
                    if config.debug:
                        namespace_count = len(
                            {namespace for namespace, _ in reconstruction.values()}
                        )
                        _safe_log(f"request transformed namespaces={namespace_count}")

            self._forward(suffix, body, reconstruction)

        def _authenticated(self) -> bool:
            values = self.headers.get_all("Authorization", failobj=[])
            if len(values) != 1:
                return False
            presented = _bearer_token(values[0])
            return hmac.compare_digest(presented, config.inbound_token)

        def _send_route_error(self) -> None:
            allowed = sorted(method for method, path in ALLOWED_ROUTES if path == self.path)
            headers = {"Allow": ", ".join(allowed)} if allowed else None
            self._send_json(
                405 if allowed else 404,
                {"error": {"message": "method not allowed" if allowed else "not found"}},
                headers,
            )

        def _validate_body_framing(self) -> int | None:
            if self.headers.get_all("Transfer-Encoding", failobj=[]):
                self._send_json(400, {"error": {"message": "transfer encoding is not supported"}})
                return None
            lengths = self.headers.get_all("Content-Length", failobj=[])
            if not lengths:
                self._send_json(411, {"error": {"message": "content length required"}})
                return None
            if len(lengths) != 1:
                self._send_json(400, {"error": {"message": "ambiguous content length"}})
                return None
            if re.fullmatch(r"[0-9]+", lengths[0], flags=re.ASCII) is None:
                self._send_json(400, {"error": {"message": "invalid content length"}})
                return None
            normalized_length = lengths[0].lstrip("0") or "0"
            maximum = str(config.max_body_bytes)
            if len(normalized_length) > len(maximum) or (
                len(normalized_length) == len(maximum) and normalized_length > maximum
            ):
                self._send_json(413, {"error": {"message": "request body too large"}})
                return None
            length = int(normalized_length)
            return length

        def _read_json_body(self) -> dict[str, Any] | None:
            length = self._validate_body_framing()
            if length is None:
                return None
            try:
                raw = self.rfile.read(length)
            except socket.timeout:
                self._send_json(408, {"error": {"message": "request body timeout"}})
                return None
            if len(raw) != length:
                self._send_json(400, {"error": {"message": "incomplete request body"}})
                return None
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": {"message": "invalid JSON"}})
                return None
            if not isinstance(parsed, dict):
                self._send_json(400, {"error": {"message": "JSON body must be an object"}})
                return None
            return parsed

        def _forward(
            self,
            suffix: str,
            body: dict[str, Any] | None,
            reconstruction: dict[str, tuple[str, str]],
        ) -> None:
            upstream = config.upstream
            port = upstream.port or (443 if upstream.scheme == "https" else 80)
            headers = self._forward_headers()
            encoded = None
            if body is not None:
                encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
                headers["Content-Type"] = "application/json"
                headers["Content-Length"] = str(len(encoded))

            connection_class = (
                http.client.HTTPSConnection
                if upstream.scheme == "https"
                else http.client.HTTPConnection
            )
            kwargs: dict[str, Any] = {"timeout": config.upstream_timeout_seconds}
            if upstream.scheme == "https":
                kwargs["context"] = ssl.create_default_context()
            connection = connection_class(upstream.hostname, port, **kwargs)
            lifecycle = _UpstreamLifecycle(connection)
            response = None
            if not self.server.track_upstream(lifecycle):
                lifecycle.close()
                self._send_json(503, {"error": {"message": "gateway shutting down"}})
                return
            try:
                connection.request(
                    self.command,
                    config.upstream_path(suffix),
                    body=encoded,
                    headers=headers,
                )
                lifecycle.capture_transport()
                response = connection.getresponse()
                if not self.server.register_upstream_response(lifecycle, response):
                    self.close_connection = True
                    if not self._response_started:
                        self._send_json(
                            503, {"error": {"message": "gateway shutting down"}}
                        )
                    return
                content_type = response.getheader("Content-Type", "")
                if "text/event-stream" in content_type.lower():
                    self._stream_sse(response, lifecycle, reconstruction)
                else:
                    self._forward_plain(response, reconstruction)
            except (TimeoutError, socket.timeout):
                self.close_connection = True
                if not self._response_started and not self.wfile.closed:
                    if self.server.is_closing():
                        self._send_json(503, {"error": {"message": "gateway shutting down"}})
                    else:
                        self._send_json(504, {"error": {"message": "upstream timeout"}})
            except (http.client.HTTPException, OSError) as error:
                self.close_connection = True
                if config.debug:
                    _safe_log(f"upstream transport error type={type(error).__name__}")
                if not self._response_started and not self.wfile.closed:
                    if self.server.is_closing():
                        self._send_json(503, {"error": {"message": "gateway shutting down"}})
                    else:
                        self._send_json(502, {"error": {"message": "upstream unavailable"}})
            finally:
                self.server.untrack_upstream(lifecycle)
                lifecycle.close()

        def _forward_headers(self) -> dict[str, str]:
            connection_names = {
                name.strip().lower()
                for value in self.headers.get_all("Connection", failobj=[])
                for name in value.split(",")
                if name.strip()
            }
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in HOP_BY_HOP_HEADERS
                and key.lower() not in connection_names
                and key.lower() != "authorization"
            }
            headers["Accept-Encoding"] = "identity"
            headers["Connection"] = "close"
            if config.upstream_token:
                headers["Authorization"] = f"Bearer {config.upstream_token}"
            return headers

        def _forward_plain(
            self,
            response: http.client.HTTPResponse,
            reconstruction: dict[str, tuple[str, str]],
        ) -> None:
            raw = response.read()
            content_type = response.getheader("Content-Type", "")
            if "json" in content_type.lower() and raw:
                try:
                    original = json.loads(raw)
                    transformed = transform_response(original, reconstruction)
                    for response_id in _response_ids(original):
                        namespace_state.remember(response_id, reconstruction)
                    if transformed != original:
                        raw = json.dumps(transformed, separators=(",", ":")).encode("utf-8")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
            self.close_connection = True
            self._response_started = True
            self.send_response(response.status)
            self._copy_response_headers(response)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                if config.debug:
                    _safe_log("downstream disconnected")

        def _stream_sse(
            self,
            response: http.client.HTTPResponse,
            lifecycle: _UpstreamLifecycle,
            reconstruction: dict[str, tuple[str, str]],
        ) -> None:
            self.close_connection = True
            self._response_started = True
            self.send_response(response.status)
            self._copy_response_headers(response)
            self.send_header("Connection", "close")
            self.end_headers()
            terminal_observed = False
            terminal_logged = False
            transport_done_observed = False
            frame: list[bytes] = []
            reader = _SSELineReader(response, lifecycle)
            try:
                while True:
                    item = reader.next_item(config.sse_heartbeat_seconds)
                    if item is None:
                        if not frame and not terminal_observed:
                            self.wfile.write(b": codex-ns-proxy keep-alive\n\n")
                            self.wfile.flush()
                        continue
                    kind, value = item
                    if kind == "error":
                        if config.debug:
                            _safe_log("SSE stream ended before upstream EOF")
                        break
                    upstream_eof = kind == "eof"
                    if kind == "eof":
                        if not frame:
                            break
                    else:
                        line = value
                        if not isinstance(line, bytes):
                            break
                        frame.append(line)
                        if line not in {b"\n", b"\r\n"}:
                            continue

                    transport_done = _is_sse_done_frame(frame)
                    transformed_frame, is_terminal, response_ids = _transform_sse_frame(
                        frame, reconstruction
                    )
                    for response_id in response_ids:
                        namespace_state.remember(response_id, reconstruction)
                    self.wfile.write(transformed_frame)
                    self.wfile.flush()
                    if is_terminal:
                        terminal_observed = True
                        if config.debug and not terminal_logged:
                            _safe_log("SSE terminal_completed=true")
                            terminal_logged = True
                    frame = []
                    if transport_done:
                        transport_done_observed = True
                    if upstream_eof or transport_done:
                        break
            except (
                BrokenPipeError,
                ConnectionResetError,
                TimeoutError,
                socket.timeout,
                http.client.HTTPException,
                OSError,
            ):
                if config.debug:
                    _safe_log("SSE stream ended before upstream EOF")
            finally:
                if config.debug and not terminal_logged:
                    _safe_log(f"SSE terminal_completed={str(terminal_observed).lower()}")
                reader.close(wait_for_reader=not transport_done_observed)

        def _copy_response_headers(self, response: http.client.HTTPResponse) -> None:
            response_headers = response.getheaders()
            connection_names = {
                name.strip().lower()
                for key, value in response_headers
                if key.lower() == "connection"
                for name in value.split(",")
                if name.strip()
            }
            for key, value in response_headers:
                if (
                    key.lower() not in HOP_BY_HOP_HEADERS
                    and key.lower() not in connection_names
                ):
                    self.send_header(key, value)

        def _send_json(
            self,
            status: int,
            payload: dict[str, Any],
            headers: dict[str, str] | None = None,
        ) -> None:
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.close_connection = True
            self._response_started = True
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        def log_message(self, _format: str, *args: Any) -> None:
            return

    return ProxyHandler


def _transform_sse_frame(
    lines: list[bytes], reconstruction: dict[str, tuple[str, str]]
) -> tuple[bytes, bool, set[str]]:
    raw = b"".join(lines)
    data_indexes: list[int] = []
    data_parts: list[bytes] = []
    event_name = b""
    for index, line in enumerate(lines):
        content = line[:-2] if line.endswith(b"\r\n") else line[:-1] if line.endswith(b"\n") else line
        if content.startswith(b"event:"):
            event_name = content[6:]
            if event_name.startswith(b" "):
                event_name = event_name[1:]
        elif content.startswith(b"data:"):
            payload = content[5:]
            if payload.startswith(b" "):
                payload = payload[1:]
            data_indexes.append(index)
            data_parts.append(payload)
    if not data_parts:
        return raw, False, set()
    payload = b"\n".join(data_parts)
    if not payload.strip() or payload.strip() == b"[DONE]":
        return raw, False, set()
    try:
        original = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw, False, set()
    response_ids = _response_ids(original)
    terminal = (
        isinstance(original, dict)
        and original.get("type") == "response.completed"
        and event_name in {b"", b"response.completed"}
    )
    transformed = transform_response(original, reconstruction)
    if transformed == original:
        return raw, terminal, response_ids

    first = data_indexes[0]
    newline = b"\r\n" if lines[first].endswith(b"\r\n") else b"\n"
    replacement = (
        b"data: "
        + json.dumps(transformed, separators=(",", ":")).encode("utf-8")
        + newline
    )
    rebuilt = [
        replacement if index == first else line
        for index, line in enumerate(lines)
        if index not in data_indexes[1:]
    ]
    return b"".join(rebuilt), terminal, response_ids


def _is_sse_done_frame(lines: list[bytes]) -> bool:
    data_parts: list[bytes] = []
    for line in lines:
        content = (
            line[:-2]
            if line.endswith(b"\r\n")
            else line[:-1]
            if line.endswith(b"\n")
            else line
        )
        if not content:
            continue
        if not content.startswith(b"data:"):
            return False
        payload = content[5:]
        if payload.startswith(b" "):
            payload = payload[1:]
        data_parts.append(payload)
    return data_parts == [b"[DONE]"]


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = True

    def __init__(self, *args: Any, **kwargs: Any):
        self._active_upstreams: set[_UpstreamLifecycle] = set()
        self._upstream_lock = threading.Lock()
        self._closing = False
        super().__init__(*args, **kwargs)

    def track_upstream(self, lifecycle: _UpstreamLifecycle) -> bool:
        with self._upstream_lock:
            if self._closing:
                return False
            self._active_upstreams.add(lifecycle)
            return True

    def register_upstream_response(
        self,
        lifecycle: _UpstreamLifecycle,
        response: http.client.HTTPResponse,
    ) -> bool:
        return lifecycle.attach_response(response)

    def untrack_upstream(self, lifecycle: _UpstreamLifecycle) -> None:
        with self._upstream_lock:
            self._active_upstreams.discard(lifecycle)

    def is_closing(self) -> bool:
        with self._upstream_lock:
            return self._closing

    def server_close(self) -> None:
        with self._upstream_lock:
            self._closing = True
            active = tuple(self._active_upstreams)
        try:
            for lifecycle in active:
                try:
                    lifecycle.close()
                except Exception:
                    pass
        finally:
            super().server_close()


def create_server(config: ProxyConfig) -> ThreadingHTTPServer:
    """Create a validated gateway server without contacting the upstream."""
    namespace_state = _NamespaceState(config.namespace_map_capacity)
    return ThreadingHTTPServer(
        (config.listen_host, config.listen_port), _handler_for(config, namespace_state)
    )


def main() -> int:
    try:
        config = ProxyConfig.from_env()
        server = create_server(config)
    except (ConfigurationError, ValueError) as error:
        _safe_log(f"configuration error: {error}")
        return 2

    host, port = server.server_address[:2]
    _safe_log(f"listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _safe_log("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
