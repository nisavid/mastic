import http.client
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

try:
    import psutil
except ImportError:
    psutil = None

from mlxctl.config import DaemonSettings, ModelDefinition, ServerDefinition
from mlxctl.metrics import MetricQuery, MetricsEngine
from mlxctl.supervisor import (
    GetModels,
    GetStatus,
    LifecycleState,
    StartServer,
    StopServer,
    Supervisor,
)


_FAKE_SERVER = r"""#!/usr/bin/env python3
import argparse
import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--model", required=True)
parser.add_argument("--host", required=True)
parser.add_argument("--port", type=int, required=True)
args, _ = parser.parse_known_args()
started = time.monotonic()

if os.environ.get("FAKE_IGNORE_TERM") == "1":
    def ignore_term(*_):
        path = os.environ.get("FAKE_TERM_FILE")
        if path:
            with open(path, "a", encoding="utf-8") as stream:
                stream.write("term\n")
    signal.signal(signal.SIGTERM, ignore_term)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        elapsed = time.monotonic() - started
        if self.path == "/health":
            health_file = os.environ.get("FAKE_HEALTH_FILE")
            if elapsed < float(os.environ.get("FAKE_HEALTH_DELAY", "0")) or (health_file and not os.path.exists(health_file)):
                self.reply(503, {"status": "starting"})
            else:
                self.reply(200, {"status": "ok"})
            return
        if self.path == "/v1/models":
            release = os.environ.get("FAKE_READY_FILE")
            delayed = elapsed < float(os.environ.get("FAKE_READY_DELAY", "0"))
            if delayed or (release and not os.path.exists(release)):
                self.reply(503, {"error": "loading"})
            else:
                model_file = os.environ.get("FAKE_MODEL_ID_FILE")
                model_id = open(model_file, encoding="utf-8").read() if model_file else args.model
                self.reply(200, {"object": "list", "data": [{"id": model_id}]})
            return
        if self.path == "/crash":
            self.reply(200, {"crashing": True})
            threading.Thread(target=lambda: (time.sleep(0.02), os._exit(23)), daemon=True).start()
            return
        self.reply(404, {"error": "missing"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.reply(200, {"id": "answer", "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}})

    def reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass

server = ThreadingHTTPServer((args.host, args.port), Handler)
for key in ("FAKE_STARTED_FILE", "FAKE_START_COUNT_FILE"):
    path = os.environ.get(key)
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(str(os.getpid()) + "\n")
if os.environ.get("FAKE_EARLY_CRASH") == "1":
    threading.Thread(target=lambda: (time.sleep(0.03), os._exit(19)), daemon=True).start()
server.serve_forever()
"""


class _IdentityChangingProcess:
    def __init__(self, create_times: tuple[float, ...]) -> None:
        self._create_times = list(create_times)
        self.terminated = False
        self.killed = False
        self.pid = 4242

    def create_time(self) -> float:
        if len(self._create_times) > 1:
            return self._create_times.pop(0)
        return self._create_times[0]

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float) -> None:
        raise psutil.TimeoutExpired(timeout, pid=self.pid)


class SupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        executable = self.bin_dir / "mlx_lm.server"
        executable.write_text(_FAKE_SERVER, encoding="utf-8")
        executable.chmod(0o755)
        self.old_path = os.environ.get("PATH")
        os.environ["PATH"] = f"{self.bin_dir}{os.pathsep}{self.old_path or ''}"
        self.state_dir = self.root / "state"
        self.log_dir = self.root / "logs"
        self.engine = MetricsEngine(self.root / "metrics.sqlite3")
        self.supervisors: list[Supervisor] = []

    def tearDown(self) -> None:
        for supervisor in reversed(self.supervisors):
            supervisor.close()
        if self.old_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = self.old_path
        self.temporary.cleanup()

    def test_start_waits_until_ready_and_proxy_records_metrics(self) -> None:
        supervisor = self._supervisor()

        status = supervisor.apply(StartServer(*self._definitions()))

        self.assertEqual(status.lifecycle, LifecycleState.READY)
        self.assertEqual(status.advertised_models, ("repo/tiny",))
        connection = http.client.HTTPConnection(
            status.client_endpoint.host, status.client_endpoint.port, timeout=1
        )
        connection.request("POST", "/v1/chat/completions", body=b"{}")
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 200)
        summary = self._wait_for(
            lambda: self.engine.query(MetricQuery(server_id="chat")), bool
        )[0]
        self.assertEqual(summary.request_count, 1)
        self.assertEqual(summary.total_tokens, 9)

    def test_live_not_ready_is_observable_as_starting(self) -> None:
        ready_file = self.root / "release-ready"
        supervisor = self._supervisor(readiness_timeout_seconds=2)
        command = StartServer(*self._definitions(FAKE_READY_FILE=str(ready_file)))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(supervisor.apply, command)
            status = self._wait_for(
                lambda: supervisor.apply(GetStatus("chat")),
                lambda item: (
                    item.lifecycle is LifecycleState.STARTING
                    and item.upstream_endpoint is not None
                    and self._get(item.upstream_endpoint, "/health") == 200
                ),
            )
            self.assertIsNotNone(status.pid)
            ready_file.touch()
            self.assertEqual(future.result(2).lifecycle, LifecycleState.READY)

    def test_repeated_and_concurrent_start_uses_one_process(self) -> None:
        count_file = self.root / "start-count"
        supervisor = self._supervisor()
        command = StartServer(*self._definitions(FAKE_START_COUNT_FILE=str(count_file)))

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = tuple(pool.map(supervisor.apply, (command,) * 8))
        statuses += (supervisor.apply(command),)

        self.assertEqual({item.pid for item in statuses}, {statuses[0].pid})
        self.assertEqual(
            count_file.read_text(encoding="utf-8").splitlines(), [str(statuses[0].pid)]
        )

    def test_readiness_timeout_and_early_exit_fail_and_clean_up(self) -> None:
        for environment, expected in (
            ({"FAKE_READY_FILE": str(self.root / "never")}, "readiness timed out"),
            (
                {"FAKE_EARLY_CRASH": "1", "FAKE_HEALTH_DELAY": "0.1"},
                "exited with status 19",
            ),
        ):
            with self.subTest(environment=environment):
                supervisor = self._supervisor(readiness_timeout_seconds=0.2)
                status = supervisor.apply(
                    StartServer(*self._definitions(**environment))
                )
                self.assertEqual(status.lifecycle, LifecycleState.FAILED)
                self.assertIn(expected, status.error)
                self.assertIsNone(status.pid)
                self.assertIsNone(status.upstream_endpoint)
                self.assertEqual(
                    json.loads((self.state_dir / "runtime.json").read_text()), {}
                )
                supervisor.close()
                self.supervisors.remove(supervisor)

    def test_stop_is_graceful_idempotent_and_forces_kill_when_needed(self) -> None:
        for environment in ({}, {"FAKE_IGNORE_TERM": "1"}):
            with self.subTest(environment=environment):
                supervisor = self._supervisor(stop_timeout_seconds=0.15)
                ready = supervisor.apply(StartServer(*self._definitions(**environment)))
                stopped = supervisor.apply(StopServer("chat"))
                again = supervisor.apply(StopServer("chat"))
                self.assertEqual(stopped.lifecycle, LifecycleState.STOPPED)
                self.assertEqual(again, stopped)
                self.assertFalse(self._pid_exists(ready.pid))
                supervisor.close()
                self.supervisors.remove(supervisor)

    def test_unexpected_exit_becomes_failed_and_proxy_closes(self) -> None:
        supervisor = self._supervisor(metrics_interval_seconds=0.03)
        ready = supervisor.apply(StartServer(*self._definitions()))

        self.assertEqual(self._get(ready.client_endpoint, "/crash"), 200)
        failed = self._wait_for(
            lambda: supervisor.apply(GetStatus("chat")),
            lambda item: item.lifecycle is LifecycleState.FAILED,
        )

        self.assertIn("exited with status 23", failed.error)
        with self.assertRaises(OSError):
            self._get(ready.client_endpoint, "/health")

    def test_liveness_loss_is_unhealthy_and_recovers(self) -> None:
        health_file = self.root / "healthy"
        health_file.touch()
        supervisor = self._supervisor(metrics_interval_seconds=0.03)
        supervisor.apply(
            StartServer(*self._definitions(FAKE_HEALTH_FILE=str(health_file)))
        )

        health_file.unlink()
        unhealthy = self._wait_for(
            lambda: supervisor.apply(GetStatus("chat")),
            lambda item: item.lifecycle is LifecycleState.UNHEALTHY,
        )
        self.assertEqual(unhealthy.advertised_models, ("repo/tiny",))
        health_file.touch()
        ready = self._wait_for(
            lambda: supervisor.apply(GetStatus("chat")),
            lambda item: item.lifecycle is LifecycleState.READY,
        )
        self.assertEqual(ready.advertised_models, ("repo/tiny",))

    def test_monitor_observes_liveness_and_readiness_independently(self) -> None:
        health_file = self.root / "healthy"
        ready_file = self.root / "models-ready"
        model_file = self.root / "advertised-model"
        health_file.touch()
        ready_file.touch()
        model_file.write_text("repo/initial", encoding="utf-8")
        supervisor = self._supervisor(metrics_interval_seconds=0.03)
        supervisor.apply(
            StartServer(
                *self._definitions(
                    FAKE_HEALTH_FILE=str(health_file),
                    FAKE_READY_FILE=str(ready_file),
                    FAKE_MODEL_ID_FILE=str(model_file),
                )
            )
        )

        health_file.unlink()
        model_file.write_text("repo/models-still-ready", encoding="utf-8")
        unhealthy = self._wait_for(
            lambda: supervisor.apply(GetStatus("chat")),
            lambda item: (
                item.lifecycle is LifecycleState.UNHEALTHY
                and item.advertised_models == ("repo/models-still-ready",)
            ),
        )
        self.assertEqual(unhealthy.advertised_models, ("repo/models-still-ready",))

        health_file.touch()
        ready_file.unlink()
        still_unhealthy = self._wait_for(
            lambda: supervisor.apply(GetStatus("chat")),
            lambda item: item.lifecycle is LifecycleState.UNHEALTHY,
        )
        self.assertEqual(
            still_unhealthy.advertised_models, ("repo/models-still-ready",)
        )

    @unittest.skipIf(psutil is None, "psutil dependency is not installed")
    def test_monitor_persists_process_samples_and_get_models_uses_status(self) -> None:
        supervisor = self._supervisor(metrics_interval_seconds=0.03)
        supervisor.apply(StartServer(*self._definitions()))

        summary = self._wait_for(
            lambda: self.engine.query(MetricQuery(server_id="chat")),
            lambda items: bool(items) and items[0].peak_rss_bytes is not None,
        )[0]

        self.assertGreater(summary.peak_rss_bytes, 0)
        self.assertEqual(supervisor.apply(GetModels("chat")), ("repo/tiny",))

    def test_close_is_bounded_and_secures_runtime_files(self) -> None:
        supervisor = self._supervisor(stop_timeout_seconds=0.15)
        ready = supervisor.apply(StartServer(*self._definitions(FAKE_IGNORE_TERM="1")))

        started = time.monotonic()
        supervisor.close()
        duration = time.monotonic() - started

        self.assertLess(duration, 0.8)
        self.assertFalse(self._pid_exists(ready.pid))
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.log_dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.state_dir / "runtime.json").stat().st_mode), 0o600
        )
        self.assertEqual(
            stat.S_IMODE((self.log_dir / "chat.log").stat().st_mode), 0o600
        )
        self.supervisors.remove(supervisor)

    def test_close_cancels_an_in_progress_start_boundedly(self) -> None:
        release = self.root / "never-ready"
        supervisor = self._supervisor(
            readiness_timeout_seconds=5, stop_timeout_seconds=0.15
        )
        command = StartServer(
            *self._definitions(FAKE_READY_FILE=str(release), FAKE_IGNORE_TERM="1")
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(supervisor.apply, command)
            starting = self._wait_for(
                lambda: supervisor.apply(GetStatus("chat")),
                lambda item: (
                    item.lifecycle is LifecycleState.STARTING and item.pid is not None
                ),
            )
            started = time.monotonic()
            supervisor.close()
            self.assertLess(time.monotonic() - started, 0.8)
            self.assertEqual(future.result(1).lifecycle, LifecycleState.STOPPED)
            self.assertFalse(self._pid_exists(starting.pid))
        self.supervisors.remove(supervisor)

    def test_concurrent_close_waits_for_one_complete_stop_sequence(self) -> None:
        term_file = self.root / "term-count"
        supervisor = self._supervisor(stop_timeout_seconds=0.2)
        ready = supervisor.apply(
            StartServer(
                *self._definitions(FAKE_IGNORE_TERM="1", FAKE_TERM_FILE=str(term_file))
            )
        )
        release = threading.Event()

        def close() -> float:
            release.wait()
            supervisor.close()
            return time.monotonic()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(close)
            second = pool.submit(close)
            release.set()
            time.sleep(0.05)
            self.assertFalse(first.done())
            self.assertFalse(second.done())
            completed = (first.result(1), second.result(1))

        self.assertLess(abs(completed[0] - completed[1]), 0.05)
        self.assertEqual(term_file.read_text(encoding="utf-8").splitlines(), ["term"])
        self.assertFalse(self._pid_exists(ready.pid))
        self.supervisors.remove(supervisor)

    def test_spawn_and_proxy_failures_become_clean_failed_statuses(self) -> None:
        executable = self.bin_dir / "mlx_lm.server"
        executable.rename(self.bin_dir / "mlx_lm.server.disabled")
        supervisor = self._supervisor()
        spawn_failed = supervisor.apply(
            StartServer(*self._definitions(PATH=str(self.bin_dir)))
        )
        self.assertEqual(spawn_failed.lifecycle, LifecycleState.FAILED)
        self.assertIn("start failed", spawn_failed.error)
        self.assertIsNone(spawn_failed.pid)
        supervisor.close()
        self.supervisors.remove(supervisor)
        executable = self.bin_dir / "mlx_lm.server.disabled"
        executable.rename(self.bin_dir / "mlx_lm.server")

        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            server, model = self._definitions()
            server = ServerDefinition(
                server.name,
                server.type,
                server.model,
                server.host,
                occupied.getsockname()[1],
                server.environment,
                server.options,
            )
            supervisor = self._supervisor()
            proxy_failed = supervisor.apply(StartServer(server, model))
        self.assertEqual(proxy_failed.lifecycle, LifecycleState.FAILED)
        self.assertIn("Address already in use", proxy_failed.error)
        self.assertIsNone(proxy_failed.pid)

    @unittest.skipIf(psutil is None, "psutil dependency is not installed")
    def test_startup_recovers_matching_orphan_but_ignores_pid_reuse_mismatch(
        self,
    ) -> None:
        orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(self._terminate, orphan)
        self.state_dir.mkdir(mode=0o700)
        state_file = self.state_dir / "runtime.json"
        state_file.write_text(
            json.dumps(
                {
                    "orphan": {
                        "pid": orphan.pid,
                        "create_time": self._create_time(orphan.pid),
                    }
                }
            ),
            encoding="utf-8",
        )
        state_file.chmod(0o600)

        supervisor = self._supervisor(stop_timeout_seconds=0.15)

        orphan.wait(1)
        self.assertEqual(json.loads(state_file.read_text()), {})
        supervisor.close()
        self.supervisors.remove(supervisor)
        state_file.write_text(
            json.dumps(
                {
                    "reused": {
                        "pid": os.getpid(),
                        "create_time": self._create_time(os.getpid()) + 100,
                    }
                }
            ),
            encoding="utf-8",
        )
        supervisor = self._supervisor()
        self.assertTrue(self._pid_exists(os.getpid()))
        self.assertEqual(json.loads(state_file.read_text()), {})

    @unittest.skipIf(psutil is None, "psutil dependency is not installed")
    def test_orphan_identity_change_before_term_is_never_signaled(self) -> None:
        fake_process = _IdentityChangingProcess((10.0, 20.0))
        self._write_runtime_identity(4242, 10.0)

        with (
            mock.patch.object(psutil, "Process", return_value=fake_process),
            mock.patch.object(os, "kill") as kill,
        ):
            supervisor = self._supervisor(stop_timeout_seconds=0.001)

        self.assertFalse(fake_process.terminated)
        self.assertFalse(fake_process.killed)
        kill.assert_not_called()
        supervisor.close()

    @unittest.skipIf(psutil is None, "psutil dependency is not installed")
    def test_orphan_identity_change_between_term_and_kill_is_not_killed(self) -> None:
        fake_process = _IdentityChangingProcess((10.0, 10.0, 10.0, 20.0))
        self._write_runtime_identity(4242, 10.0)

        with (
            mock.patch.object(psutil, "Process", return_value=fake_process),
            mock.patch.object(os, "kill") as kill,
        ):
            supervisor = self._supervisor(stop_timeout_seconds=0.001)

        self.assertTrue(fake_process.terminated)
        self.assertFalse(fake_process.killed)
        kill.assert_not_called()
        supervisor.close()

    @unittest.skipIf(psutil is None, "psutil dependency is not installed")
    def test_child_identity_change_before_term_is_never_signaled(self) -> None:
        fake_process = _IdentityChangingProcess((10.0, 20.0))
        term_file = self.root / "term-count"
        supervisor = self._supervisor(metrics_interval_seconds=10)

        with mock.patch.object(psutil, "Process", return_value=fake_process):
            ready = supervisor.apply(
                StartServer(
                    *self._definitions(
                        FAKE_IGNORE_TERM="1", FAKE_TERM_FILE=str(term_file)
                    )
                )
            )
            supervisor.apply(StopServer("chat"))

        self.assertTrue(self._pid_exists(ready.pid))
        self.assertFalse(term_file.exists())
        self._kill_and_reap(supervisor, ready.pid)

    @unittest.skipIf(psutil is None, "psutil dependency is not installed")
    def test_child_identity_change_between_term_and_kill_is_not_killed(self) -> None:
        fake_process = _IdentityChangingProcess((10.0, 10.0, 10.0, 20.0))
        term_file = self.root / "term-count"
        supervisor = self._supervisor(
            metrics_interval_seconds=10, stop_timeout_seconds=0.05
        )

        with mock.patch.object(psutil, "Process", return_value=fake_process):
            ready = supervisor.apply(
                StartServer(
                    *self._definitions(
                        FAKE_IGNORE_TERM="1", FAKE_TERM_FILE=str(term_file)
                    )
                )
            )
            supervisor.apply(StopServer("chat"))

        self.assertTrue(self._pid_exists(ready.pid))
        self.assertEqual(term_file.read_text(encoding="utf-8").splitlines(), ["term"])
        self._kill_and_reap(supervisor, ready.pid)

    def test_recovery_without_psutil_never_signals_a_recycled_pid(self) -> None:
        self._write_runtime_identity(4242, 123.0)

        with (
            mock.patch("mlxctl.supervisor.psutil", None),
            mock.patch.object(time, "time", return_value=123.0),
            mock.patch.object(os, "kill") as kill,
        ):
            supervisor = self._supervisor()

        kill.assert_not_called()
        self.assertEqual(json.loads((self.state_dir / "runtime.json").read_text()), {})
        supervisor.close()

    def _supervisor(self, **settings: float) -> Supervisor:
        values = {
            "readiness_timeout_seconds": 1,
            "stop_timeout_seconds": 0.25,
            "metrics_interval_seconds": 0.05,
        }
        values.update(settings)
        supervisor = Supervisor(
            DaemonSettings(**values), self.engine, self.state_dir, self.log_dir
        )
        self.supervisors.append(supervisor)
        return supervisor

    def _write_runtime_identity(self, pid: int, create_time: float) -> None:
        self.state_dir.mkdir(mode=0o700, exist_ok=True)
        state_file = self.state_dir / "runtime.json"
        state_file.write_text(
            json.dumps({"orphan": {"pid": pid, "create_time": create_time}}),
            encoding="utf-8",
        )
        state_file.chmod(0o600)

    def _definitions(self, **environment: str):
        model = ModelDefinition("tiny", "repo/tiny")
        server = ServerDefinition(
            "chat",
            "mlx_lm",
            "tiny",
            "127.0.0.1",
            self._free_port(),
            environment,
            {},
        )
        return server, model

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    @staticmethod
    def _get(endpoint, path: str) -> int:
        connection = http.client.HTTPConnection(
            endpoint.host, endpoint.port, timeout=0.2
        )
        connection.request("GET", path)
        response = connection.getresponse()
        response.read()
        connection.close()
        return response.status

    def _wait_for(self, read, predicate):
        deadline = time.monotonic() + 2
        last = None
        while time.monotonic() < deadline:
            try:
                last = read()
                if predicate(last):
                    return last
            except (OSError, IndexError):
                pass
            time.sleep(0.01)
        self.fail(f"condition not met; last value: {last!r}")

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.kill()
            process.wait()

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    @staticmethod
    def _create_time(pid: int) -> float:
        return psutil.Process(pid).create_time()

    @staticmethod
    def _kill_and_reap(supervisor: Supervisor, pid: int) -> None:
        os.kill(pid, signal.SIGKILL)
        supervisor._runs["chat"].process.wait()  # test cleanup after PID simulation


if __name__ == "__main__":
    unittest.main()
