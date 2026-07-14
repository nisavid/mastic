import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from mlxctl.control import (
    ControlClient,
    ControlDomainError,
    ControlPlane,
    ControlRequest,
    ControlResult,
    ControlSocketError,
    UnixControlServer,
)
from mlxctl.config import load_config
from mlxctl.metrics import MetricsEngine, RequestMetricEvent, RequestOutcome
from mlxctl.supervisor import Supervisor


_FAKE_SERVER = r"""#!/usr/bin/env python3
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--model", required=True)
parser.add_argument("--host", required=True)
parser.add_argument("--port", type=int, required=True)
args, _ = parser.parse_known_args()

count_file = __import__("os").environ.get("FAKE_START_COUNT_FILE")
if count_file:
    with open(count_file, "a", encoding="utf-8") as stream:
        stream.write("started\n")

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"status": "ok"})
        if self.path == "/v1/models":
            return self.reply(200, {"object": "list", "data": [{"id": args.model}]})
        self.reply(404, {"error": "missing"})

    def reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass

ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
"""


class _EchoControlPlane:
    def handle(self, request: ControlRequest) -> ControlResult:
        return ControlResult(
            {"command": request.command, "server_id": request.server_id}
        )


class _FailingControlPlane:
    def __init__(self, message: str) -> None:
        self.message = message

    def handle(self, request: ControlRequest) -> ControlResult:
        raise ControlDomainError("failed", self.message)


class _BlockingControlPlane:
    def __init__(self, clients: int) -> None:
        self.arrived = threading.Barrier(clients + 1)
        self.release = threading.Event()

    def handle(self, request: ControlRequest) -> ControlResult:
        self.arrived.wait(2)
        self.release.wait()
        return ControlResult({})


class UnixControlProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.socket_path = self.root / "state" / "mlxd.sock"
        self.server = UnixControlServer(self.socket_path, _EchoControlPlane())
        self.server.start()

    def tearDown(self) -> None:
        self.server.close()
        self.temporary.cleanup()

    def test_real_client_round_trips_one_versioned_request(self) -> None:
        result = ControlClient(self.socket_path).send(
            ControlRequest(command="status", server_id="chat")
        )

        self.assertEqual(result.value, {"command": "status", "server_id": "chat"})
        self.assertEqual(stat.S_IMODE(self.socket_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.socket_path.stat().st_mode), 0o600)

    def test_fast_peer_close_never_turns_a_successful_response_into_enotconn(
        self,
    ) -> None:
        client = ControlClient(self.socket_path)

        for _ in range(200):
            result = client.send(ControlRequest(command="status"))
            self.assertEqual(result.value["command"], "status")

    def test_malformed_version_and_unknown_commands_are_stable_errors(self) -> None:
        cases = (
            (b"not json\n", "malformed_request"),
            (b'{"version":2,"command":"status"}\n', "unsupported_version"),
            (b'{"version":1,"command":"nope"}\n', "unknown_command"),
        )
        for request, code in cases:
            with self.subTest(code=code):
                response = self._raw(request)
                self.assertEqual(response["version"], 1)
                self.assertIs(response["ok"], False)
                self.assertEqual(response["error"]["code"], code)
                self.assertNotIn(str(self.root), response["error"]["message"])

    def test_oversize_request_is_rejected_without_stopping_server(self) -> None:
        response = self._raw(b"{" + b"x" * (1024 * 1024) + b"}\n")

        self.assertEqual(response["error"]["code"], "request_too_large")
        self.assertEqual(
            ControlClient(self.socket_path)
            .send(ControlRequest(command="status"))
            .value["command"],
            "status",
        )

    def test_protocol_error_redacts_quoted_absolute_path(self) -> None:
        self.server.close()
        secret = self.root / "operator" / "config.toml"
        self.server = UnixControlServer(
            self.socket_path,
            _FailingControlPlane(f"cannot read '{secret}'"),
        )
        self.server.start()

        response = self._raw(b'{"version":1,"command":"status"}\n')

        self.assertEqual(response["error"]["code"], "failed")
        self.assertNotIn(str(self.root), response["error"]["message"])
        self.assertIn("<path>", response["error"]["message"])

    def _raw(self, request: bytes) -> dict[str, object]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(os.fspath(self.socket_path))
            client.sendall(request)
            response = b""
            while not response.endswith(b"\n"):
                response += client.recv(65536)
        return json.loads(response)


class UnixControlSocketSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "state" / "mlxd.sock"
        self.path.parent.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_rejects_non_socket_and_live_socket_collisions(self) -> None:
        self.path.write_text("owned by operator", encoding="utf-8")
        with self.assertRaisesRegex(ControlSocketError, "non-socket"):
            UnixControlServer(self.path, _EchoControlPlane()).start()
        self.path.unlink()

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as live:
            live.bind(os.fspath(self.path))
            live.listen()
            with self.assertRaisesRegex(ControlSocketError, "already listening"):
                UnixControlServer(self.path, _EchoControlPlane()).start()

    def test_rejects_symlink_collision_without_touching_its_target(self) -> None:
        target = self.root / "operator-file"
        target.write_text("preserve", encoding="utf-8")
        self.path.symlink_to(target)

        with self.assertRaisesRegex(ControlSocketError, "non-socket"):
            UnixControlServer(self.path, _EchoControlPlane()).start()

        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "preserve")

    def test_indeterminate_live_probe_never_unlinks_socket(self) -> None:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(os.fspath(self.path))
        stale.close()
        inode = self.path.stat().st_ino

        with mock.patch.object(
            socket.socket, "connect", side_effect=PermissionError("denied")
        ):
            with self.assertRaisesRegex(ControlSocketError, "cannot safely probe"):
                UnixControlServer(self.path, _EchoControlPlane()).start()

        self.assertEqual(self.path.stat().st_ino, inode)

    def test_replaces_stale_socket_but_preserves_replacement_inode_on_close(
        self,
    ) -> None:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(os.fspath(self.path))
        stale.close()

        server = UnixControlServer(self.path, _EchoControlPlane())
        server.start()
        self.path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as replacement:
            replacement.bind(os.fspath(self.path))
            replacement.listen()
            replacement_inode = self.path.stat().st_ino
            server.close()
            self.assertEqual(self.path.stat().st_ino, replacement_inode)

    def test_close_is_bounded_with_concurrent_blocked_handlers(self) -> None:
        handler = _BlockingControlPlane(4)
        server = UnixControlServer(
            self.root / "blocked.sock", handler, io_timeout_seconds=0.05
        )
        server.start()
        clients = []
        release = None
        try:
            for _ in range(4):
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.connect(os.fspath(server.path))
                client.sendall(b'{"version":1,"command":"status"}\n')
                clients.append(client)
            handler.arrived.wait(2)
            release = threading.Timer(1, handler.release.set)
            release.start()
            started = time.monotonic()

            server.close()

            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            handler.release.set()
            if release is not None:
                release.cancel()
            for client in clients:
                client.close()
            server.close()


class ControlPlaneProcessSeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        executable = self.bin_dir / "mlx_lm.server"
        executable.write_text(_FAKE_SERVER, encoding="utf-8")
        executable.chmod(0o755)
        self.config_path = self.root / "config.toml"
        self.port = self._free_port()
        self.start_count = self.root / "start-count"
        self._write_valid_config()
        config = load_config(self.config_path)
        self.metrics = MetricsEngine(self.root / "state" / "metrics.db")
        self.supervisor = Supervisor(
            config.daemon, self.metrics, self.root / "state", self.root / "logs"
        )
        self.server = UnixControlServer(
            self.root / "state" / "mlxd.sock",
            ControlPlane(self.config_path, self.supervisor, self.metrics),
        )
        self.server.start()
        self.client = ControlClient(self.root / "state" / "mlxd.sock")

    def tearDown(self) -> None:
        self.server.close()
        self.supervisor.close()
        self.temporary.cleanup()

    def test_start_status_models_and_stop_cross_real_process_seams(self) -> None:
        started = self.client.send(ControlRequest("start", "chat")).value
        self.assertEqual(started["lifecycle"], "ready")
        self.assertEqual(started["model_alias"], "tiny")
        self.assertNotIn("model_id", started)

        status = self.client.send(ControlRequest("status")).value
        self.assertEqual(status["servers"][0]["server_id"], "chat")
        self.assertEqual(status["servers"][0]["lifecycle"], "ready")
        models = self.client.send(ControlRequest("models", "chat")).value
        self.assertEqual(models["models"], ("repo/tiny",))
        self.metrics.record(
            RequestMetricEvent(
                "chat",
                "tiny",
                started["run_id"],
                datetime(2026, 7, 13, 20, tzinfo=UTC),
                25.0,
                5.0,
                200,
                RequestOutcome.COMPLETED,
                4,
                2,
                6,
                1,
            )
        )
        metrics = self.client.send(
            ControlRequest(
                "metrics",
                server_id="chat",
                model_alias="tiny",
                start=datetime(2026, 7, 13, 19, tzinfo=UTC),
                end=datetime(2026, 7, 13, 21, tzinfo=UTC),
            )
        ).value
        self.assertEqual(metrics["summaries"][0]["request_count"], 1)
        self.assertEqual(metrics["summaries"][0]["total_tokens"], 6)

        stopped = self.client.send(ControlRequest("stop", "chat")).value
        self.assertEqual(stopped["lifecycle"], "stopped")

    def test_concurrent_client_starts_create_one_child(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = tuple(
                pool.map(
                    lambda _index: (
                        ControlClient(self.server.path)
                        .send(ControlRequest("start", "chat"))
                        .value
                    ),
                    range(8),
                )
            )

        self.assertEqual({item["pid"] for item in results}, {results[0]["pid"]})
        self.assertEqual(self.start_count.read_text().splitlines(), ["started"])

    def test_invalid_edit_blocks_start_but_preserves_status_and_stop(self) -> None:
        started = self.client.send(ControlRequest("start", "chat")).value
        running_pid = started["pid"]
        self.config_path.write_text("not = [valid", encoding="utf-8")

        with self.assertRaisesRegex(ControlDomainError, "configuration is invalid"):
            self.client.send(ControlRequest("start", "chat"))
        status = self.client.send(ControlRequest("status", "chat")).value
        self.assertEqual(status["servers"][0]["pid"], running_pid)
        self.assertEqual(status["servers"][0]["lifecycle"], "ready")
        self.assertIn("config_error", status)
        self.assertEqual(
            self.client.send(ControlRequest("stop", "chat")).value["lifecycle"],
            "stopped",
        )

    def _write_valid_config(self) -> None:
        path = f"{self.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        self.config_path.write_text(
            "\n".join(
                (
                    "schema_version = 1",
                    "[daemon]",
                    "readiness_timeout_seconds = 1",
                    "stop_timeout_seconds = 0.2",
                    "metrics_interval_seconds = 0.05",
                    "[metrics]",
                    "retention_days = 30",
                    "[models.tiny]",
                    'reference = "repo/tiny"',
                    "[servers.chat]",
                    'type = "mlx_lm"',
                    'model = "tiny"',
                    'host = "127.0.0.1"',
                    f"port = {self.port}",
                    "environment = { "
                    f"PATH = {json.dumps(path)}, "
                    f"FAKE_START_COUNT_FILE = {json.dumps(str(self.start_count))} "
                    "}",
                    "",
                )
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]


if __name__ == "__main__":
    unittest.main()
