import http.client
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mlxctl.adapters import Endpoint
from mlxctl.metrics import (
    MetricQuery,
    MetricsEngine,
    RequestMetricEvent,
    RequestOutcome,
)
from mlxctl.metrics_proxy import MetricsProxy


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    release_stream = threading.Event()
    release_ordinary = threading.Event()
    release_disconnect = threading.Event()
    request_body = b""

    def do_POST(self) -> None:
        type(self).request_body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path in ("/stream", "/stream-usage", "/hold-stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._chunk(b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n')
            if self.path in ("/stream", "/hold-stream"):
                type(self).release_stream.wait(2)
            else:
                self._chunk(
                    b'data: {"choices":[],"usage":{"prompt_tokens":11,'
                    b'"completion_tokens":5,"total_tokens":16}}\n\n'
                )
            self._chunk(b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        if self.path == "/slow":
            body = b"first-second"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"first-")
            self.wfile.flush()
            type(self).release_ordinary.wait(2)
            self.wfile.write(b"second")
            self.wfile.flush()
            return
        if self.path == "/disconnect":
            size = 4 * 1024 * 1024
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            self.wfile.write(b"first")
            self.wfile.flush()
            type(self).release_disconnect.wait(2)
            block = b"x" * 65536
            try:
                for _ in range(size // len(block)):
                    self.wfile.write(block)
                    self.wfile.flush()
            except OSError:
                pass
            return
        if self.path == "/truncate-fixed":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", "20")
            self.end_headers()
            self.wfile.write(b"short")
            self.wfile.flush()
            self.close_connection = True
            return
        if self.path == "/truncate-chunked":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._chunk(b"first")
            self.wfile.write(b"A\r\nshort")
            self.wfile.flush()
            self.close_connection = True
            return
        if self.path == "/failure":
            body = b'{"error":"capacity"}'
            self.send_response(503, "At Capacity")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = (
            b'{"id":"answer","usage":{"prompt_tokens":7,'
            b'"completion_tokens":2,"total_tokens":9,'
            b'"prompt_tokens_details":{"cached_tokens":3}}}'
        )
        self.send_response(201, "Generated")
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Upstream", "preserved")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "https://client.example")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format_string: str, *args: object) -> None:
        pass

    def _chunk(self, body: bytes) -> None:
        self.wfile.write(f"{len(body):X}\r\n".encode() + body + b"\r\n")
        self.wfile.flush()


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request: object, client_address: object) -> None:
        pass


class MetricsProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        _UpstreamHandler.release_stream.clear()
        _UpstreamHandler.release_ordinary.clear()
        _UpstreamHandler.release_disconnect.clear()
        _UpstreamHandler.request_body = b""
        _GatedHTTPConnection.reset()
        _ThreadOwnedBlockingHTTPConnection.reset()
        self.directory = tempfile.TemporaryDirectory()
        self.engine = MetricsEngine(Path(self.directory.name) / "metrics.sqlite3")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_forwards_nonstream_response_and_records_openai_usage(self) -> None:
        request_body = b'{"messages":[{"role":"user","content":"hello"}]}'
        with self._upstream() as upstream, self._proxy(upstream) as proxy:
            client = proxy.client_endpoint
            connection = http.client.HTTPConnection(client.host, client.port, timeout=2)
            connection.request(
                "POST",
                "/v1/chat/completions?trace=yes",
                body=request_body,
                headers={"Content-Type": "application/json", "X-Client": "kept"},
            )
            response = connection.getresponse()
            body = response.read()
            connection.close()

        self.assertEqual(response.status, 201)
        self.assertEqual(response.reason, "Generated")
        self.assertEqual(response.getheader("X-Upstream"), "preserved")
        self.assertEqual(response.getheader("Content-Length"), str(len(body)))
        self.assertEqual(
            body,
            b'{"id":"answer","usage":{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9,"prompt_tokens_details":{"cached_tokens":3}}}',
        )
        self.assertEqual(_UpstreamHandler.request_body, request_body)
        summary = self.engine.query(MetricQuery())[0]
        self.assertEqual(summary.request_count, 1)
        self.assertEqual(summary.success_count, 1)
        self.assertEqual(summary.prompt_tokens, 7)
        self.assertEqual(summary.completion_tokens, 2)
        self.assertEqual(summary.total_tokens, 9)
        self.assertEqual(summary.cached_tokens, 3)

    def test_streams_before_upstream_completion_and_records_ttft_without_usage(
        self,
    ) -> None:
        with self._upstream() as upstream, self._proxy(upstream) as proxy:
            client = proxy.client_endpoint
            connection = http.client.HTTPConnection(client.host, client.port, timeout=2)
            connection.request("POST", "/stream", body=b"{}")
            response = connection.getresponse()
            first = response.read1(1024)
            _UpstreamHandler.release_stream.set()
            rest = response.read()
            connection.close()

        self.assertIn(b'"content":"one"', first)
        self.assertEqual(
            first + rest,
            b'data: {"choices":[{"delta":{"content":"one"}}]}\n\ndata: [DONE]\n\n',
        )
        summary = self.engine.query(MetricQuery())[0]
        self.assertIsNotNone(summary.average_ttft_ms)
        self.assertIsNone(summary.total_tokens)

    def test_streams_ordinary_bytes_and_measures_ttft_at_first_body_bytes(self) -> None:
        with self._upstream() as upstream, self._proxy(upstream) as proxy:
            client = proxy.client_endpoint
            connection = http.client.HTTPConnection(client.host, client.port, timeout=2)
            connection.request("POST", "/slow", body=b"{}")
            response = connection.getresponse()
            first = response.read1(1024)
            time.sleep(0.25)
            _UpstreamHandler.release_ordinary.set()
            rest = response.read()
            connection.close()

        self.assertEqual(response.getheader("Transfer-Encoding"), "chunked")
        self.assertEqual(first, b"first-")
        self.assertEqual(rest, b"second")
        summary = self.engine.query(MetricQuery())[0]
        self.assertGreater(
            summary.average_duration_ms - summary.average_ttft_ms,
            200,
        )

    def test_returns_502_and_records_upstream_error_on_connection_failure(self) -> None:
        unavailable = Endpoint("127.0.0.1", self._free_port())
        with self._proxy(unavailable) as proxy:
            client = proxy.client_endpoint
            connection = http.client.HTTPConnection(client.host, client.port, timeout=2)
            connection.request("POST", "/v1/chat/completions", body=b"{}")
            response = connection.getresponse()
            response.read()
            connection.close()

        self.assertEqual(response.status, 502)
        summary = self.engine.query(MetricQuery())[0]
        self.assertEqual(summary.failure_count, 1)
        self.assertIsNone(summary.total_tokens)

    def test_records_final_sse_usage_and_completed_upstream_failure(self) -> None:
        with self._upstream() as upstream, self._proxy(upstream) as proxy:
            client = proxy.client_endpoint
            connection = http.client.HTTPConnection(client.host, client.port, timeout=2)
            connection.request("POST", "/stream-usage", body=b"{}")
            streamed = connection.getresponse()
            streamed.read()
            connection.request("POST", "/failure", body=b"{}")
            failed = connection.getresponse()
            failed_body = failed.read()
            connection.close()

        self.assertEqual(failed.status, 503)
        self.assertEqual(failed.reason, "At Capacity")
        self.assertEqual(failed_body, b'{"error":"capacity"}')
        summary = self.engine.query(MetricQuery())[0]
        self.assertEqual(summary.request_count, 2)
        self.assertEqual(summary.success_count, 1)
        self.assertEqual(summary.failure_count, 1)
        self.assertEqual(summary.prompt_tokens, 11)
        self.assertEqual(summary.completion_tokens, 5)
        self.assertEqual(summary.total_tokens, 16)

    def test_forwards_options_cors_and_closes_the_listener(self) -> None:
        with self._upstream() as upstream:
            client = Endpoint("127.0.0.1", self._free_port())
            with self._proxy(upstream, client):
                connection = http.client.HTTPConnection(
                    client.host, client.port, timeout=2
                )
                connection.request("OPTIONS", "/v1/chat/completions")
                response = connection.getresponse()
                response.read()
                connection.close()
                self.assertEqual(response.status, 204)
                self.assertEqual(
                    response.getheader("Access-Control-Allow-Origin"),
                    "https://client.example",
                )
                self.assertEqual(self.engine.query(MetricQuery()), ())

            with self.assertRaises(OSError):
                socket.create_connection((client.host, client.port), timeout=0.2)

    def test_metric_persistence_failure_preserves_keep_alive_responses(self) -> None:
        with (
            self._upstream() as upstream,
            self._proxy(upstream, engine=_FailingEngine()) as proxy,
            self.assertLogs("mlxctl.metrics_proxy", level="ERROR") as logs,
        ):
            response = self._raw_response(
                proxy.client_endpoint,
                b"POST /v1/chat/completions HTTP/1.1\r\n"
                b"Host: localhost\r\nContent-Length: 2\r\n\r\n{}"
                b"POST /v1/chat/completions HTTP/1.1\r\n"
                b"Host: localhost\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}",
            )

        self.assertEqual(response.count(b"HTTP/1.1 201 Generated\r\n"), 2)
        self.assertEqual(len(logs.records), 2)

    def test_incomplete_request_is_closed_after_downstream_timeout(self) -> None:
        proxy = self._proxy(Endpoint("127.0.0.1", self._free_port()))
        proxy.DOWNSTREAM_IO_TIMEOUT_SECONDS = 0.1
        with proxy:
            client = socket.create_connection(
                (proxy.client_endpoint.host, proxy.client_endpoint.port), timeout=1
            )
            try:
                client.sendall(
                    b"POST / HTTP/1.1\r\nHost: localhost\r\n"
                    b"Content-Length: 100\r\n\r\npartial"
                )
                client.settimeout(0.5)
                self.assertEqual(client.recv(1), b"")
            finally:
                client.close()

    def test_close_is_bounded_with_an_active_sse(self) -> None:
        with self._upstream() as upstream:
            proxy = self._proxy(upstream)
            proxy.__enter__()
            client = proxy.client_endpoint
            connection = http.client.HTTPConnection(client.host, client.port, timeout=2)
            connection.request("POST", "/hold-stream", body=b"{}")
            response = connection.getresponse()
            self.assertIn(b'"content":"one"', response.read1(1024))

            started = time.monotonic()
            proxy.__exit__(None, None, None)
            elapsed = time.monotonic() - started
            _UpstreamHandler.release_stream.set()
            connection.close()

            self.assertLess(elapsed, 1)
            with self.assertRaises(OSError):
                socket.create_connection((client.host, client.port), timeout=0.2)

    def test_close_is_bounded_with_an_incomplete_client_body(self) -> None:
        proxy = self._proxy(Endpoint("127.0.0.1", self._free_port()))
        proxy.__enter__()
        client = proxy.client_endpoint
        downstream = socket.create_connection((client.host, client.port), timeout=2)
        downstream.sendall(
            b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\npartial"
        )
        time.sleep(0.05)

        started = time.monotonic()
        proxy.__exit__(None, None, None)
        elapsed = time.monotonic() - started
        downstream.close()

        self.assertLess(elapsed, 1)
        with self.assertRaises(OSError):
            socket.create_connection((client.host, client.port), timeout=0.2)

    def test_rejects_ambiguous_and_malformed_request_framing(self) -> None:
        cases = (
            b"Content-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
            b"Transfer-Encoding: gzip\r\n\r\n",
            b"Transfer-Encoding: chunked\r\n\r\nZ\r\ninvalid\r\n",
            b"Transfer-Encoding: chunked\r\n\r\n1\r\naXX",
        )
        unavailable = Endpoint("127.0.0.1", self._free_port())
        for framing in cases:
            with self.subTest(framing=framing), self._proxy(unavailable) as proxy:
                response = self._raw_response(
                    proxy.client_endpoint,
                    b"POST / HTTP/1.1\r\nHost: localhost\r\n" + framing,
                )
                self.assertIn(b"HTTP/1.1 400 Bad Request\r\n", response)
                self.assertIn(b"Connection: close\r\n", response)

    def test_rejects_fixed_and_chunked_bodies_over_the_v1_limit(self) -> None:
        unavailable = Endpoint("127.0.0.1", self._free_port())
        requests = (
            b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 6\r\n\r\n",
            b"POST / HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n6\r\nabcdef\r\n0\r\n\r\n",
        )
        for request in requests:
            with self.subTest(request=request), self._proxy(unavailable) as proxy:
                proxy.MAX_REQUEST_BODY_BYTES = 5
                response = self._raw_response(proxy.client_endpoint, request)
                self.assertIn(b"HTTP/1.1 413 Payload Too Large\r\n", response)
                self.assertIn(b"Connection: close\r\n", response)

    def test_records_client_disconnect_without_reclassifying_upstream(self) -> None:
        recorder = _RecordingEngine()
        with (
            self._upstream() as upstream,
            self._proxy(upstream, engine=recorder) as proxy,
        ):
            client = proxy.client_endpoint
            connection = http.client.HTTPConnection(client.host, client.port, timeout=2)
            connection.request("POST", "/disconnect", body=b"{}")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read1(5)
            connection.close()
            _UpstreamHandler.release_disconnect.set()
            self.assertTrue(recorder.recorded.wait(2))

        self.assertEqual(recorder.events[-1].status_code, 200)
        self.assertIs(recorder.events[-1].outcome, RequestOutcome.CLIENT_DISCONNECT)

    def test_truncated_upstream_responses_close_the_downstream_connection(self) -> None:
        cases = {
            "/truncate-fixed": b"short",
            "/truncate-chunked": b"firstshort",
        }
        for path, expected_partial in cases.items():
            with (
                self.subTest(path=path),
                self._upstream() as upstream,
                self._proxy(upstream) as proxy,
            ):
                client = proxy.client_endpoint
                connection = http.client.HTTPConnection(
                    client.host, client.port, timeout=0.5
                )
                try:
                    connection.request("POST", path, body=b"{}")
                    response = connection.getresponse()

                    started = time.monotonic()
                    with self.assertRaises(http.client.IncompleteRead) as raised:
                        response.read()
                    self.assertLess(time.monotonic() - started, 0.5)
                    self.assertEqual(raised.exception.partial, expected_partial)
                    with self.assertRaises((OSError, http.client.HTTPException)):
                        connection.request("OPTIONS", "/")
                        connection.getresponse()
                finally:
                    connection.close()

    def test_closes_an_upstream_socket_published_after_shutdown_started(self) -> None:
        recorder = _RecordingEngine()
        with self._upstream() as upstream:
            proxy = self._proxy(upstream, engine=recorder)
            proxy.HTTP_CONNECTION_CLASS = _GatedHTTPConnection
            proxy.__enter__()
            client = proxy.client_endpoint
            downstream = socket.create_connection((client.host, client.port), timeout=2)
            proxy_closed = False
            try:
                downstream.sendall(
                    b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}"
                )
                self.assertTrue(_GatedHTTPConnection.before_connect.wait(1))

                proxy.__exit__(None, None, None)
                proxy_closed = True
                _GatedHTTPConnection.release_connect.set()

                self.assertTrue(_GatedHTTPConnection.closed.wait(2))
                self.assertFalse(_GatedHTTPConnection.getresponse_called.is_set())
            finally:
                _GatedHTTPConnection.release_connect.set()
                if not proxy_closed:
                    proxy.__exit__(None, None, None)
                downstream.close()

    def test_close_interrupts_a_body_send_after_connect(self) -> None:
        recorder = _RecordingEngine()
        proxy = self._proxy(Endpoint("127.0.0.1", self._free_port()), engine=recorder)
        proxy.HTTP_CONNECTION_CLASS = _ThreadOwnedBlockingHTTPConnection
        proxy.__enter__()
        client = proxy.client_endpoint
        downstream = socket.create_connection((client.host, client.port), timeout=2)
        proxy_closed = False
        try:
            body = b"x" * 65536
            downstream.sendall(
                b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 65536\r\n\r\n"
                + body
            )
            self.assertTrue(
                _ThreadOwnedBlockingHTTPConnection.body_send_started.wait(1)
            )

            started = time.monotonic()
            proxy.__exit__(None, None, None)
            proxy_closed = True

            self.assertLess(time.monotonic() - started, 1)
            self.assertTrue(_ThreadOwnedBlockingHTTPConnection.interrupted.wait(0.2))
        finally:
            blocking_socket = _ThreadOwnedBlockingHTTPConnection.blocking_socket
            if blocking_socket is not None:
                blocking_socket.shutdown(socket.SHUT_RDWR)
                blocking_socket.close()
            if not proxy_closed:
                proxy.__exit__(None, None, None)
            downstream.close()
            _ThreadOwnedBlockingHTTPConnection.closed.wait(2)

    def _proxy(
        self,
        upstream: Endpoint,
        client: Endpoint | None = None,
        engine: object | None = None,
    ) -> MetricsProxy:
        return MetricsProxy(
            client_endpoint=client or Endpoint("127.0.0.1", self._free_port()),
            upstream_endpoint=upstream,
            engine=engine or self.engine,
            server_id="chat",
            model_alias="tiny",
            run_id="run-1",
        )

    def _upstream(self):
        test = self

        class Upstream:
            def __enter__(self) -> Endpoint:
                self.server = _QuietThreadingHTTPServer(
                    ("127.0.0.1", test._free_port()), _UpstreamHandler
                )
                self.thread = threading.Thread(
                    target=self.server.serve_forever, daemon=True
                )
                self.thread.start()
                host, port = self.server.server_address
                return Endpoint(host, port)

            def __exit__(self, *args: object) -> None:
                self.server.shutdown()
                self.server.server_close()
                self.thread.join(2)

        return Upstream()

    @staticmethod
    def _raw_response(endpoint: Endpoint, request: bytes) -> bytes:
        with socket.create_connection(
            (endpoint.host, endpoint.port), timeout=2
        ) as client:
            client.settimeout(2)
            client.sendall(request)
            client.shutdown(socket.SHUT_WR)
            chunks = []
            while chunk := client.recv(4096):
                chunks.append(chunk)
            return b"".join(chunks)

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]


