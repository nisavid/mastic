"""Transparent loopback HTTP proxy with request metric capture."""

from __future__ import annotations

import http.client
import json
import logging
import socket
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable, Protocol

from .adapters import Endpoint
from .metrics import RequestMetricEvent, RequestOutcome

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_MAX_PARSE_BYTES = 1024 * 1024
_LOGGER = logging.getLogger(__name__)


class _MetricRecorder(Protocol):
    def record(self, event: RequestMetricEvent) -> None: ...


class MetricsProxy:
    """Forward endpoints; the v1 request-body limit is 32 MiB."""

    MAX_REQUEST_BODY_BYTES = 32 * 1024 * 1024
    DOWNSTREAM_IO_TIMEOUT_SECONDS = 30
    UPSTREAM_IO_TIMEOUT_SECONDS = 30
    HTTP_CONNECTION_CLASS = http.client.HTTPConnection

    def __init__(
        self,
        client_endpoint: Endpoint,
        upstream_endpoint: Endpoint,
        engine: _MetricRecorder,
        server_id: str,
        model_alias: str,
        run_id: str,
    ) -> None:
        self.client_endpoint = client_endpoint
        self.upstream_endpoint = upstream_endpoint
        self._engine = engine
        self._server_id = server_id
        self._model_alias = model_alias
        self._run_id = run_id
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._active_lock = threading.Lock()
        self._downstream_sockets: set[socket.socket] = set()
        self._upstream_connections: dict[
            http.client.HTTPConnection, socket.socket | None
        ] = {}
        self._active_empty = threading.Event()
        self._active_empty.set()
        self._stopping = False

    def __enter__(self) -> MetricsProxy:
        if self._server is not None:
            raise RuntimeError("metrics proxy is already running")
        with self._active_lock:
            self._stopping = False
        proxy = self

        class Handler(_ProxyHandler):
            pass

        Handler.proxy = proxy
        server_type = (
            _IPv6ThreadingHTTPServer
            if ":" in self.client_endpoint.host
            else _ThreadingHTTPServer
        )
        self._server = server_type(
            (self.client_endpoint.host, self.client_endpoint.port), Handler
        )
        server = self._server
        self._thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.05),
            name=f"metrics-proxy-{self._server_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        with self._active_lock:
            self._stopping = True
        if server is not None:
            server.shutdown()
            server.server_close()
        with self._active_lock:
            downstream = tuple(self._downstream_sockets)
            upstream = tuple(self._upstream_connections.items())
        for connection, tracked_socket in upstream:
            upstream_socket = tracked_socket or connection.sock
            if upstream_socket is not None:
                try:
                    upstream_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                upstream_socket.close()
        for connection in downstream:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        self._active_empty.wait(0.5)
        if thread is not None:
            thread.join(1)

    def _register_downstream(self, connection: socket.socket) -> bool:
        with self._active_lock:
            if self._stopping:
                return False
            self._downstream_sockets.add(connection)
            self._active_empty.clear()
            return True

    def _unregister_downstream(self, connection: socket.socket) -> None:
        with self._active_lock:
            self._downstream_sockets.discard(connection)
            self._set_empty_if_idle()

    def _register_upstream(self, connection: http.client.HTTPConnection) -> bool:
        with self._active_lock:
            if self._stopping:
                return False
            self._upstream_connections[connection] = None
            self._active_empty.clear()
            return True

    def _track_upstream_socket(self, connection: http.client.HTTPConnection) -> bool:
        upstream_socket: socket.socket | None = None
        with self._active_lock:
            if connection not in self._upstream_connections:
                return False
            if not self._stopping:
                self._upstream_connections[connection] = connection.sock
                return True
            upstream_socket = connection.sock
            self._upstream_connections.pop(connection, None)
            self._set_empty_if_idle()
        if upstream_socket is not None:
            try:
                upstream_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            upstream_socket.close()
        return False

    def _unregister_upstream(self, connection: http.client.HTTPConnection) -> None:
        with self._active_lock:
            self._upstream_connections.pop(connection, None)
            self._set_empty_if_idle()

    def _set_empty_if_idle(self) -> None:
        if not self._downstream_sockets and not self._upstream_connections:
            self._active_empty.set()


