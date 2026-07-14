import json
import socket
import struct
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mlxctl.probe import ProbeError, probe_liveness, probe_readiness


class ProbeTests(unittest.TestCase):
    def test_reads_liveness_and_readiness_from_a_real_http_server(self) -> None:
        base_url = self._serve(
            {
                "/health": {"status": "ok"},
                "/v1/models": {
                    "object": "list",
                    "data": [{"id": "repo/one"}, {"id": "/models/two"}],
                },
            }
        )

        live = probe_liveness(base_url)
        model_ids = probe_readiness(base_url)

        self.assertIs(live, True)
        self.assertEqual(model_ids, ("repo/one", "/models/two"))

    def test_live_server_can_be_distinguished_from_not_ready_server(self) -> None:
        base_url = self._serve(
            {
                "/health": {"status": "ok"},
                "/v1/models": {"data": {}},
            }
        )

        self.assertIs(probe_liveness(base_url), True)
        with self.assertRaisesRegex(
            ProbeError, "/v1/models must return an object with list 'data'"
        ):
            probe_readiness(base_url)

    def test_rejects_responses_outside_the_uniform_probe_contract(self) -> None:
        cases = {
            "/health must return exactly": {
                "/health": {"status": "ok", "detail": "extra"},
                "/v1/models": {"data": []},
            },
            "/v1/models must return an object with list 'data'": {
                "/health": {"status": "ok"},
                "/v1/models": {"data": {}},
            },
            r"/v1/models data\[0\] must contain string 'id'": {
                "/health": {"status": "ok"},
                "/v1/models": {"data": [{"id": 42}]},
            },
        }

        for message, payloads in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ProbeError, message):
                    base_url = self._serve(payloads)
                    if message.startswith("/health"):
                        probe_liveness(base_url)
                    else:
                        probe_readiness(base_url)

    def test_normalizes_connection_reset_during_response_for_both_probes(self) -> None:
        for probe in (probe_liveness, probe_readiness):
            for endpoint in (self._reset_during_response, self._truncate_response):
                with self.subTest(probe=probe.__name__, failure=endpoint.__name__):
                    base_url = endpoint()

                    with self.assertRaisesRegex(ProbeError, "GET .* failed"):
                        probe(base_url)

    def _serve(self, payloads: dict[str, object]) -> str:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                payload = payloads.get(self.path)
                if payload is None:
                    self.send_error(404)
                    return
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format_string: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address
        return f"http://{host}:{port}"

    def _reset_during_response(self) -> str:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()

        def reset() -> None:
            connection, _ = listener.accept()
            connection.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
            connection.close()

        thread = threading.Thread(target=reset, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(listener.close)
        host, port = listener.getsockname()
        return f"http://{host}:{port}"

    def _truncate_response(self) -> str:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()

        def truncate() -> None:
            connection, _ = listener.accept()
            connection.recv(4096)
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n{")
            connection.close()

        thread = threading.Thread(target=truncate, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(listener.close)
        host, port = listener.getsockname()
        return f"http://{host}:{port}"


if __name__ == "__main__":
    unittest.main()