class _RecordingEngine:
    def __init__(self) -> None:
        self.events: list[RequestMetricEvent] = []
        self.recorded = threading.Event()

    def record(self, event: RequestMetricEvent) -> None:
        self.events.append(event)
        self.recorded.set()


class _FailingEngine:
    def record(self, event: RequestMetricEvent) -> None:
        raise RuntimeError("metrics unavailable")


class _GatedHTTPConnection(http.client.HTTPConnection):
    before_connect = threading.Event()
    release_connect = threading.Event()
    getresponse_called = threading.Event()
    closed = threading.Event()

    @classmethod
    def reset(cls) -> None:
        cls.before_connect.clear()
        cls.release_connect.clear()
        cls.getresponse_called.clear()
        cls.closed.clear()

    def connect(self) -> None:
        type(self).before_connect.set()
        type(self).release_connect.wait(2)
        super().connect()

    def getresponse(self) -> http.client.HTTPResponse:
        type(self).getresponse_called.set()
        return super().getresponse()

    def close(self) -> None:
        try:
            super().close()
        finally:
            type(self).closed.set()


class _BlockingSocket:
    def __init__(self) -> None:
        self._send_count = 0

    def sendall(self, data: bytes | bytearray) -> None:
        self._send_count += 1
        if self._send_count == 1:
            return
        _ThreadOwnedBlockingHTTPConnection.body_send_started.set()
        _ThreadOwnedBlockingHTTPConnection.interrupted.wait(3)
        raise OSError("send interrupted")

    def shutdown(self, how: int) -> None:
        _ThreadOwnedBlockingHTTPConnection.interrupted.set()

    def close(self) -> None:
        _ThreadOwnedBlockingHTTPConnection.interrupted.set()


class _ThreadOwnedBlockingHTTPConnection(http.client.HTTPConnection):
    body_send_started = threading.Event()
    interrupted = threading.Event()
    closed = threading.Event()
    blocking_socket: _BlockingSocket | None = None

    def __init__(self, *args: object, **kwargs: object) -> None:
        self._owner_thread: int | None = None
        self._owned_socket: _BlockingSocket | None = None
        super().__init__(*args, **kwargs)

    @property
    def sock(self) -> _BlockingSocket | None:
        if self._owner_thread == threading.get_ident():
            return self._owned_socket
        return None

    @sock.setter
    def sock(self, value: _BlockingSocket | None) -> None:
        self._owned_socket = value

    @classmethod
    def reset(cls) -> None:
        cls.body_send_started.clear()
        cls.interrupted.clear()
        cls.closed.clear()
        cls.blocking_socket = None

    def connect(self) -> None:
        self._owner_thread = threading.get_ident()
        blocking_socket = _BlockingSocket()
        type(self).blocking_socket = blocking_socket
        self.sock = blocking_socket

    def close(self) -> None:
        try:
            super().close()
        finally:
            type(self).closed.set()


if __name__ == "__main__":
    unittest.main()
