import json
import io
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import TextIO
from unittest import mock

from mlxctl import cli, daemon as daemon_module
from mlxctl.control import ControlClient, ControlRequest


_FAKE_SERVER = r"""#!/usr/bin/env python3
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--model", required=True)
parser.add_argument("--host", required=True)
parser.add_argument("--port", type=int, required=True)
args, _ = parser.parse_known_args()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = ({"status": "ok"} if self.path == "/health" else
                   {"object": "list", "data": [{"id": args.model}]})
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_):
        pass

ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
"""


class DaemonCliProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config_dir = self.root / "config"
        self.state_dir = self.root / "state"
        self.log_dir = self.root / "logs"
        self.bin_dir = self.root / "bin"
        self.config_dir.mkdir()
        self.bin_dir.mkdir()
        executable = self.bin_dir / "mlx_lm.server"
        executable.write_text(_FAKE_SERVER, encoding="utf-8")
        executable.chmod(0o755)
        self.port = self._free_port()
        self._write_config()
        self.env = os.environ.copy()
        self.env.update(
            {
                "PYTHONPATH": os.fspath(Path(__file__).parents[1] / "src"),
                "PATH": f"{self.bin_dir}{os.pathsep}{self.env.get('PATH', '')}",
                "MLXD_CONFIG_DIR": os.fspath(self.config_dir),
                "MLXD_STATE_DIR": os.fspath(self.state_dir),
                "MLXD_LOG_DIR": os.fspath(self.log_dir),
            }
        )
        self.daemons: list[subprocess.Popen[str]] = []
        self.daemon_logs: dict[int, TextIO] = {}

    def tearDown(self) -> None:
        for daemon in self.daemons:
            if daemon.poll() is None:
                daemon.send_signal(signal.SIGTERM)
                daemon.wait(3)
            self.daemon_logs.pop(daemon.pid).close()
        self.temporary.cleanup()

    def test_actual_daemon_and_cli_start_status_models_stop_then_idle_exit(
        self,
    ) -> None:
        daemon = self._start_daemon(idle_grace="0.35")
        self._wait_for_socket()

        cold = self._cli("status", "--json")
        self.assertEqual(cold.returncode, 0, cold.stderr)
        self.assertEqual(json.loads(cold.stdout)["servers"][0]["lifecycle"], "stopped")
        human_status = self._cli("status")
        self.assertEqual(human_status.returncode, 0, human_status.stderr)
        self.assertIn("chat is stopped", human_status.stdout)
        started = self._cli("start", "chat", "--json")
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(json.loads(started.stdout)["lifecycle"], "ready")
        human_start = self._cli("start", "chat")
        self.assertEqual(human_start.returncode, 0, human_start.stderr)
        self.assertIn("chat is ready", human_start.stdout)
        models = self._cli("models", "chat", "--json")
        self.assertEqual(json.loads(models.stdout)["models"], ["repo/tiny"])
        human_models = self._cli("models", "chat")
        self.assertEqual(human_models.returncode, 0, human_models.stderr)
        self.assertIn("chat advertises", human_models.stdout)
        self.assertIn("repo/tiny", human_models.stdout)
        metrics = self._cli("metrics", "chat", "--json")
        self.assertEqual(metrics.returncode, 0, metrics.stderr)
        metric_summaries = json.loads(metrics.stdout)["summaries"]
        self.assertEqual(metric_summaries[0]["server_id"], "chat")
        self.assertEqual(metric_summaries[0]["request_count"], 0)
        human_metrics = self._cli("metrics", "chat")
        self.assertEqual(human_metrics.returncode, 0, human_metrics.stderr)
        self.assertIn("chat / tiny: 0 requests", human_metrics.stdout)
        missing = self._cli("start", "missing")
        self.assertEqual(missing.returncode, 1)
        self.assertEqual(missing.stdout, "")
        self.assertIn("not configured", missing.stderr)

        time.sleep(0.5)
        self.assertIsNone(
            daemon.poll(), self._daemon_output(daemon) if daemon.poll() else ""
        )
        human_stop = self._cli("stop", "chat")
        self.assertEqual(human_stop.returncode, 0, human_stop.stderr)
        self.assertIn("chat is stopped", human_stop.stdout)
        stopped = self._cli("stop", "chat", "--json")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(json.loads(stopped.stdout)["lifecycle"], "stopped")
        daemon.wait(3)
        self.assertEqual(daemon.returncode, 0, self._daemon_output(daemon))
        self.assertFalse((self.state_dir / "mlxd.sock").exists())
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.state_dir / "metrics.db").stat().st_mode), 0o600
        )

    def test_term_and_int_signals_cause_bounded_socket_cleanup(self) -> None:
        for signal_number in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal_number=signal_number):
                daemon = self._start_daemon(idle_grace="30")
                socket_path = self._wait_for_socket()

                daemon.send_signal(signal_number)
                daemon.wait(3)

                self.assertEqual(daemon.returncode, 0, self._daemon_output(daemon))
                self.assertFalse(socket_path.exists())

    def test_control_activity_extends_idle_grace(self) -> None:
        daemon = self._start_daemon(idle_grace="0.7")
        socket_path = self._wait_for_socket()
        client = ControlClient(socket_path)

        for _ in range(3):
            status = client.send(ControlRequest("status"))
            self.assertEqual(status.value["servers"][0]["lifecycle"], "stopped")
            time.sleep(0.25)
            self.assertIsNone(daemon.poll())

        daemon.wait(3)
        self.assertEqual(daemon.returncode, 0, self._daemon_output(daemon))

    def test_human_output_rejects_a_non_object_protocol_response(self) -> None:
        with self.assertRaisesRegex(
            TypeError, "expected an object response for 'status', got list"
        ):
            cli._print_human("status", [])

    def test_daemon_rejects_nonfinite_idle_grace_values(self) -> None:
        for value in ("nan", "inf", "-inf"):
            with (
                self.subTest(value=value),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                daemon_module._parser().parse_args([f"--idle-grace-seconds={value}"])

    def _start_daemon(self, *, idle_grace: str) -> subprocess.Popen[str]:
        log_stream = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        daemon = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "mlxctl.daemon",
                "--idle-grace-seconds",
                idle_grace,
            ],
            env=self.env,
            text=True,
            stdout=log_stream,
            stderr=log_stream,
        )
        self.daemons.append(daemon)
        self.daemon_logs[daemon.pid] = log_stream
        return daemon

    def _daemon_output(self, daemon: subprocess.Popen[str]) -> str:
        log_stream = self.daemon_logs[daemon.pid]
        log_stream.flush()
        log_stream.seek(0)
        return log_stream.read()

    def _cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "mlxctl.cli", *arguments],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )

    def _wait_for_socket(self) -> Path:
        path = self.state_dir / "mlxd.sock"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if path.exists():
                return path
            daemon = self.daemons[-1]
            if daemon.poll() is not None:
                self.fail(f"daemon exited: {self._daemon_output(daemon)}")
            time.sleep(0.01)
        self.fail("daemon socket was not created")

    def _write_config(self) -> None:
        (self.config_dir / "config.toml").write_text(
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


class LaunchctlActivationTests(unittest.TestCase):
    def test_activator_uses_exact_launchctl_kickstart_target(self) -> None:
        with mock.patch.object(subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")

            cli.activate_daemon(
                launchctl_path="/fake/launchctl", platform="darwin", uid=501
            )

        run.assert_called_once_with(
            ["/fake/launchctl", "kickstart", "gui/501/io.nisavid.mlxd"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def test_live_client_does_not_activate_and_absent_socket_wait_is_bounded(
        self,
    ) -> None:
        live = mock.Mock()
        live.send.return_value = cli.ControlResult({"servers": ()})
        activator = mock.Mock()

        self.assertEqual(
            cli._send_with_activation(
                live,
                cli.ControlRequest("status"),
                activator,
                wait_seconds=0,
            ).value["servers"],
            (),
        )
        activator.assert_not_called()

        absent = mock.Mock()
        absent.send.side_effect = FileNotFoundError(2, "missing")
        started = time.monotonic()
        with self.assertRaisesRegex(cli.ControlDomainError, "did not open"):
            cli._send_with_activation(
                absent,
                cli.ControlRequest("status"),
                activator,
                wait_seconds=0.02,
            )
        self.assertLess(time.monotonic() - started, 0.2)
        activator.assert_called_once_with()

    def test_connection_reset_never_activates_a_second_daemon(self) -> None:
        client = mock.Mock()
        client.send.side_effect = ConnectionResetError(54, "reset")
        activator = mock.Mock()

        with self.assertRaises(ConnectionResetError):
            cli._send_with_activation(
                client,
                cli.ControlRequest("status"),
                activator,
                wait_seconds=0.02,
            )

        activator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