class _ThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False


class _IPv6ThreadingHTTPServer(_ThreadingHTTPServer):
    address_family = socket.AF_INET6


class _BadRequest(Exception):
    pass


class _PayloadTooLarge(Exception):
    pass


class _ClientDisconnected(Exception):
    pass


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    proxy: MetricsProxy

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.proxy.DOWNSTREAM_IO_TIMEOUT_SECONDS)
        if not self.proxy._register_downstream(self.connection):
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()

    def handle(self) -> None:
        try:
            super().handle()
        except OSError:
            pass

    def finish(self) -> None:
        try:
            super().finish()
        except OSError:
            pass
        finally:
            self.proxy._unregister_downstream(self.connection)

    def do_GET(self) -> None:
        self._forward()

    def do_HEAD(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_PUT(self) -> None:
        self._forward()

    def do_PATCH(self) -> None:
        self._forward()

    def do_DELETE(self) -> None:
        self._forward()

    def do_OPTIONS(self) -> None:
        self._forward()

    def log_message(self, format_string: str, *args: object) -> None:
        pass

    def _forward(self) -> None:
        started_at = datetime.now(UTC)
        started = time.monotonic()
        ttft_ms: float | None = None
        status_code: int | None = None
        outcome = RequestOutcome.UPSTREAM_ERROR
        usage = (None, None, None, None)
        connection: http.client.HTTPConnection | None = None
        try:
            try:
                request_body = self._request_body()
            except _BadRequest:
                status_code = 400
                outcome = RequestOutcome.COMPLETED
                try:
                    self._send_local_error(400, "Bad Request")
                except OSError:
                    outcome = RequestOutcome.CLIENT_DISCONNECT
                return
            except _PayloadTooLarge:
                status_code = 413
                outcome = RequestOutcome.COMPLETED
                try:
                    self._send_local_error(413, "Payload Too Large")
                except OSError:
                    outcome = RequestOutcome.CLIENT_DISCONNECT
                return
            except (_ClientDisconnected, OSError):
                outcome = RequestOutcome.CLIENT_DISCONNECT
                return

            connection = self.proxy.HTTP_CONNECTION_CLASS(
                self.proxy.upstream_endpoint.host,
                self.proxy.upstream_endpoint.port,
                timeout=self.proxy.UPSTREAM_IO_TIMEOUT_SECONDS,
            )
            if not self.proxy._register_upstream(connection):
                outcome = RequestOutcome.CLIENT_DISCONNECT
                return
            try:
                connection.connect()
                if not self.proxy._track_upstream_socket(connection):
                    outcome = RequestOutcome.CLIENT_DISCONNECT
                    return
                connection.putrequest(
                    self.command, self.path, skip_host=True, skip_accept_encoding=True
                )
                request_headers = list(self.headers.items())
                for name, value in _end_to_end_headers(request_headers):
                    if name.lower() != "content-length":
                        connection.putheader(name, value)
                if not any(name.lower() == "host" for name, _ in request_headers):
                    connection.putheader(
                        "Host",
                        f"{self.proxy.upstream_endpoint.host}:{self.proxy.upstream_endpoint.port}",
                    )
                if (
                    request_body
                    or self.headers.get("Content-Length") is not None
                    or self.headers.get("Transfer-Encoding") is not None
                ):
                    connection.putheader("Content-Length", str(len(request_body)))
                connection.endheaders(request_body if request_body else None)
                response = connection.getresponse()
            except (OSError, http.client.HTTPException):
                status_code = 502
                outcome = RequestOutcome.UPSTREAM_ERROR
                try:
                    self._send_local_error(502, "Bad Gateway")
                except OSError:
                    outcome = RequestOutcome.CLIENT_DISCONNECT
                return

            status_code = response.status
            outcome = RequestOutcome.COMPLETED
            response_headers = response.getheaders()
            content_type = (
                response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            )
            has_body = self.command != "HEAD" and not (
                100 <= response.status < 200 or response.status in (204, 304)
            )
            content_length = _content_length(response)
            downstream_chunked = has_body and content_length is None
            try:
                self.send_response_only(response.status, response.reason)
                for name, value in _end_to_end_headers(response_headers):
                    if name.lower() != "content-length":
                        self.send_header(name, value)
                if content_length is not None:
                    self.send_header("Content-Length", str(content_length))
                elif downstream_chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
            except OSError:
                outcome = RequestOutcome.CLIENT_DISCONNECT
                return
            if not has_body:
                return

            is_sse = content_type == "text/event-stream"
            is_json = content_type == "application/json" or content_type.endswith(
                "+json"
            )
            parser = _SSEUsageParser() if is_sse else None
            capture = bytearray()
            capture_complete = True
            forwarded = 0
            while True:
                try:
                    chunk = response.read1(8192)
                except (
                    OSError,
                    AttributeError,
                    ValueError,
                    http.client.HTTPException,
                ):
                    outcome = RequestOutcome.UPSTREAM_ERROR
                    self._abort_downstream_response()
                    return
                if not chunk:
                    break
                forwarded += len(chunk)
                if ttft_ms is None:
                    ttft_ms = (time.monotonic() - started) * 1000
                if parser is not None:
                    parser.feed(chunk)
                elif is_json and capture_complete:
                    if len(capture) + len(chunk) <= _MAX_PARSE_BYTES:
                        capture.extend(chunk)
                    else:
                        capture.clear()
                        capture_complete = False
                try:
                    if downstream_chunked:
                        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                    self.wfile.write(chunk)
                    if downstream_chunked:
                        self.wfile.write(b"\r\n")
                    self.wfile.flush()
                except OSError:
                    outcome = RequestOutcome.CLIENT_DISCONNECT
                    return
            if content_length is not None and forwarded != content_length:
                outcome = RequestOutcome.UPSTREAM_ERROR
                self._abort_downstream_response()
                return
            if downstream_chunked:
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    outcome = RequestOutcome.CLIENT_DISCONNECT
                    return
            if parser is not None:
                usage = parser.usage
            elif is_json and capture_complete:
                usage = _json_usage(capture)
        finally:
            if connection is not None:
                connection.close()
                self.proxy._unregister_upstream(connection)
            if self.command == "POST":
                try:
                    self.proxy._engine.record(
                        RequestMetricEvent(
                            server_id=self.proxy._server_id,
                            model_alias=self.proxy._model_alias,
                            run_id=self.proxy._run_id,
                            started_at=started_at,
                            duration_ms=(time.monotonic() - started) * 1000,
                            ttft_ms=ttft_ms,
                            status_code=status_code,
                            outcome=outcome,
                            prompt_tokens=usage[0],
                            completion_tokens=usage[1],
                            total_tokens=usage[2],
                            cached_tokens=usage[3],
                        )
                    )
                except Exception:
                    _LOGGER.exception("failed to record request metrics")

    def _request_body(self) -> bytes | bytearray:
        lengths = self.headers.get_all("Content-Length", [])
        transfer_encodings = self.headers.get_all("Transfer-Encoding", [])
        if lengths and transfer_encodings:
            raise _BadRequest
        if len(lengths) > 1:
            raise _BadRequest
        if lengths:
            raw_length = lengths[0].strip()
            if not raw_length.isascii() or not raw_length.isdigit():
                raise _BadRequest
            length = int(raw_length)
            if length > self.proxy.MAX_REQUEST_BODY_BYTES:
                raise _PayloadTooLarge
            return self._read_exact(length)
        if not transfer_encodings:
            return b""
        encodings = [
            item.strip().lower()
            for value in transfer_encodings
            for item in value.split(",")
        ]
        if encodings != ["chunked"]:
            raise _BadRequest
        body = bytearray()
        while True:
            line = self.rfile.readline(8194)
            if not line:
                raise _ClientDisconnected
            if len(line) > 8192 or not line.endswith(b"\r\n"):
                raise _BadRequest
            raw_size = line[:-2].split(b";", 1)[0].strip()
            if not raw_size or any(
                byte not in b"0123456789abcdefABCDEF" for byte in raw_size
            ):
                raise _BadRequest
            size = int(raw_size, 16)
            if size == 0:
                self._consume_trailers()
                return body
            if len(body) + size > self.proxy.MAX_REQUEST_BODY_BYTES:
                raise _PayloadTooLarge
            body.extend(self._read_exact(size))
            if self._read_exact(2) != b"\r\n":
                raise _BadRequest

    def _read_exact(self, length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            chunk = self.rfile.read(length - len(chunks))
            if not chunk:
                raise _ClientDisconnected
            chunks.extend(chunk)
        return bytes(chunks)

    def _consume_trailers(self) -> None:
        total = 0
        while True:
            line = self.rfile.readline(8194)
            if not line:
                raise _ClientDisconnected
            total += len(line)
            if total > 8192 or not line.endswith(b"\r\n"):
                raise _BadRequest
            if line == b"\r\n":
                return
            if b":" not in line:
                raise _BadRequest

    def _send_local_error(self, status: int, reason: str) -> None:
        body = f"{reason}\n".encode("ascii")
        self.close_connection = True
        self.send_response_only(status, reason)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
            self.wfile.flush()

    def _abort_downstream_response(self) -> None:
        self.close_connection = True
        try:
            self.wfile.flush()
        except OSError:
            pass
        try:
            self.connection.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _end_to_end_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    headers = list(headers)
    connection_tokens = {
        token.strip().lower()
        for name, value in headers
        if name.lower() == "connection"
        for token in value.split(",")
    }
    stripped = _HOP_BY_HOP | connection_tokens
    return [(name, value) for name, value in headers if name.lower() not in stripped]


def _content_length(response: http.client.HTTPResponse) -> int | None:
    raw = response.getheader("Content-Length")
    if response.chunked or raw is None or not raw.strip().isdigit():
        return None
    return int(raw.strip())


def _json_usage(
    body: bytes | bytearray,
) -> tuple[int | None, int | None, int | None, int | None]:
    if len(body) > _MAX_PARSE_BYTES:
        return (None, None, None, None)
    try:
        document = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return (None, None, None, None)
    return _usage_from_document(document)


def _usage_from_document(
    document: object,
) -> tuple[int | None, int | None, int | None, int | None]:
    if not isinstance(document, dict) or not isinstance(document.get("usage"), dict):
        return (None, None, None, None)
    usage = document["usage"]
    details = usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    return (
        _token(usage.get("prompt_tokens")),
        _token(usage.get("completion_tokens")),
        _token(usage.get("total_tokens")),
        _token(cached),
    )


def _token(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


class _SSEUsageParser:
    def __init__(self) -> None:
        self._buffer = b""
        self.usage: tuple[int | None, int | None, int | None, int | None] = (
            None,
            None,
            None,
            None,
        )

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        if len(self._buffer) > _MAX_PARSE_BYTES:
            self._buffer = self._buffer[-_MAX_PARSE_BYTES:]
        normalized = self._buffer.replace(b"\r\n", b"\n")
        events = normalized.split(b"\n\n")
        self._buffer = events.pop()
        for event in events:
            data = b"\n".join(
                line[5:].lstrip()
                for line in event.splitlines()
                if line.startswith(b"data:")
            )
            if not data or data == b"[DONE]":
                continue
            parsed = _json_usage(data)
            if any(value is not None for value in parsed):
                self.usage = parsed
