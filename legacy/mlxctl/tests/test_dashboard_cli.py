import errno
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path

from mlxctl.control import (
    ControlDomainError,
    ControlRequest,
    ControlResult,
    UnixControlServer,
)


class _DashboardControlPlane:
    def __init__(self) -> None:
        self.requests: list[ControlRequest] = []

    def handle(self, request: ControlRequest) -> ControlResult:
        self.requests.append(request)
        if request.command == "status":
            return ControlResult(
                {
                    "servers": (
                        {
                            "server_id": "chat",
                            "model_alias": "tiny",
                            "lifecycle": "ready",
                            "client_endpoint": {"host": "127.0.0.1", "port": 8080},
                            "pid": 4321,
                            "advertised_models": ("repo/tiny",),
                            "error": None,
                        },
                    )
                }
            )
        if request.command == "metrics":
            return ControlResult(
                {
                    "summaries": (
                        {
                            "server_id": "chat",
                            "model_alias": "tiny",
                            "request_count": 10,
                            "success_count": 8,
                            "failure_count": 2,
                            "total_tokens": 42,
                            "average_duration_ms": 12.5,
                            "average_ttft_ms": 3.25,
                            "peak_rss_bytes": 2048,
                            "average_cpu_percent": 25.0,
                        },
                    )
                }
            )
        raise AssertionError(f"unexpected command {request.command}")


class _InteractiveControlPlane:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str | None]] = []
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def handle(self, request: ControlRequest) -> ControlResult:
        with self._lock:
            self.requests.append((request.command, request.server_id))
            if request.command == "status":
                return ControlResult(
                    {
                        "servers": tuple(
                            self._status(server_id) for server_id in ("alpha", "beta")
                        )
                    }
                )
            if request.command == "metrics":
                return ControlResult({"summaries": ()})
            if request.command == "start":
                assert request.server_id is not None
                self._active.add(request.server_id)
                return ControlResult(self._status(request.server_id))
            if request.command == "stop":
                assert request.server_id is not None
                self._active.discard(request.server_id)
                return ControlResult(self._status(request.server_id))
        raise AssertionError(f"unexpected command {request.command}")

    def _status(self, server_id: str) -> dict[str, object]:
        active = server_id in self._active
        return {
            "server_id": server_id,
            "model_alias": "tiny",
            "lifecycle": "ready" if active else "stopped",
            "client_endpoint": {"host": "127.0.0.1", "port": 8080},
            "pid": 4321 if active else None,
            "advertised_models": ("repo/tiny",) if active else (),
            "error": None,
        }


class _DegradedControlPlane:
    def handle(self, request: ControlRequest) -> ControlResult:
        if request.command == "status":
            return ControlResult(
                {
                    "config_error": "models table is invalid",
                    "servers": (
                        {
                            "server_id": "chat",
                            "model_alias": "tiny",
                            "lifecycle": "stopped",
                            "client_endpoint": None,
                            "pid": None,
                            "advertised_models": (),
                            "error": None,
                        },
                    ),
                }
            )
        if request.command == "metrics":
            raise ControlDomainError("metrics_failed", "metrics store unavailable")
        raise AssertionError(f"unexpected command {request.command}")


class _SlowRefreshControlPlane:
    def __init__(self) -> None:
        self.metrics_requests = 0
        self.blocked_metrics = threading.Event()

    def handle(self, request: ControlRequest) -> ControlResult:
        if request.command == "status":
            return ControlResult(
                {
                    "servers": (
                        {
                            "server_id": "chat",
                            "model_alias": "tiny",
                            "lifecycle": "stopped",
                            "client_endpoint": None,
                            "pid": None,
                            "advertised_models": (),
                            "error": None,
                        },
                    )
                }
            )
        if request.command == "metrics":
            self.metrics_requests += 1
            if self.metrics_requests > 1:
                self.blocked_metrics.set()
                time.sleep(0.8)
            return ControlResult({"summaries": ()})
        raise AssertionError(f"unexpected command {request.command}")


