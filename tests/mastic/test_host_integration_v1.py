from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from mastic.application.dispatch import OperationResult
from mastic.infrastructure.host_integration import (
    LaunchdSupervisorActivator,
    LocalSnapshotProvider,
    PrivateLogReader,
    StateMetricsSource,
)


class _Launchd:
    def __init__(self, *, registered: bool, running: bool) -> None:
        self.registered = registered
        self.running = running
        self.calls: list[str] = []

    def status(self):
        self.calls.append("status")
        return type(
            "Status",
            (),
            {"registered": self.registered, "running": self.running},
        )()

    def register(self):
        self.calls.append("register")
        self.registered = True

    def kickstart(self):
        self.calls.append("kickstart")
        self.running = True


class _Dispatcher:
    def __init__(self, value) -> None:
        self.value = value
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return OperationResult(request.name, self.value)


class _State:
    def metrics(self, kind=None):
        items = (
            {"kind": "request", "scope": "service", "resource": "coding"},
            {"kind": "pressure", "scope": "supervisor"},
        )
        return tuple(item for item in items if kind is None or item["kind"] == kind)


class HostIntegrationTests(unittest.TestCase):
    def test_activator_registers_and_starts_only_when_explicitly_called(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "masticd.sock"
            launchd = _Launchd(registered=False, running=False)

            def sleep(_seconds):
                socket_path.touch()

            activator = LaunchdSupervisorActivator(
                launchd,
                socket_path,
                socket_ready=lambda path: path.exists(),
                monotonic=iter((0.0, 0.1)).__next__,
                sleep=sleep,
            )

            self.assertEqual(launchd.calls, [])
            activator.activate()

            self.assertEqual(launchd.calls, ["status", "register", "kickstart"])

    def test_activator_does_not_reregister_an_existing_job(self) -> None:
        launchd = _Launchd(registered=True, running=True)
        activator = LaunchdSupervisorActivator(
            launchd,
            Path("/unused"),
            socket_ready=lambda _path: True,
        )

        activator.activate()

        self.assertEqual(launchd.calls, ["status"])

    def test_snapshot_provider_uses_real_status_without_mutation(self) -> None:
        dispatcher = _Dispatcher(
            {
                "supervisor": {"state": "running", "pressure": "warning"},
                "gateway": {"state": "ready", "host": "127.0.0.1", "port": 8766},
                "services": [
                    {
                        "name": "coding",
                        "desired": {
                            "model_alias": "qwen",
                            "runtime_installation": "optiq@0.3.3",
                            "pinned": True,
                        },
                        "run": {"state": "ready"},
                    }
                ],
                "operations": [
                    {"id": "op-1", "status": "running"},
                    {"id": "op-2", "status": "ready"},
                ],
            }
        )

        snapshot = LocalSnapshotProvider(dispatcher).snapshot()

        self.assertEqual(dispatcher.requests[0].name, "status")
        self.assertEqual(snapshot.supervisor, "running")
        self.assertIn("127.0.0.1:8766", snapshot.gateway)
        self.assertEqual(snapshot.pressure, "warning")
        self.assertEqual(snapshot.active_operations, 1)
        self.assertEqual(snapshot.services[0].runtime, "optiq@0.3.3")

    def test_private_log_reader_bounds_files_and_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "coding.log"
            log.write_text("one\ntwo\nthree\n")
            log.chmod(0o600)
            (root / "unsafe.log").symlink_to(log)

            rows = PrivateLogReader(root, max_lines=2).read("service", "coding")

            self.assertEqual([row["message"] for row in rows], ["two", "three"])
            self.assertTrue(stat.S_ISREG(log.stat().st_mode))

    def test_metrics_adapter_filters_scope_and_resource(self) -> None:
        metrics = StateMetricsSource(_State())

        rows = metrics.query("service", "coding")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "request")


if __name__ == "__main__":
    unittest.main()
