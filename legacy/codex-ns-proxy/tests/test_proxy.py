from __future__ import annotations

import contextlib
import copy
import dataclasses
import http.client
import http.server
import importlib.util
import io
import json
import pathlib
import socket
import socketserver
import struct
import sys
import threading
import time
import unittest


MODULE_PATH = pathlib.Path(__file__).parents[1] / "codex-ns-proxy.py"
SPEC = importlib.util.spec_from_file_location("codex_ns_proxy", MODULE_PATH)
proxy = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = proxy
SPEC.loader.exec_module(proxy)

INBOUND_TOKEN = "inbound-secret-token-0000000000000001"
UPSTREAM_TOKEN = "upstream-secret-token-00000000000001"


class FakeUpstreamHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests = []
    response_status = 200
    response_headers = {"Content-Type": "application/json"}
    response_body = b'{"object":"ok"}'
    read_started = threading.Event()
    response_delay = 0
    hold_open = False
    release_response = threading.Event()
    stream_chunks = None
    observe_disconnect = False
    upstream_disconnect_observed = threading.Event()

    def do_GET(self):
        self._record_and_respond()

    def do_POST(self):
        self._record_and_respond()

    def do_PUT(self):
        self._record_and_respond()

    def _record_and_respond(self):
        self.__class__.read_started.set()
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.__class__.requests.append(
            {"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body}
        )
        if self.__class__.response_delay:
            time.sleep(self.__class__.response_delay)
        if self.__class__.hold_open:
            self.send_response(self.__class__.response_status)
            for key, value in self.__class__.response_headers.items():
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(self.__class__.response_body)
            self.wfile.flush()
            if self.__class__.observe_disconnect:
                self.connection.settimeout(0.05)
                while not self.__class__.release_response.is_set():
                    try:
                        if not self.connection.recv(1):
                            self.__class__.upstream_disconnect_observed.set()
                            break
                    except socket.timeout:
                        pass
            self.__class__.release_response.wait(5)
            return
        if self.__class__.stream_chunks is not None:
            self.send_response(self.__class__.response_status)
            for key, value in self.__class__.response_headers.items():
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            for delay, chunk in self.__class__.stream_chunks:
                if delay:
                    time.sleep(delay)
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
            return
        self.send_response(self.__class__.response_status)
        for key, value in self.__class__.response_headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(self.__class__.response_body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(self.__class__.response_body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, _format, *args):
        return


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = True


class ProxyIntegrationTest(unittest.TestCase):
    def setUp(self):
        FakeUpstreamHandler.requests = []
        FakeUpstreamHandler.response_status = 200
        FakeUpstreamHandler.response_headers = {"Content-Type": "application/json"}
        FakeUpstreamHandler.response_body = b'{"object":"ok"}'
        FakeUpstreamHandler.read_started = threading.Event()
        FakeUpstreamHandler.response_delay = 0
        FakeUpstreamHandler.hold_open = False
        FakeUpstreamHandler.release_response = threading.Event()
        FakeUpstreamHandler.stream_chunks = None
        FakeUpstreamHandler.observe_disconnect = False
        FakeUpstreamHandler.upstream_disconnect_observed = threading.Event()
        self.upstream = ThreadingServer(("127.0.0.1", 0), FakeUpstreamHandler)
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever)
        self.upstream_thread.start()
        upstream_port = self.upstream.server_address[1]
        self.config = proxy.ProxyConfig(
            upstream_url=f"http://127.0.0.1:{upstream_port}/provider/v1",
            inbound_token=INBOUND_TOKEN,
            listen_port=0,
            upstream_timeout_seconds=1,
            inbound_timeout_seconds=1,
            adapter="codex-namespace",
        )
        self.gateway = proxy.create_server(self.config)
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever)
        self.gateway_thread.start()

    def tearDown(self):
        self.gateway.shutdown()
        self.gateway.server_close()
        self.gateway_thread.join(2)
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join(2)
        self.assertFalse(self.gateway_thread.is_alive())
        self.assertFalse(self.upstream_thread.is_alive())

    def request(self, method, path, body=None, token=INBOUND_TOKEN, headers=None):
        request_headers = dict(headers or {})
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"
        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode()
            request_headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.gateway.server_address[1], timeout=2
        )
        connection.request(method, path, body=encoded, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        result = response.status, dict(response.getheaders()), raw
        connection.close()
        return result

    def restart_gateway(self, **config_overrides):
        self.gateway.shutdown()
        self.gateway.server_close()
        self.gateway_thread.join(2)
        config = dataclasses.replace(
            self.config,
            listen_port=0,
            **config_overrides,
        )
        self.gateway = proxy.create_server(config)
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever)
        self.gateway_thread.start()

    def raw_request(self, request, *, shutdown_write=False, timeout=2):
        connection = socket.create_connection(
            ("127.0.0.1", self.gateway.server_address[1]), timeout=timeout
        )
        connection.settimeout(timeout)
        connection.sendall(request)
        if shutdown_write:
            connection.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            try:
                chunk = connection.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
        connection.close()
        return b"".join(chunks)

    def test_auth_rejection_happens_without_upstream_contact(self):
        status, _, _ = self.request("POST", "/v1/responses", {"input": []}, token=None)
        self.assertEqual(401, status)
        status, _, _ = self.request("POST", "/v1/responses", {"input": []}, token="wrong")
        self.assertEqual(401, status)
        self.assertEqual([], FakeUpstreamHandler.requests)
        self.assertFalse(FakeUpstreamHandler.read_started.is_set())

    def test_unauthorized_expect_request_gets_no_continue_and_connection_closes(self):
        raw = self.raw_request(
            b"POST /v1/responses HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer wrong\r\n"
            b"Expect: 100-continue\r\n"
            b"Content-Length: 100\r\n\r\n"
        )
        self.assertTrue(raw.startswith(b"HTTP/1.1 401"), raw)
        self.assertNotIn(b"100 Continue", raw)
        self.assertIn(b"Connection: close", raw)
        self.assertEqual([], FakeUpstreamHandler.requests)

    def test_framing_rejections_close_without_upstream_contact(self):
        auth = f"Authorization: Bearer {INBOUND_TOKEN}\r\n".encode()
        cases = {
            "missing": b"",
            "duplicate": b"Content-Length: 2\r\nContent-Length: 2\r\n",
            "comma": b"Content-Length: 2, 2\r\n",
            "plus": b"Content-Length: +2\r\n",
            "negative": b"Content-Length: -1\r\n",
            "malformed": b"Content-Length: no\r\n",
            "trailing-space": b"Content-Length: 2 \r\n",
            "non-ascii-digit": "Content-Length: ٢\r\n".encode("utf-8"),
            "transfer": b"Transfer-Encoding: chunked\r\nContent-Length: 2\r\n",
        }
        for name, framing in cases.items():
            with self.subTest(name=name):
                raw = self.raw_request(
                    b"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    + auth
                    + framing
                    + b"\r\n"
                )
                self.assertRegex(raw, rb"^HTTP/1\.1 (400|411)")
                self.assertIn(b"Connection: close", raw)
        self.assertEqual([], FakeUpstreamHandler.requests)

    def test_short_body_is_rejected(self):
        auth = f"Authorization: Bearer {INBOUND_TOKEN}\r\n".encode()
        raw = self.raw_request(
            b"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            + auth
            + b"Content-Length: 10\r\n\r\n{}",
            shutdown_write=True,
        )
        self.assertTrue(raw.startswith(b"HTTP/1.1 400"), raw)
        self.assertEqual([], FakeUpstreamHandler.requests)

    def test_very_long_digit_content_length_is_rejected_without_integer_conversion(self):
        auth = f"Authorization: Bearer {INBOUND_TOKEN}\r\n".encode()
        raw = self.raw_request(
            b"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            + auth
            + b"Content-Length: "
            + (b"9" * 5000)
            + b"\r\n\r\n"
        )
        self.assertTrue(raw.startswith(b"HTTP/1.1 413"), raw[:100])
        self.assertIn(b"Connection: close", raw)
        self.assertEqual([], FakeUpstreamHandler.requests)

    def test_inbound_body_timeout_is_fixed_and_closes(self):
        self.gateway.shutdown()
        self.gateway.server_close()
        self.gateway_thread.join(2)
        self.gateway = proxy.create_server(
            proxy.ProxyConfig(
                upstream_url=self.config.upstream_url,
                inbound_token=INBOUND_TOKEN,
                listen_port=0,
                inbound_timeout_seconds=0.05,
                adapter="codex-namespace",
            )
        )
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever)
        self.gateway_thread.start()
        auth = f"Authorization: Bearer {INBOUND_TOKEN}\r\n".encode()
        raw = self.raw_request(
            b"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            + auth
            + b"Content-Length: 10\r\n\r\n",
        )
        self.assertTrue(raw.startswith(b"HTTP/1.1 408"), raw)
        self.assertIn(b"Connection: close", raw)
        self.assertEqual([], FakeUpstreamHandler.requests)

    def test_inbound_credential_is_not_forwarded(self):
        status, _, _ = self.request(
            "GET", "/v1/models", headers={"Proxy-Connection": "keep-alive"}
        )
        self.assertEqual(200, status)
        forwarded = FakeUpstreamHandler.requests[0]
        self.assertEqual("/provider/v1/models", forwarded["path"])
        self.assertNotIn("Authorization", forwarded["headers"])
        self.assertNotIn("Proxy-Connection", forwarded["headers"])

    def test_optional_upstream_bearer_is_injected_separately(self):
        self.gateway.shutdown()
        self.gateway.server_close()
        self.gateway_thread.join(2)
        self.gateway = proxy.create_server(
            proxy.ProxyConfig(
                upstream_url=self.config.upstream_url,
                inbound_token=INBOUND_TOKEN,
                upstream_token=UPSTREAM_TOKEN,
                listen_port=0,
                adapter="codex-namespace",
            )
        )
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever)
        self.gateway_thread.start()
        self.request("GET", "/v1/models")
        self.assertEqual(
            f"Bearer {UPSTREAM_TOKEN}", FakeUpstreamHandler.requests[0]["headers"]["Authorization"]
        )

    def test_path_and_method_allowlist(self):
        for method, path, expected in [
            ("POST", "/v1/models", 405),
            ("GET", "/v1/responses", 405),
            ("PUT", "/v1/responses", 405),
            ("HEAD", "/v1/models", 405),
            ("TRACE", "/v1/responses", 405),
            ("GET", "/other", 404),
            ("GET", "/v1/models?x=1", 404),
            ("GET", "/v1/%6dodels", 404),
            ("GET", "/v1/./models", 404),
            ("GET", "http://127.0.0.1/v1/models", 404),
        ]:
            with self.subTest(method=method, path=path):
                status, _, _ = self.request(method, path)
                self.assertEqual(expected, status)
        self.assertEqual([], FakeUpstreamHandler.requests)

    def test_request_transforms_tools_history_and_function_output(self):
        request = {
            "model": "local",
            "tools": [
                {
                    "type": "namespace",
                    "name": "math",
                    "description": "Math tools",
                    "tools": [
                        {"type": "function", "name": "add", "description": "Add", "parameters": {}}
                    ],
                },
                {"type": "function", "name": "plain", "parameters": {}},
            ],
            "input": [
                {"type": "function_call", "namespace": "math", "name": "add", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "one", "output": "5"},
                {"type": "agent_message", "author": "a", "recipient": "b", "content": []},
                {"type": "encrypted_content", "encrypted_content": "opaque"},
            ],
        }
        original = copy.deepcopy(request)
        status, _, _ = self.request("POST", "/v1/responses", request)
        self.assertEqual(200, status)
        forwarded = json.loads(FakeUpstreamHandler.requests[0]["body"])
        self.assertEqual("math__add", forwarded["tools"][0]["name"])
        self.assertEqual("[math] Math tools\n\nAdd", forwarded["tools"][0]["description"])
        self.assertEqual("plain", forwarded["tools"][1]["name"])
        self.assertEqual("math__add", forwarded["input"][0]["name"])
        self.assertNotIn("namespace", forwarded["input"][0])
        self.assertEqual(request["input"][1], forwarded["input"][1])
        self.assertEqual(request["input"][2], forwarded["input"][2])
        self.assertEqual(request["input"][3], forwarded["input"][3])
        self.assertEqual(original, request)

    def test_plain_json_response_reconstructs_namespace_without_metadata(self):
        FakeUpstreamHandler.response_body = json.dumps(
            {
                "id": "response-one",
                "output": [
                    {"type": "function_call", "name": "math__add", "arguments": "{}"},
                    {"type": "message", "content": [{"type": "output_text", "text": "hello"}]},
                ],
            }
        ).encode()
        status, _, raw = self.request(
            "POST",
            "/v1/responses",
            {
                "input": [],
                "tools": [
                    {
                        "type": "namespace",
                        "name": "math",
                        "tools": [{"type": "function", "name": "add", "parameters": {}}],
                    }
                ],
            },
        )
        self.assertEqual(200, status)
        result = json.loads(raw)
        self.assertEqual("add", result["output"][0]["name"])
        self.assertEqual("math", result["output"][0]["namespace"])
        self.assertNotIn("usage", result)
        self.assertNotIn("model", result)

    def test_sse_preserves_order_and_unchanged_bytes_and_observes_terminal(self):
        sse = (
            b"event: response.output_item.added\n"
            b'data: {"type":"response.output_item.added","item":{"type":"function_call","name":"math__add","arguments":"{}"}}\n\n'
            b": keep-alive\n\n"
            b"event: response.output_text.delta\n"
            b'data:  {"type":"response.output_text.delta","delta":"hello"}\n\n'
            b"event: response.completed\n"
            b'data: {"type":"response.completed","response":{"id":"one","output":[]}}\n\n'
        )
        FakeUpstreamHandler.response_headers = {"Content-Type": "text/event-stream"}
        FakeUpstreamHandler.response_body = sse
        stderr = io.StringIO()
        self.gateway.shutdown()
        self.gateway.server_close()
        self.gateway_thread.join(2)
        debug_config = proxy.ProxyConfig(
            upstream_url=self.config.upstream_url,
            inbound_token=INBOUND_TOKEN,
            listen_port=0,
            debug=True,
            adapter="codex-namespace",
        )
        self.gateway = proxy.create_server(debug_config)
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever)
        self.gateway_thread.start()
        with contextlib.redirect_stderr(stderr):
            status, _, raw = self.request(
                "POST",
                "/v1/responses",
                {
                    "input": [],
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "math",
                            "tools": [
                                {"type": "function", "name": "add", "parameters": {}}
                            ],
                        }
                    ],
                },
            )
        self.assertEqual(200, status)
        lines = raw.splitlines(keepends=True)
        self.assertEqual(b"event: response.output_item.added\n", lines[0])
        reconstructed = json.loads(lines[1][6:])
        self.assertEqual("add", reconstructed["item"]["name"])
        self.assertEqual("math", reconstructed["item"]["namespace"])
        unchanged = b'data:  {"type":"response.output_text.delta","delta":"hello"}\n'
        self.assertIn(unchanged, lines)
        self.assertLess(raw.index(b"response.output_item.added"), raw.index(b"response.completed"))
        self.assertIn("SSE terminal_completed=true", stderr.getvalue())

    def test_sse_heartbeat_precedes_delayed_first_upstream_frame(self):
        upstream_frame = (
            b"event: response.completed\n"
            b'data: {"type":"response.completed","response":{"id":"one","output":[]}}\n\n'
        )
        FakeUpstreamHandler.response_headers = {"Content-Type": "text/event-stream"}
        FakeUpstreamHandler.stream_chunks = [(0.12, upstream_frame)]
        self.restart_gateway(
            upstream_timeout_seconds=1,
            sse_heartbeat_seconds=0.03,
            adapter="codex-namespace",
        )

        status, _, raw = self.request("POST", "/v1/responses", {"input": []})

        self.assertEqual(200, status)
        heartbeat = b": codex-ns-proxy keep-alive\n\n"
        self.assertIn(heartbeat, raw)
        self.assertLess(raw.index(heartbeat), raw.index(upstream_frame))

    def test_sse_heartbeats_preserve_exact_upstream_frames_and_order(self):
        first = b"event: response.output_text.delta\ndata: {\"delta\":\"a\"}\n\n"
        terminal = (
            b"event: response.completed\n"
            b'data: {"type":"response.completed","response":{"id":"two","output":[]}}\n\n'
        )
        FakeUpstreamHandler.response_headers = {"Content-Type": "text/event-stream"}
        FakeUpstreamHandler.stream_chunks = [(0, first), (0.08, terminal)]
        self.restart_gateway(
            upstream_timeout_seconds=1,
            sse_heartbeat_seconds=0.02,
            adapter="identity",
        )

        status, _, raw = self.request("POST", "/v1/responses", {"input": []})

        heartbeat = b": codex-ns-proxy keep-alive\n\n"
        self.assertEqual(200, status)
        self.assertGreaterEqual(raw.count(heartbeat), 1)
        self.assertEqual(first + terminal, raw.replace(heartbeat, b""))

    def test_plain_json_never_receives_sse_heartbeats(self):
        body = b'{"object":"delayed-json"}'
        FakeUpstreamHandler.response_headers = {"Content-Type": "application/json"}
        FakeUpstreamHandler.stream_chunks = [(0.08, body)]
        self.restart_gateway(
            upstream_timeout_seconds=1,
            sse_heartbeat_seconds=0.02,
            adapter="identity",
        )

        status, _, raw = self.request("POST", "/v1/responses", {"input": []})

        self.assertEqual(200, status)
        self.assertEqual(body, raw)
        self.assertNotIn(b"codex-ns-proxy keep-alive", raw)

    def test_sse_completed_and_done_finish_downstream_before_upstream_eof(self):
        terminal = (
            b"event: response.completed\n"
            b'data: {"type":"response.completed","response":{"id":"done","output":[]}}\n\n'
        )
        transport_done = b"data: [DONE]\n\n"
        FakeUpstreamHandler.response_headers = {"Content-Type": "text/event-stream"}
        FakeUpstreamHandler.response_body = terminal + transport_done
        FakeUpstreamHandler.hold_open = True
        FakeUpstreamHandler.observe_disconnect = True
        self.restart_gateway(
            upstream_timeout_seconds=5,
            sse_heartbeat_seconds=0.02,
            adapter="identity",
            debug=True,
        )
        result = []
        client_errors = []

        class TerminalOrderingLog(io.StringIO):
            upstream_disconnected_at_terminal = None

            def write(log_self, value):
                if (
                    "SSE terminal_completed=true" in value
                    and log_self.upstream_disconnected_at_terminal is None
                ):
                    log_self.upstream_disconnected_at_terminal = (
                        FakeUpstreamHandler.upstream_disconnect_observed.is_set()
                    )
                return super().write(value)

        stderr = TerminalOrderingLog()

        def consume():
            connection = socket.create_connection(
                ("127.0.0.1", self.gateway.server_address[1]), timeout=1
            )
            connection.settimeout(1)
            body = b'{"input":[]}'
            request = (
                b"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                + f"Authorization: Bearer {INBOUND_TOKEN}\r\n".encode()
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            try:
                connection.sendall(request)
                chunks = []
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                result.append(b"".join(chunks))
            except OSError as error:
                client_errors.append(type(error).__name__)
            finally:
                connection.close()

        client = threading.Thread(target=consume)
        try:
            with contextlib.redirect_stderr(stderr):
                client.start()
                self.assertTrue(FakeUpstreamHandler.read_started.wait(0.5))
                client.join(1.0)
                completed_before_release = not client.is_alive()
                disconnected_before_release = (
                    FakeUpstreamHandler.upstream_disconnect_observed.wait(1.0)
                )
                terminal_logged_before_release = "SSE terminal_completed=true" in stderr.getvalue()
        finally:
            FakeUpstreamHandler.release_response.set()
            client.join(2)

        evidence = (
            f"completed={completed_before_release} "
            f"upstream_disconnected={disconnected_before_release} "
            f"logs={stderr.getvalue()!r}"
        )
        self.assertTrue(completed_before_release, evidence)
        self.assertTrue(disconnected_before_release, evidence)
        self.assertTrue(terminal_logged_before_release, evidence)
        self.assertFalse(stderr.upstream_disconnected_at_terminal, evidence)
        self.assertFalse(client.is_alive())
        self.assertEqual(1, len(result))
        self.assertEqual([], client_errors)
        headers, raw = result[0].split(b"\r\n\r\n", 1)
        self.assertTrue(headers.startswith(b"HTTP/1.1 200"), headers)
        self.assertEqual(terminal + transport_done, raw)
        FakeUpstreamHandler.hold_open = False
        FakeUpstreamHandler.response_headers = {"Content-Type": "application/json"}
        FakeUpstreamHandler.response_body = b'{"object":"ok"}'
        status, _, _ = self.request("GET", "/v1/models")
        self.assertEqual(200, status)

    def test_sse_completed_without_done_logs_immediately_and_uses_timeout_fallback(
        self,
    ):
        terminal = (
            b"event: response.completed\n"
            b'data: {"type":"response.completed","response":{"id":"semantic","output":[]}}\n\n'
        )
        FakeUpstreamHandler.response_headers = {"Content-Type": "text/event-stream"}
        FakeUpstreamHandler.response_body = terminal
        FakeUpstreamHandler.hold_open = True
        self.restart_gateway(
            upstream_timeout_seconds=0.5,
            sse_heartbeat_seconds=0.02,
            adapter="identity",
            debug=True,
        )
        result = []

        class SemanticTerminalLog(io.StringIO):
            terminal_logged = threading.Event()

            def write(log_self, value):
                if "SSE terminal_completed=true" in value:
                    log_self.terminal_logged.set()
                return super().write(value)

        stderr = SemanticTerminalLog()
        client = threading.Thread(
            target=lambda: result.append(
                self.request("POST", "/v1/responses", {"input": []})
            )
        )
        try:
            with contextlib.redirect_stderr(stderr):
                client.start()
                self.assertTrue(stderr.terminal_logged.wait(0.3))
                client.join(0.05)
                waiting_for_transport_fallback = client.is_alive()
                client.join(1)
        finally:
            FakeUpstreamHandler.release_response.set()
            client.join(2)

        self.assertTrue(waiting_for_transport_fallback)
        self.assertFalse(client.is_alive())
        self.assertEqual(1, len(result))
        status, _, raw = result[0]
        self.assertEqual(200, status)
        self.assertEqual(terminal, raw)
        self.assertNotIn(b"[DONE]", raw)
        self.assertNotIn(b"codex-ns-proxy keep-alive", raw)
        self.assertEqual(1, stderr.getvalue().count("SSE terminal_completed=true"))

    def test_sse_terminal_frame_materialized_at_eof_is_flushed_once(self):
        terminal = (
            b"event: response.completed\n"
            b'data: {"type":"response.completed","response":{"id":"eof","output":[]}}\n'
        )
        FakeUpstreamHandler.response_headers = {"Content-Type": "text/event-stream"}
        FakeUpstreamHandler.stream_chunks = [(0, terminal)]
        self.restart_gateway(adapter="identity", debug=True)
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            status, _, raw = self.request("POST", "/v1/responses", {"input": []})

        self.assertEqual(200, status)
        self.assertEqual(terminal, raw)
        self.assertEqual(1, stderr.getvalue().count("SSE terminal_completed=true"))

    def test_upstream_error_is_forwarded_without_dump_or_fabrication(self):
        FakeUpstreamHandler.response_status = 429
        FakeUpstreamHandler.response_body = b'{"error":{"message":"limited"}}'
        status, _, raw = self.request("POST", "/v1/responses", {"input": []})
        self.assertEqual(429, status)
        self.assertEqual({"error": {"message": "limited"}}, json.loads(raw))

    def test_body_size_limit_rejects_before_upstream(self):
        self.gateway.shutdown()
        self.gateway.server_close()
        self.gateway_thread.join(2)
        self.gateway = proxy.create_server(
            proxy.ProxyConfig(
                upstream_url=self.config.upstream_url,
                inbound_token=INBOUND_TOKEN,
                listen_port=0,
                max_body_bytes=4,
                adapter="codex-namespace",
            )
        )
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever)
        self.gateway_thread.start()
        status, _, _ = self.request("POST", "/v1/responses", {"input": []})
        self.assertEqual(413, status)
        self.assertEqual([], FakeUpstreamHandler.requests)

    def test_logs_do_not_contain_tokens_or_content(self):
        self.gateway.shutdown()
        self.gateway.server_close()
        self.gateway_thread.join(2)
        self.gateway = proxy.create_server(
            proxy.ProxyConfig(
                upstream_url=self.config.upstream_url,
                inbound_token=INBOUND_TOKEN,
                upstream_token=UPSTREAM_TOKEN,
                listen_port=0,
                adapter="codex-namespace",
                debug=True,
            )
        )
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever)
        self.gateway_thread.start()
        FakeUpstreamHandler.response_body = (
            b'{"output":[{"type":"message","content":['
            b'{"type":"output_text","text":"generated-secret"}]}]}'
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status, _, _ = self.request(
                "POST",
                "/v1/responses",
                {
                    "input": [{"role": "user", "content": "prompt-secret"}],
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "namespace-secret",
                            "description": "description-secret",
                            "tools": [
                                {
                                    "type": "function",
                                    "name": "tool-secret",
                                    "description": "argument-secret",
                                }
                            ],
                        }
                    ],
                },
            )
        self.assertEqual(200, status)
        logs = stderr.getvalue()
        for secret in (
            INBOUND_TOKEN,
            UPSTREAM_TOKEN,
            "prompt-secret",
            "namespace-secret",
            "description-secret",
            "tool-secret",
            "argument-secret",
            "generated-secret",
        ):
            self.assertNotIn(secret, logs)
        self.assertIn("request transformed namespaces=1", logs)

    def test_flattening_collision_is_rejected_before_upstream(self):
        body = {
            "input": [],
            "tools": [
                {"type": "function", "name": "math__add", "parameters": {}},
                {
                    "type": "namespace",
                    "name": "math",
                    "tools": [{"type": "function", "name": "add", "parameters": {}}],
                },
            ],
        }
        status, _, raw = self.request("POST", "/v1/responses", body)
        self.assertEqual(400, status)
        self.assertIn("collision", json.loads(raw)["error"]["message"])
        self.assertEqual([], FakeUpstreamHandler.requests)

    def test_generated_name_collision_with_delimiters_is_rejected(self):
        body = {
            "input": [],
            "tools": [
                {
                    "type": "namespace",
                    "name": "a__b",
                    "tools": [{"type": "function", "name": "c"}],
                },
                {
                    "type": "namespace",
                    "name": "a",
                    "tools": [{"type": "function", "name": "b__c"}],
                },
            ],
        }
        status, _, raw = self.request("POST", "/v1/responses", body)
        self.assertEqual(400, status)
        self.assertIn("collision", json.loads(raw)["error"]["message"])
        self.assertEqual([], FakeUpstreamHandler.requests)

    def test_ordinary_double_underscore_call_is_not_reconstructed(self):
        FakeUpstreamHandler.response_body = json.dumps(
            {"output": [{"type": "function_call", "name": "ordinary__function"}]}
        ).encode()
        status, _, raw = self.request(
            "POST",
            "/v1/responses",
            {
                "input": [],
                "tools": [{"type": "function", "name": "ordinary__function", "parameters": {}}],
            },
        )
        self.assertEqual(200, status)
        call = json.loads(raw)["output"][0]
        self.assertEqual("ordinary__function", call["name"])
        self.assertNotIn("namespace", call)

    def test_identity_adapter_preserves_namespace_payload(self):
        self.gateway.shutdown()
        self.gateway.server_close()
        self.gateway_thread.join(2)
        self.gateway = proxy.create_server(
            proxy.ProxyConfig(
                upstream_url=self.config.upstream_url,
                inbound_token=INBOUND_TOKEN,
                listen_port=0,
                adapter="identity",
            )
        )
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever)
        self.gateway_thread.start()
        body = {
            "input": [],
            "tools": [
                {
                    "type": "namespace",
                    "name": "math",
                    "tools": [{"type": "custom", "name": "opaque"}],
                }
            ],
        }
        status, _, _ = self.request("POST", "/v1/responses", body)
        self.assertEqual(200, status)
        self.assertEqual(body, json.loads(FakeUpstreamHandler.requests[0]["body"]))

    def test_namespace_adapter_rejects_custom_children_and_prior_response_chain(self):
        for body in [
            {
                "input": [],
                "tools": [
                    {
                        "type": "namespace",
                        "name": "math",
                        "tools": [{"type": "custom", "name": "opaque"}],
                    }
                ],
            },
            {
                "previous_response_id": "response-one",
                "input": [],
                "tools": [
                    {
                        "type": "namespace",
                        "name": "math",
                        "tools": [{"type": "function", "name": "add"}],
                    }
                ],
            },
            {
                "input": [
                    {
                        "type": "custom_tool_call",
                        "namespace": "math",
                        "name": "opaque",
                    }
                ]
            },
        ]:
            with self.subTest(body=body):
                status, _, _ = self.request("POST", "/v1/responses", body)
                self.assertEqual(400, status)
        self.assertEqual([], FakeUpstreamHandler.requests)

    def test_previous_response_id_inherits_exact_bounded_mapping(self):
        tools = [
            {
                "type": "namespace",
                "name": "math",
                "tools": [{"type": "function", "name": "add", "parameters": {}}],
            }
        ]
        FakeUpstreamHandler.response_body = b'{"id":"response-one","output":[]}'
        first_status, _, _ = self.request(
            "POST", "/v1/responses", {"input": [], "tools": tools}
        )
        self.assertEqual(200, first_status)
        FakeUpstreamHandler.response_body = (
            b'{"id":"response-two","output":['
            b'{"type":"function_call","name":"math__add","arguments":"{}"}]}'
        )
        status, _, raw = self.request(
            "POST",
            "/v1/responses",
            {"previous_response_id": "response-one", "input": []},
        )
        self.assertEqual(200, status)
        call = json.loads(raw)["output"][0]
        self.assertEqual(("math", "add"), (call["namespace"], call["name"]))
        forwarded = json.loads(FakeUpstreamHandler.requests[1]["body"])
        self.assertEqual("response-one", forwarded["previous_response_id"])

    def test_previous_response_id_preserves_known_empty_mapping(self):
        FakeUpstreamHandler.response_body = b'{"id":"ordinary-one","output":[]}'
        first_status, _, _ = self.request(
            "POST", "/v1/responses", {"input": [], "tools": []}
        )
        self.assertEqual(200, first_status)
        FakeUpstreamHandler.response_body = b'{"id":"ordinary-two","output":[]}'
        second_status, _, _ = self.request(
            "POST",
            "/v1/responses",
            {"previous_response_id": "ordinary-one", "input": [], "tools": []},
        )
        self.assertEqual(200, second_status)
        self.assertEqual(2, len(FakeUpstreamHandler.requests))

    def test_inherited_namespace_mapping_rejects_changed_ordinary_tool_collision(self):
        namespace_tools = [
            {
                "type": "namespace",
                "name": "math",
                "tools": [{"type": "function", "name": "add"}],
            }
        ]
        FakeUpstreamHandler.response_body = b'{"id":"mapped-one","output":[]}'
        first_status, _, _ = self.request(
            "POST", "/v1/responses", {"input": [], "tools": namespace_tools}
        )
        self.assertEqual(200, first_status)
        status, _, raw = self.request(
            "POST",
            "/v1/responses",
            {
                "previous_response_id": "mapped-one",
                "input": [],
                "tools": [{"type": "function", "name": "math__add"}],
            },
        )
        self.assertEqual(400, status)
        self.assertIn("collides", json.loads(raw)["error"]["message"])
        self.assertEqual(1, len(FakeUpstreamHandler.requests))

    def test_malformed_namespaced_history_fails_closed(self):
        malformed = [
            {"type": "function_call", "namespace": "", "name": "add"},
            {"type": "function_call", "namespace": 1, "name": "add"},
            {"type": "function_call", "namespace": "math", "name": ""},
            {"type": "function_call", "namespace": "math", "name": 1},
        ]
        for item in malformed:
            with self.subTest(item=item):
                status, _, _ = self.request(
                    "POST", "/v1/responses", {"input": [item], "tools": []}
                )
                self.assertEqual(400, status)
        self.assertEqual([], FakeUpstreamHandler.requests)

    def test_upstream_timeout_is_fixed_and_sanitized(self):
        FakeUpstreamHandler.response_delay = 0.2
        self.gateway.shutdown()
        self.gateway.server_close()
        self.gateway_thread.join(2)
        self.gateway = proxy.create_server(
            proxy.ProxyConfig(
                upstream_url=self.config.upstream_url,
                inbound_token=INBOUND_TOKEN,
                listen_port=0,
                upstream_timeout_seconds=0.05,
                adapter="codex-namespace",
            )
        )
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever)
        self.gateway_thread.start()
        status, _, raw = self.request("POST", "/v1/responses", {"input": []})
        self.assertEqual(504, status)
        self.assertEqual({"error": {"message": "upstream timeout"}}, json.loads(raw))

    def test_downstream_cancellation_does_not_stop_gateway_or_emit_traceback(self):
        FakeUpstreamHandler.response_delay = 0.1
        body = b'{"input":[]}'
        request = (
            b"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            + f"Authorization: Bearer {INBOUND_TOKEN}\r\n".encode()
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        client = socket.create_connection(("127.0.0.1", self.gateway.server_address[1]))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            client.sendall(request)
            client.close()
            time.sleep(0.2)
        FakeUpstreamHandler.response_delay = 0
        status, _, _ = self.request("GET", "/v1/models")
        self.assertEqual(200, status)
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_server_close_interrupts_live_sse_and_returns_bounded(self):
        FakeUpstreamHandler.response_headers = {"Content-Type": "text/event-stream"}
        FakeUpstreamHandler.response_body = (
            b"event: response.output_text.delta\n"
            b'data: {"type":"response.output_text.delta","delta":"x"}\n\n'
        )
        FakeUpstreamHandler.hold_open = True
        client_errors = []
        downstream_stream_started = threading.Event()

        def consume():
            connection = http.client.HTTPConnection(
                "127.0.0.1", self.gateway.server_address[1], timeout=2
            )
            try:
                body = json.dumps({"input": []}).encode()
                connection.request(
                    "POST",
                    "/v1/responses",
                    body=body,
                    headers={
                        "Authorization": f"Bearer {INBOUND_TOKEN}",
                        "Content-Type": "application/json",
                    },
                )
                response = connection.getresponse()
                if response.read(1):
                    downstream_stream_started.set()
                response.read()
            except (OSError, http.client.HTTPException) as error:
                client_errors.append(type(error).__name__)
            finally:
                connection.close()

        client = threading.Thread(target=consume)
        client.start()
        self.assertTrue(downstream_stream_started.wait(1))
        self.gateway.shutdown()
        self.gateway_thread.join(2)
        close_thread = threading.Thread(target=self.gateway.server_close)
        close_thread.start()
        close_thread.join(1)
        if close_thread.is_alive():
            FakeUpstreamHandler.release_response.set()
            close_thread.join(2)
            self.fail("server_close did not interrupt the live SSE upstream")
        FakeUpstreamHandler.release_response.set()
        client.join(2)
        self.assertFalse(client.is_alive())

    def test_downstream_disconnect_closes_upstream_stream_and_gateway_stays_healthy(self):
        FakeUpstreamHandler.response_headers = {"Content-Type": "text/event-stream"}
        FakeUpstreamHandler.response_body = b""
        FakeUpstreamHandler.hold_open = True
        FakeUpstreamHandler.observe_disconnect = True
        self.restart_gateway(
            upstream_timeout_seconds=1,
            sse_heartbeat_seconds=0.02,
            adapter="identity",
        )
        body = b'{"input":[]}'
        request = (
            b"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            + f"Authorization: Bearer {INBOUND_TOKEN}\r\n".encode()
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        client = socket.create_connection(("127.0.0.1", self.gateway.server_address[1]))
        client.settimeout(1)
        client.sendall(request)
        received = b""
        while b": codex-ns-proxy keep-alive\n\n" not in received:
            received += client.recv(65536)
        client.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )
        client.close()

        self.assertTrue(FakeUpstreamHandler.upstream_disconnect_observed.wait(1))
        FakeUpstreamHandler.release_response.set()
        FakeUpstreamHandler.hold_open = False
        FakeUpstreamHandler.response_headers = {"Content-Type": "application/json"}
        FakeUpstreamHandler.response_body = b'{"object":"ok"}'
        status, _, _ = self.request("GET", "/v1/models")

        self.assertEqual(200, status)

    def test_shutdown_race_after_getresponse_returns_503_and_closes_handler(self):
        reached_registration = threading.Event()
        allow_registration = threading.Event()
        original_register = self.gateway.register_upstream_response

        def synchronized_register(lifecycle, response):
            reached_registration.set()
            self.assertTrue(allow_registration.wait(2))
            return original_register(lifecycle, response)

        self.gateway.register_upstream_response = synchronized_register
        result = []

        def request_during_shutdown():
            try:
                result.append(self.request("GET", "/v1/models"))
            except Exception as error:
                result.append(error)

        client = threading.Thread(target=request_during_shutdown)
        client.start()
        self.assertTrue(reached_registration.wait(1))
        self.gateway.shutdown()
        self.gateway_thread.join(2)
        close_thread = threading.Thread(target=self.gateway.server_close)
        close_thread.start()
        deadline = time.monotonic() + 1
        while not self.gateway.is_closing() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(self.gateway.is_closing())
        allow_registration.set()
        close_thread.join(1)
        client.join(1)
        self.assertFalse(close_thread.is_alive())
        self.assertFalse(client.is_alive())
        self.assertEqual(1, len(result))
        self.assertIsInstance(result[0], tuple)
        status, headers, raw = result[0]
        self.assertEqual(503, status)
        self.assertEqual("close", headers["Connection"])
        self.assertEqual(
            {"error": {"message": "gateway shutting down"}}, json.loads(raw)
        )

    def test_timeout_after_sse_headers_never_writes_second_status(self):
        FakeUpstreamHandler.response_headers = {"Content-Type": "text/event-stream"}
        FakeUpstreamHandler.response_body = b"data: {}\n\n"
        handler_class = self.gateway.RequestHandlerClass
        original_stream = handler_class._stream_sse

        def timeout_after_headers(handler, _response, _lifecycle, _reconstruction):
            handler.close_connection = True
            handler._response_started = True
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(b"data: {}\n\n")
            handler.wfile.flush()
            raise socket.timeout()

        handler_class._stream_sse = timeout_after_headers
        body = b'{"input":[]}'
        try:
            raw = self.raw_request(
                b"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                + f"Authorization: Bearer {INBOUND_TOKEN}\r\n".encode()
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
        finally:
            handler_class._stream_sse = original_stream
        self.assertTrue(raw.startswith(b"HTTP/1.1 200"), raw)
        self.assertEqual(1, raw.count(b"HTTP/1.1"), raw)
        self.assertNotIn(b"504", raw)

    def test_multiline_crlf_sse_reconstructs_only_changed_frame(self):
        sse = (
            b": comment\r\n"
            b"event: response.output_item.done\r\n"
            b'data: {"type":"response.output_item.done",\r\n'
            b'data: "item":{"type":"function_call","name":"math__add"}}\r\n'
            b"\r\n"
            b": unchanged\r\n"
            b"event: response.output_text.delta\r\n"
            b'data: {"type":"response.output_text.delta","delta":"x"}\r\n'
            b"\r\n"
        )
        FakeUpstreamHandler.response_headers = {"Content-Type": "text/event-stream"}
        FakeUpstreamHandler.response_body = sse
        status, _, raw = self.request(
            "POST",
            "/v1/responses",
            {
                "input": [],
                "tools": [
                    {
                        "type": "namespace",
                        "name": "math",
                        "tools": [{"type": "function", "name": "add"}],
                    }
                ],
            },
        )
        self.assertEqual(200, status)
        self.assertTrue(raw.startswith(b": comment\r\nevent: response.output_item.done\r\n"))
        self.assertIn(b'"name":"add","namespace":"math"', raw)
        unchanged = (
            b": unchanged\r\nevent: response.output_text.delta\r\n"
            b'data: {"type":"response.output_text.delta","delta":"x"}\r\n\r\n'
        )
        self.assertIn(unchanged, raw)


class PureTransformAndConfigTest(unittest.TestCase):
    def test_response_transform_does_not_mutate_caller_input(self):
        source = {
            "response": {
                "output": [{"type": "function_call", "name": "github__get_me"}]
            }
        }
        original = copy.deepcopy(source)
        result = proxy.transform_response(source, {"github__get_me": ("github", "get_me")})
        self.assertEqual(original, source)
        self.assertEqual("get_me", result["response"]["output"][0]["name"])
        self.assertEqual("github", result["response"]["output"][0]["namespace"])

    def test_custom_tool_response_remains_identity_even_with_matching_function_map(self):
        source = {"output": [{"type": "custom_tool_call", "name": "github__get_me"}]}
        result = proxy.transform_response(
            source, {"github__get_me": ("github", "get_me")}
        )
        self.assertEqual(source, result)

    def test_terminal_observation_requires_valid_matching_completion_data(self):
        for frame in [
            [b"event: response.completed\n", b"data: not-json\n", b"\n"],
            [b"event: response.completed\n", b"\n"],
            [
                b"event: response.output_text.delta\n",
                b'data: {"type":"response.completed"}\n',
                b"\n",
            ],
        ]:
            with self.subTest(frame=frame):
                _, terminal, _ = proxy._transform_sse_frame(frame, {})
                self.assertFalse(terminal)

    def test_transport_done_requires_exact_single_data_payload(self):
        self.assertTrue(proxy._is_sse_done_frame([b"data: [DONE]\n", b"\n"]))
        self.assertTrue(proxy._is_sse_done_frame([b"data:[DONE]\r\n", b"\r\n"]))
        for frame in [
            [b"data: [DONE] \n", b"\n"],
            [b"data: [done]\n", b"\n"],
            [b"data: [DONE]\n", b"data: extra\n", b"\n"],
            [b": [DONE]\n", b"\n"],
            [b"event: response.completed\n", b"data: [DONE]\n", b"\n"],
            [b"id: done\n", b"data: [DONE]\n", b"\n"],
            [b"retry: 1\n", b"data: [DONE]\n", b"\n"],
            [b": comment\n", b"data: [DONE]\n", b"\n"],
        ]:
            with self.subTest(frame=frame):
                self.assertFalse(proxy._is_sse_done_frame(frame))

    def test_configuration_requires_explicit_upstream_and_fresh_token(self):
        with self.assertRaises(proxy.ConfigurationError):
            proxy.ProxyConfig.from_env({"NS_PROXY_INBOUND_TOKEN": INBOUND_TOKEN})
        with self.assertRaises(proxy.ConfigurationError):
            proxy.ProxyConfig.from_env({"NS_PROXY_UPSTREAM": "http://127.0.0.1/v1"})
        with self.assertRaises(proxy.ConfigurationError):
            proxy.ProxyConfig(
                upstream_url="http://127.0.0.1/v1", inbound_token="short", listen_port=0
            )

    def test_sse_heartbeat_configuration_has_positive_finite_env_contract(self):
        base_env = {
            "NS_PROXY_UPSTREAM": "http://127.0.0.1/v1",
            "NS_PROXY_INBOUND_TOKEN": INBOUND_TOKEN,
        }
        self.assertEqual(15.0, proxy.ProxyConfig.from_env(base_env).sse_heartbeat_seconds)
        configured = proxy.ProxyConfig.from_env(
            {**base_env, "NS_PROXY_SSE_HEARTBEAT": "7.5"}
        )
        self.assertEqual(7.5, configured.sse_heartbeat_seconds)
        for invalid in ("0", "-1", "nan", "inf"):
            with self.subTest(invalid=invalid), self.assertRaises(
                proxy.ConfigurationError
            ):
                proxy.ProxyConfig.from_env(
                    {**base_env, "NS_PROXY_SSE_HEARTBEAT": invalid}
                )

    def test_configuration_rejects_non_loopback_listener_and_embedded_credentials(self):
        with self.assertRaises(proxy.ConfigurationError):
            proxy.ProxyConfig(
                upstream_url="http://127.0.0.1/v1",
                inbound_token=INBOUND_TOKEN,
                listen_host="0.0.0.0",
            )
        with self.assertRaises(proxy.ConfigurationError):
            proxy.ProxyConfig(
                upstream_url="https://user:password@example.test/v1",
                inbound_token=INBOUND_TOKEN,
            )
        with self.assertRaises(proxy.ConfigurationError):
            proxy.ProxyConfig(
                upstream_url="http://127.0.0.1/v1",
                inbound_token=INBOUND_TOKEN,
                upstream_token="unsafe\r\nvalue",
            )
        with self.assertRaises(proxy.ConfigurationError):
            proxy.ProxyConfig(
                upstream_url="http://127.0.0.1/v1",
                inbound_token=INBOUND_TOKEN,
                upstream_token=INBOUND_TOKEN,
            )

    def test_creating_server_does_not_contact_upstream(self):
        config = proxy.ProxyConfig(
            upstream_url="http://127.0.0.1:1/v1",
            inbound_token=INBOUND_TOKEN,
            listen_port=0,
        )
        server = proxy.create_server(config)
        server.server_close()

    def test_namespace_state_is_bounded_and_evicts_least_recently_used(self):
        state = proxy._NamespaceState(2)
        state.remember("one", {"a__b": ("a", "b")})
        state.remember("two", {"c__d": ("c", "d")})
        self.assertIsNotNone(state.get("one"))
        state.remember("three", {"e__f": ("e", "f")})
        self.assertIsNone(state.get("two"))
        self.assertIsNotNone(state.get("one"))
        self.assertIsNotNone(state.get("three"))

    def test_server_close_allows_immediate_port_rebind(self):
        first = proxy.create_server(
            proxy.ProxyConfig(
                upstream_url="http://127.0.0.1:1/v1",
                inbound_token=INBOUND_TOKEN,
                listen_port=0,
            )
        )
        port = first.server_address[1]
        first.server_close()
        second = proxy.create_server(
            proxy.ProxyConfig(
                upstream_url="http://127.0.0.1:1/v1",
                inbound_token=INBOUND_TOKEN,
                listen_port=port,
            )
        )
        second.server_close()

    def test_close_failures_are_contained_and_listener_still_closes(self):
        class RaisingConnection:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True
                raise RuntimeError("injected connection close failure")

        class RecordingResponse:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        connection = RaisingConnection()
        response = RecordingResponse()
        lifecycle = proxy._UpstreamLifecycle(connection)
        self.assertTrue(lifecycle.attach_response(response))
        lifecycle.close()
        self.assertTrue(connection.closed)
        self.assertTrue(response.closed)

        class RaisingLifecycle:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True
                raise RuntimeError("injected lifecycle close failure")

        class RecordingLifecycle:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        server = proxy.create_server(
            proxy.ProxyConfig(
                upstream_url="http://127.0.0.1:1/v1",
                inbound_token=INBOUND_TOKEN,
                listen_port=0,
            )
        )
        port = server.server_address[1]
        raising = RaisingLifecycle()
        recording = RecordingLifecycle()
        server._active_upstreams.update({raising, recording})
        server.server_close()
        self.assertTrue(raising.closed)
        self.assertTrue(recording.closed)
        rebound = proxy.create_server(
            proxy.ProxyConfig(
                upstream_url="http://127.0.0.1:1/v1",
                inbound_token=INBOUND_TOKEN,
                listen_port=port,
            )
        )
        rebound.server_close()


if __name__ == "__main__":
    unittest.main()