class DashboardCliTests(unittest.TestCase):
    def test_non_tty_cli_renders_one_plain_snapshot_without_terminal_codes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = _DashboardControlPlane()
            server = UnixControlServer(root / "state" / "mlxd.sock", control)
            server.start()
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "mlxctl.cli",
                        "dashboard",
                        "--refresh-interval",
                        "0.2",
                    ],
                    env=self._environment(root),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            finally:
                server.close()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertNotIn("\x1b", result.stdout)
        self.assertEqual(
            result.stdout,
            "\n".join(
                (
                    "MLX server dashboard",
                    "> chat / tiny [ready]",
                    "    endpoint http://127.0.0.1:8080 | PID 4321 | models repo/tiny",
                    "    requests 10 | success 8 | failure 2 | tokens 42 | latency 12.5 ms | TTFT 3.2 ms | peak RSS 2.0 KiB | CPU 25.0%",
                    "",
                )
            ),
        )
        self.assertEqual(
            [request.command for request in control.requests],
            ["status", "status", "metrics"],
        )

    def test_non_tty_cli_keeps_status_visible_when_configuration_and_metrics_fail(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = UnixControlServer(
                root / "state" / "mlxd.sock", _DegradedControlPlane()
            )
            server.start()
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "mlxctl.cli", "dashboard"],
                    env=self._environment(root),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            finally:
                server.close()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            "\n".join(
                (
                    "MLX server dashboard",
                    "Configuration error: models table is invalid",
                    "Control error: metrics store unavailable",
                    "> chat / tiny [stopped]",
                    "    endpoint - | PID - | models -",
                    "    requests - | success - | failure - | tokens - | latency - | TTFT - | peak RSS - | CPU -",
                    "",
                )
            ),
        )

    def test_invalid_refresh_interval_is_an_exact_cli_usage_error(self) -> None:
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(value=value):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "mlxctl.cli",
                        "dashboard",
                        "--refresh-interval",
                        value,
                    ],
                    env=self._environment(Path("/not-used")),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )

                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn(
                    "argument --refresh-interval: must be a positive finite number",
                    result.stderr,
                )

    def test_real_pty_navigation_actions_quit_and_resize_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = _InteractiveControlPlane()
            server = UnixControlServer(root / "state" / "mlxd.sock", control)
            server.start()
            master, slave = pty.openpty()
            self._set_size(slave, rows=8, columns=50)
            environment = self._environment(root)
            environment["TERM"] = "xterm"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "mlxctl.cli",
                    "dashboard",
                    "--refresh-interval",
                    "10",
                ],
                env=environment,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
                preexec_fn=self._claim_terminal,
            )
            os.close(slave)
            try:
                self._read_until(master, b"Terminal too small", process)
                status_count = sum(
                    request[0] == "status" for request in control.requests
                )
                os.write(master, b"r")
                deadline = time.monotonic() + 3
                while (
                    sum(request[0] == "status" for request in control.requests)
                    <= status_count
                ):
                    if time.monotonic() >= deadline:
                        self.fail("narrow refresh did not request status")
                    time.sleep(0.01)
                self._read_until(master, b"ed.", process)
                self._set_size(master, rows=24, columns=100)
                process.send_signal(signal.SIGWINCH)
                self._read_until(master, b"MLX server dashboard", process)

                actions_before = self._actions(control)
                os.write(master, b"j")
                time.sleep(0.15)
                self.assertEqual(self._actions(control), actions_before)

                os.write(master, b"s")
                self._wait_for_action(control, ("start", "beta"), process)
                self._read_until(master, b"beta is ready", process)
                os.write(master, b"x")
                self._wait_for_action(control, ("stop", "beta"), process)
                self._read_until(master, b"beta is stopped", process)
                os.write(master, b"q")
                self._wait_for_exit(master, process)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(3)
                os.close(master)
                server.close()

        self.assertEqual(process.returncode, 0)
        self.assertEqual(self._actions(control), [("start", "beta"), ("stop", "beta")])

    def test_periodic_control_latency_does_not_block_q(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = _SlowRefreshControlPlane()
            server = UnixControlServer(root / "state" / "mlxd.sock", control)
            server.start()
            master, slave = pty.openpty()
            self._set_size(slave, rows=24, columns=100)
            environment = self._environment(root)
            environment["TERM"] = "xterm"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "mlxctl.cli",
                    "dashboard",
                    "--refresh-interval",
                    "0.05",
                ],
                env=environment,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
                preexec_fn=self._claim_terminal,
            )
            os.close(slave)
            try:
                self._read_until(master, b"MLX server dashboard", process)
                self.assertTrue(control.blocked_metrics.wait(3))
                started = time.monotonic()
                os.write(master, b"q")
                self._wait_for_exit(master, process)
                elapsed = time.monotonic() - started
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(3)
                os.close(master)
                server.close()

        self.assertEqual(process.returncode, 0)
        self.assertLess(elapsed, 0.4)

    @staticmethod
    def _actions(control: _InteractiveControlPlane) -> list[tuple[str, str | None]]:
        return [
            request for request in control.requests if request[0] in {"start", "stop"}
        ]

    def _wait_for_action(
        self,
        control: _InteractiveControlPlane,
        expected: tuple[str, str],
        process: subprocess.Popen[bytes],
    ) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if expected in self._actions(control):
                return
            if process.poll() is not None:
                self.fail(f"dashboard exited with {process.returncode}")
            time.sleep(0.01)
        self.fail(f"dashboard did not send {expected!r}")

    @staticmethod
    def _set_size(fd: int, *, rows: int, columns: int) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    @staticmethod
    def _claim_terminal() -> None:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    def _read_until(
        self,
        master: int,
        expected: bytes,
        process: subprocess.Popen[bytes],
    ) -> bytes:
        deadline = time.monotonic() + 3
        output = bytearray()
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.05)
            if readable:
                try:
                    output.extend(os.read(master, 65536))
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
            if expected in output:
                return bytes(output)
            if process.poll() is not None:
                self.fail(
                    f"dashboard exited with {process.returncode}: {bytes(output)!r}"
                )
        self.fail(f"terminal did not render {expected!r}: {bytes(output)!r}")

    def _wait_for_exit(self, master: int, process: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.05)
            if readable:
                try:
                    os.read(master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
            if process.poll() is not None:
                return
        self.fail("dashboard did not exit after q")

    @staticmethod
    def _environment(root: Path) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONPATH": os.fspath(Path(__file__).parents[1] / "src"),
                "MLXD_STATE_DIR": os.fspath(root / "state"),
                "MLXD_CONFIG_DIR": os.fspath(root / "config"),
                "MLXD_LOG_DIR": os.fspath(root / "logs"),
            }
        )
        return environment


if __name__ == "__main__":
    unittest.main()
