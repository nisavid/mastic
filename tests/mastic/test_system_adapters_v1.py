from __future__ import annotations

import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import psutil
import tomlkit
import mastic.infrastructure.system_adapters as system_adapters

from mastic.application.config_schema import validate_config
from mastic.domain.admission import PressureLevel
from mastic.infrastructure.system_adapters import (
    ConfigDesiredState,
    ExactRuntimeLaunchSupply,
    MacOSMemoryPressure,
    MacOSProcessLauncher,
    MacOSProcessProbe,
    PendingSubprocessManagedProcess,
    SystemClock,
)
from mastic.infrastructure.model_supply import (
    ModelInstallation as SuppliedModelInstallation,
    VerificationResult,
)
from mastic.infrastructure.model_supply import ModelProvenance as SuppliedProvenance
from mastic.infrastructure.model_supply import ModelRevision as SuppliedRevision
from mastic.infrastructure.runtime_supply import (
    RuntimeCatalogue,
    RuntimeInstallation as SuppliedRuntimeInstallation,
    RuntimeLaunchBuilder,
    UnsupportedLaunchOption,
)
from mastic.infrastructure.supervisor_v1 import CapabilityValidationError


_REVISION = "70a3aa32c7feef511182bf16aa332f37e8d82014"


def _config(
    *,
    service_name: str = "coding",
    runtime_root: Path = Path("/opt/mastic/runtimes/optiq@0.2.18"),
    runtime_launcher: tuple[str, ...] = (
        "/opt/mastic/runtimes/optiq@0.2.18/bin/optiq",
        "serve",
    ),
    runtime_capabilities: frozenset[str] = frozenset(
        {"model", "host", "port", "kv_config", "mtp"}
    ),
    trust_remote_code: bool = False,
):
    launcher = ", ".join(f'"{item}"' for item in runtime_launcher)
    capabilities = ", ".join(f'"{item}"' for item in sorted(runtime_capabilities))
    remote_code = "trust_remote_code = true" if trust_remote_code else ""
    return validate_config(
        tomlkit.parse(
            f"""
schema_version = 1

[runtimes."optiq@0.2.18"]
definition = "optiq"
version = "0.2.18"
provenance = "tested"
root = "{runtime_root}"
launcher = [{launcher}]
capabilities = [{capabilities}]

[models.qwen-exact]
repository = "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit"
revision = "{_REVISION}"

[aliases.qwen-optiq]
installation = "qwen-exact"

[services.{service_name}]
model_alias = "qwen-optiq"
runtime = "optiq@0.2.18"
route = "{service_name}"

[services.{service_name}.options]
kv_config = "kv_config.json"
mtp = true
{remote_code}
"""
        )
    )


class _FakePopen:
    pid = 4123

    def poll(self):
        return None

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout):
        return 0


class _FakePsutilProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.running = True
        self.terminate_calls = 0
        self.kill_calls = 0
        self.created_at = 1_721_234_567.125
        self.status_value = "running"

    def is_running(self):
        return self.running

    def status(self):
        return self.status_value

    def create_time(self):
        return self.created_at

    def wait(self, timeout):
        if self.running:
            import psutil

            raise psutil.TimeoutExpired(timeout, self.pid)
        return 0

    def terminate(self):
        self.terminate_calls += 1
        self.running = False

    def kill(self):
        self.kill_calls += 1
        self.running = False


class _AllowingModelSecurity:
    def __init__(self, *, risks=()) -> None:
        self.risks = list(risks)
        self.calls = []

    def require(self, repository, revision):
        self.calls.append(("require", repository, revision))
        return {"overridable_risks": self.risks}

    def record_cached_verification(self, repository, revision, verification):
        self.calls.append(("verify", repository, revision, verification.status))
        if verification.status not in {"complete", "verified"} or verification.issues:
            raise ValueError("integrity mismatch")
        return {"overridable_risks": self.risks}


def _verified_model(_model):
    return VerificationResult("complete", "cache-integrity", ())


def _full_nonblocking_pipe() -> tuple[int, int]:
    read_descriptor, write_descriptor = os.pipe()
    os.set_blocking(write_descriptor, False)
    try:
        while True:
            os.write(write_descriptor, b"x" * 4096)
    except BlockingIOError:
        return read_descriptor, write_descriptor


class ProcessLauncherTests(unittest.TestCase):
    def test_launch_rejects_embedded_nul_before_spawning_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spawn_calls = 0

            def popen(*_args, **_kwargs):
                nonlocal spawn_calls
                spawn_calls += 1
                return _FakePopen()

            launcher = MacOSProcessLauncher(
                log_dir=Path(directory) / "logs",
                base_environment={"PATH": "/usr/bin"},
                popen=popen,
            )

            invalid_launches = (
                (("/runtime/bin/optiq", "serve\0unexpected"), {}),
                (("/runtime/bin/optiq",), {"PATH": "/usr/bin\0/untrusted"}),
                (("/runtime/bin/optiq",), {"PATH\0UNTRUSTED": "/usr/bin"}),
            )
            for argv, environment in invalid_launches:
                with (
                    self.subTest(argv=argv, environment=environment),
                    self.assertRaisesRegex(ValueError, "embedded NUL"),
                ):
                    launcher.launch(argv, environment)

            self.assertEqual(spawn_calls, 0)

    def test_aborting_uncommitted_launch_never_execs_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "runtime-started"
            launcher = MacOSProcessLauncher(log_dir=root / "logs")
            process = launcher.launch(
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                    str(marker),
                ),
                {"MASTIC_SERVICE_NAME": "coding"},
            )

            process.abort()
            process.wait(2)

            self.assertFalse(marker.exists())

    def test_failed_spawn_closes_the_parent_pipe_descriptor_once(self) -> None:
        class IdleThread:
            def __init__(self, **_kwargs) -> None:
                pass

            def start(self) -> None:
                pass

        closed: list[int] = []
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                system_adapters.os,
                "pipe",
                side_effect=((31, 32), (33, 34)),
            ),
            patch.object(system_adapters.os, "close", side_effect=closed.append),
            patch.object(system_adapters.threading, "Thread", IdleThread),
        ):
            launcher = MacOSProcessLauncher(
                log_dir=Path(directory),
                popen=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    OSError("spawn failed")
                ),
            )

            with self.assertRaisesRegex(OSError, "spawn failed"):
                launcher.launch(("/runtime/bin/optiq",), {})

        self.assertEqual(closed.count(32), 1)
        self.assertEqual(closed.count(33), 1)
        self.assertEqual(closed.count(34), 1)

    def test_commit_pipe_failure_closes_control_descriptor_once(self) -> None:
        class IdleThread:
            def __init__(self, **_kwargs) -> None:
                pass

            def start(self) -> None:
                pass

        class WritableSelector:
            def register(self, *_args) -> None:
                pass

            def select(self, _timeout):
                return ((object(), object()),)

            def close(self) -> None:
                pass

        closed: list[int] = []
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                system_adapters.os,
                "pipe",
                side_effect=((31, 32), (33, 34)),
            ),
            patch.object(system_adapters.os, "close", side_effect=closed.append),
            patch.object(
                system_adapters.os, "write", side_effect=BrokenPipeError("closed")
            ),
            patch.object(system_adapters.os, "set_blocking"),
            patch.object(
                system_adapters.selectors,
                "DefaultSelector",
                return_value=WritableSelector(),
            ),
            patch.object(system_adapters.threading, "Thread", IdleThread),
        ):
            launcher = MacOSProcessLauncher(
                log_dir=Path(directory),
                popen=lambda *_args, **_kwargs: _FakePopen(),
            )
            process = launcher.launch(("/runtime/bin/optiq",), {})

            with self.assertRaises(BrokenPipeError):
                process.commit()
            process.abort()

        self.assertEqual(closed.count(34), 1)

    def test_commit_deadline_fails_closed_when_the_gate_does_not_read(self) -> None:
        read_descriptor, write_descriptor = _full_nonblocking_pipe()
        process = PendingSubprocessManagedProcess(
            _FakePopen(),
            threading.Thread(),
            write_descriptor,
            b"launch-frame",
            commit_timeout_seconds=0.05,
        )
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(TimeoutError, "launch gate"):
                process.commit()
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            process.abort()
            os.close(read_descriptor)

    def test_terminate_signals_while_a_commit_is_waiting_for_the_gate(self) -> None:
        class ObservablePopen(_FakePopen):
            def __init__(self) -> None:
                self.terminated = threading.Event()

            def terminate(self) -> None:
                self.terminated.set()

        read_descriptor, write_descriptor = _full_nonblocking_pipe()
        popen = ObservablePopen()
        process = PendingSubprocessManagedProcess(
            popen,
            threading.Thread(),
            write_descriptor,
            b"launch-frame",
            commit_timeout_seconds=2.0,
        )
        commit_errors: list[Exception] = []

        def commit() -> None:
            try:
                process.commit()
            except Exception as error:
                commit_errors.append(error)

        commit_thread = threading.Thread(target=commit)
        commit_thread.start()
        time.sleep(0.05)
        started = time.monotonic()
        try:
            process.terminate()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(popen.terminated.is_set())
            commit_thread.join(0.5)
            self.assertFalse(commit_thread.is_alive())
            self.assertEqual(len(commit_errors), 1)
        finally:
            process.abort()
            os.close(read_descriptor)

    def test_log_storage_failure_does_not_stop_pipe_drain(self) -> None:
        read_descriptor, write_descriptor = os.pipe()
        os.write(write_descriptor, b"first")
        os.write(write_descriptor, b"second")
        os.close(write_descriptor)

        class FailingWriter:
            calls = 0

            def write(self, payload: bytes) -> None:
                del payload
                self.calls += 1
                raise OSError("disk full")

        writer = FailingWriter()
        system_adapters._pump_process_log(read_descriptor, writer)

        self.assertEqual(writer.calls, 1)

    def test_launch_uses_exact_argv_allowlisted_environment_and_private_service_log(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

            def popen(argv, **kwargs):
                calls.append((tuple(argv), dict(kwargs)))
                return _FakePopen()

            log_dir = Path(directory) / "logs"
            launcher = MacOSProcessLauncher(
                log_dir=log_dir,
                base_environment={
                    "PATH": "/usr/bin",
                    "HOME": "/Users/example",
                    "GITHUB_TOKEN": "must-not-leak",
                },
                popen=popen,
            )

            process = launcher.launch(
                ("/runtime/bin/optiq", "serve", "--port", "49152"),
                {"MASTIC_SERVICE_NAME": "coding", "HF_HUB_OFFLINE": "1"},
            )

            self.assertEqual(process.pid, 4123)
            argv, options = calls[0]
            self.assertEqual(argv[0], sys.executable)
            self.assertEqual(Path(argv[1]).name, "_launch_gate.py")
            self.assertEqual(len(argv), 3)
            self.assertNotIn("/runtime/bin/optiq", argv)
            self.assertIs(options["shell"], False)
            self.assertIs(options["start_new_session"], True)
            self.assertEqual(options["pass_fds"], (int(argv[2]),))
            self.assertEqual(
                options["env"],
                {
                    "PATH": "/usr/bin",
                    "HOME": "/Users/example",
                    "MASTIC_SERVICE_NAME": "coding",
                    "HF_HUB_OFFLINE": "1",
                },
            )
            log = log_dir / "coding.log"
            self.assertTrue(log.is_file())
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(log_dir.stat().st_mode), 0o700)
            process.abort()

            with self.assertRaisesRegex(ValueError, "environment variable"):
                launcher.launch(("/runtime/bin/optiq",), {"API_TOKEN": "secret"})

    def test_commit_execs_exact_runtime_in_place_with_allowlisted_environment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "runtime.json"
            launcher = MacOSProcessLauncher(
                log_dir=root / "logs",
                base_environment={
                    "HOME": str(root),
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "GITHUB_TOKEN": "must-not-leak",
                },
            )
            program = (
                "import json, os, pathlib, sys; "
                "pathlib.Path(sys.argv[1]).write_text(json.dumps("
                "{'pid': os.getpid(), 'home': os.environ.get('HOME'), "
                "'service': os.environ.get('MASTIC_SERVICE_NAME'), "
                "'secret': os.environ.get('GITHUB_TOKEN')})); "
                "print('runtime-output')"
            )
            process = launcher.launch(
                (sys.executable, "-c", program, str(marker)),
                {"MASTIC_SERVICE_NAME": "coding"},
            )
            gate_pid = process.pid

            process.commit()
            self.assertEqual(process.wait(2), 0)

            observed = json.loads(marker.read_text())
            self.assertEqual(observed["pid"], gate_pid)
            self.assertEqual(observed["home"], str(root))
            self.assertEqual(observed["service"], "coding")
            self.assertIsNone(observed["secret"])
            self.assertEqual((root / "logs/coding.log").read_text(), "runtime-output\n")

    def test_service_logs_rotate_with_bounded_size_and_retention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:

            def popen(_argv, **kwargs):
                import os

                os.write(kwargs["stdout"], b"0123456789abcdef")
                return _FakePopen()

            log_dir = Path(directory) / "logs"
            launcher = MacOSProcessLauncher(
                log_dir=log_dir,
                base_environment={"PATH": "/usr/bin"},
                max_log_bytes=8,
                retained_log_files=2,
                popen=popen,
            )

            for _ in range(3):
                process = launcher.launch(
                    ("/runtime/bin/optiq",), {"MASTIC_SERVICE_NAME": "coding"}
                )
                process.abort()
                process.wait(1)

            files = tuple(sorted(log_dir.glob("coding.log*")))
            self.assertLessEqual(len(files), 3)
            self.assertTrue(files)
            self.assertTrue(all(path.stat().st_size <= 8 for path in files))

    def test_launch_refuses_a_symlink_at_the_private_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_dir = root / "logs"
            log_dir.mkdir()
            target = root / "outside"
            target.write_text("preserve")
            (log_dir / "coding.log").symlink_to(target)
            launcher = MacOSProcessLauncher(
                log_dir=log_dir,
                popen=lambda *args, **kwargs: _FakePopen(),
            )

            with self.assertRaises(OSError):
                launcher.launch(
                    ("/runtime/bin/optiq",), {"MASTIC_SERVICE_NAME": "coding"}
                )

            self.assertEqual(target.read_text(), "preserve")

    def test_port_allocation_is_literal_loopback_only_and_attach_is_bounded(
        self,
    ) -> None:
        attached = _FakePsutilProcess(8123)
        launcher = MacOSProcessLauncher(
            log_dir=Path("/unused"),
            process_factory=lambda pid: attached,
        )

        port = launcher.allocate_loopback_port("127.0.0.1")

        self.assertGreater(port, 0)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", port))
        with self.assertRaisesRegex(ValueError, "literal loopback"):
            launcher.allocate_loopback_port("localhost")
        with self.assertRaisesRegex(ValueError, "literal loopback"):
            launcher.allocate_loopback_port("0.0.0.0")

        process = launcher.attach(8123)
        self.assertIsNotNone(process)
        self.assertEqual(process.pid, 8123)
        self.assertIsNone(process.poll())
        process.terminate()
        self.assertEqual(process.poll(), 0)

    def test_attached_process_poll_treats_a_zombie_as_stopped(self) -> None:
        attached = _FakePsutilProcess(8123)
        launcher = MacOSProcessLauncher(
            log_dir=Path("/unused"),
            process_factory=lambda pid: attached,
        )
        process = launcher.attach(8123)
        self.assertIsNotNone(process)

        attached.status_value = "zombie"

        self.assertEqual(process.poll(), 0)

    def test_attached_process_control_is_idempotent_after_disappearance(self) -> None:
        class DisappearedProcess(_FakePsutilProcess):
            def terminate(self):
                raise psutil.NoSuchProcess(self.pid)

            def kill(self):
                raise psutil.NoSuchProcess(self.pid)

            def wait(self, timeout):
                raise psutil.NoSuchProcess(self.pid)

        launcher = MacOSProcessLauncher(
            log_dir=Path("/unused"),
            process_factory=lambda pid: DisappearedProcess(pid),
        )
        process = launcher.attach(8123)
        self.assertIsNotNone(process)

        process.terminate()
        process.kill()

        self.assertEqual(process.wait(0.1), 0)


class ProcessProbeTests(unittest.TestCase):
    def test_pid_identity_includes_birth_time_and_detects_reuse(self) -> None:
        observed = _FakePsutilProcess(8123)
        probe = MacOSProcessProbe(process_factory=lambda pid: observed)
        process = type("Managed", (), {"pid": 8123})()

        identity = probe.identity(process)

        self.assertEqual(identity.pid, 8123)
        self.assertTrue(identity.birth_token.startswith("psutil-create-time:"))
        self.assertTrue(probe.identity_matches(identity))
        observed.created_at += 1
        self.assertFalse(probe.identity_matches(identity))

    def test_readiness_is_bounded_to_openai_models_on_literal_loopback(self) -> None:
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"data": []})

        probe = MacOSProcessProbe(transport=httpx.MockTransport(respond))

        self.assertTrue(probe.is_ready("http://127.0.0.1:8766", timeout=0.25))
        self.assertEqual(str(requests[0].url), "http://127.0.0.1:8766/v1/models")
        with self.assertRaisesRegex(ValueError, "literal loopback"):
            probe.is_ready("http://localhost:8766", timeout=0.25)
        with self.assertRaisesRegex(ValueError, "literal loopback"):
            probe.is_ready("http://10.0.0.2:8766", timeout=0.25)
        with self.assertRaisesRegex(ValueError, "positive"):
            probe.is_ready("http://127.0.0.1:8766", timeout=0)

    def test_readiness_treats_transport_errors_and_non_success_as_not_ready(self):
        unavailable = MacOSProcessProbe(
            transport=httpx.MockTransport(lambda request: httpx.Response(503))
        )
        disconnected = MacOSProcessProbe(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    httpx.ConnectError("refused", request=request)
                )
            )
        )

        self.assertFalse(unavailable.is_ready("http://127.0.0.1:8766", timeout=0.1))
        self.assertFalse(disconnected.is_ready("http://127.0.0.1:8766", timeout=0.1))


class HostPolicyAdapterTests(unittest.TestCase):
    def test_memory_pressure_uses_conservative_available_memory_thresholds(self):
        sample = SimpleNamespace(total=100, available=26)
        pressure = MacOSMemoryPressure(sample=lambda: sample)

        self.assertEqual(pressure.current(), PressureLevel.NORMAL)
        sample.available = 25
        self.assertEqual(pressure.current(), PressureLevel.WARNING)
        sample.available = 15
        self.assertEqual(pressure.current(), PressureLevel.CRITICAL)

        with self.assertRaisesRegex(ValueError, "thresholds"):
            MacOSMemoryPressure(
                warning_available_ratio=0.1, critical_available_ratio=0.2
            )

    def test_clock_delegates_to_system_time_with_injectable_test_seams(self) -> None:
        sleeps: list[float] = []
        clock = SystemClock(
            monotonic=lambda: 12.5,
            time_ns=lambda: 99,
            sleep=sleeps.append,
        )

        self.assertEqual(clock.monotonic(), 12.5)
        self.assertEqual(clock.time_ns(), 99)
        clock.sleep(0.2)
        self.assertEqual(sleeps, [0.2])

    def test_desired_state_view_reloads_config_without_starting_services(self) -> None:
        configs = [_config()]
        desired = ConfigDesiredState(lambda: configs[-1])

        self.assertEqual(str(desired.service("coding").name), "coding")
        self.assertIsNone(desired.service("memory"))

        configs.append(_config(service_name="memory"))
        self.assertIsNone(desired.service("coding"))
        self.assertEqual(
            tuple(str(service.name) for service in desired.services()), ("memory",)
        )


class ExactRuntimeLaunchSupplyTests(unittest.TestCase):
    def test_launch_requires_exact_revision_and_runtime_scoped_remote_code_grant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, model = self._physical_supply(root)
            capabilities = runtime.capabilities | {"trust_remote_code"}
            runtime = SuppliedRuntimeInstallation(
                installation_id=runtime.installation_id,
                runtime=runtime.runtime,
                version=runtime.version,
                provenance=runtime.provenance,
                root=runtime.root,
                launcher=runtime.launcher,
                capabilities=capabilities,
                bundle_id=runtime.bundle_id,
            )
            config = _config(
                runtime_root=runtime.root,
                runtime_launcher=runtime.launcher,
                runtime_capabilities=capabilities,
                trust_remote_code=True,
            )
            grants: list[dict[str, object]] = []
            security = _AllowingModelSecurity(risks=("repository_code",))
            supply = ExactRuntimeLaunchSupply(
                load_config=lambda: config,
                runtime_installations={runtime.installation_id: runtime},
                model_installations={"qwen-exact": model},
                launch_builder=RuntimeLaunchBuilder(RuntimeCatalogue.load_builtin()),
                model_security=security,
                model_verifier=_verified_model,
                trust_grants=lambda: grants,
            )

            with self.assertRaisesRegex(CapabilityValidationError, "not trusted"):
                supply.prepare_launch(config.services["coding"], "127.0.0.1", 49152)

            grants.append(
                {
                    "model_installation": "qwen-exact",
                    "repository": "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit",
                    "revision": _REVISION,
                    "runtime_installation": "optiq@0.2.18",
                    "accepted_risks": ["remote_code", "repository_code"],
                }
            )
            prepared = supply.prepare_launch(
                config.services["coding"], "127.0.0.1", 49152
            )

            self.assertIn("--trust-remote-code", prepared.argv)

    def test_launch_fails_closed_without_security_or_integrity_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, model = self._physical_supply(root)
            config = _config(
                runtime_root=runtime.root,
                runtime_launcher=runtime.launcher,
                runtime_capabilities=runtime.capabilities,
            )

            class MissingSecurity(_AllowingModelSecurity):
                def require(self, repository, revision):
                    raise ValueError("assessment absent")

            missing = ExactRuntimeLaunchSupply(
                load_config=lambda: config,
                runtime_installations={runtime.installation_id: runtime},
                model_installations={"qwen-exact": model},
                launch_builder=RuntimeLaunchBuilder(RuntimeCatalogue.load_builtin()),
                model_security=MissingSecurity(),
                model_verifier=_verified_model,
            )
            with self.assertRaisesRegex(CapabilityValidationError, "security gate"):
                missing.prepare_launch(config.services["coding"], "127.0.0.1", 49152)

            corrupt = ExactRuntimeLaunchSupply(
                load_config=lambda: config,
                runtime_installations={runtime.installation_id: runtime},
                model_installations={"qwen-exact": model},
                launch_builder=RuntimeLaunchBuilder(RuntimeCatalogue.load_builtin()),
                model_security=_AllowingModelSecurity(),
                model_verifier=lambda _model: VerificationResult(
                    "incomplete", "cache-integrity", ("hash mismatch",)
                ),
            )
            with self.assertRaisesRegex(CapabilityValidationError, "security gate"):
                corrupt.prepare_launch(config.services["coding"], "127.0.0.1", 49152)

    def test_launch_resolves_current_physical_supply_at_execution_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, model = self._physical_supply(root)
            config = _config(
                runtime_root=runtime.root,
                runtime_launcher=runtime.launcher,
                runtime_capabilities=runtime.capabilities,
            )
            runtimes = {}
            models = {}
            supply = ExactRuntimeLaunchSupply(
                load_config=lambda: config,
                runtime_installations=lambda: runtimes,
                model_installations=lambda: models,
                launch_builder=RuntimeLaunchBuilder(RuntimeCatalogue.load_builtin()),
                model_security=_AllowingModelSecurity(),
                model_verifier=_verified_model,
            )
            runtimes[runtime.installation_id] = runtime
            models["qwen-exact"] = model

            prepared = supply.prepare_launch(
                config.services["coding"], "127.0.0.1", 49152
            )

            self.assertEqual(prepared.argv[0], runtime.launcher[0])

    def test_launch_uses_configured_installation_and_exact_cached_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, model = self._physical_supply(root)
            config = _config(
                runtime_root=runtime.root,
                runtime_launcher=runtime.launcher,
                runtime_capabilities=runtime.capabilities,
            )
            supply = ExactRuntimeLaunchSupply(
                load_config=lambda: config,
                runtime_installations={runtime.installation_id: runtime},
                model_installations={"qwen-exact": model},
                launch_builder=RuntimeLaunchBuilder(RuntimeCatalogue.load_builtin()),
                model_security=_AllowingModelSecurity(),
                model_verifier=_verified_model,
                environment={"METAL_DEVICE_WRAPPER_TYPE": "1"},
            )

            prepared = supply.prepare_launch(
                config.services["coding"], "127.0.0.1", 49152
            )

            self.assertEqual(
                prepared.argv,
                (
                    str(runtime.root / "bin/optiq"),
                    "serve",
                    "--model",
                    str(model.snapshot_path.resolve()),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "49152",
                    "--kv-config",
                    str((model.snapshot_path / "kv_config.json").resolve()),
                    "--mtp",
                ),
            )
            self.assertEqual(
                prepared.environment,
                {
                    "METAL_DEVICE_WRAPPER_TYPE": "1",
                    "MASTIC_SERVICE_NAME": "coding",
                    "HF_HUB_OFFLINE": "1",
                },
            )
            self.assertEqual(
                prepared.required_capabilities,
                frozenset({"model", "host", "port", "kv_config", "mtp"}),
            )

    def test_launch_accepts_hugging_face_blob_symlink_without_allowing_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, model = self._physical_supply(root)
            repository_cache = root / "cache" / "models--mlx-community--Qwen"
            snapshot = repository_cache / "snapshots" / _REVISION
            snapshot.mkdir(parents=True)
            blob = repository_cache / "blobs" / ("a" * 40)
            blob.parent.mkdir()
            blob.write_text("{}")
            (snapshot / "kv_config.json").symlink_to(Path("../../blobs") / blob.name)
            model = SuppliedModelInstallation(
                installation_id=model.installation_id,
                revision=model.revision,
                cached_revision_id=model.cached_revision_id,
                snapshot_path=snapshot,
                provenance=model.provenance,
            )
            config = _config(
                runtime_root=runtime.root,
                runtime_launcher=runtime.launcher,
                runtime_capabilities=runtime.capabilities,
            )
            supply = ExactRuntimeLaunchSupply(
                load_config=lambda: config,
                runtime_installations={runtime.installation_id: runtime},
                model_installations={"qwen-exact": model},
                launch_builder=RuntimeLaunchBuilder(RuntimeCatalogue.load_builtin()),
                model_security=_AllowingModelSecurity(),
                model_verifier=_verified_model,
            )

            prepared = supply.prepare_launch(
                config.services["coding"], "127.0.0.1", 49152
            )

            kv_index = prepared.argv.index("--kv-config") + 1
            self.assertEqual(prepared.argv[kv_index], str(blob.resolve()))

            outside = root / "outside.json"
            outside.write_text("{}")
            (snapshot / "kv_config.json").unlink()
            (snapshot / "kv_config.json").symlink_to(outside)
            with self.assertRaisesRegex(
                CapabilityValidationError, "exact cached model snapshot"
            ):
                supply.prepare_launch(config.services["coding"], "127.0.0.1", 49152)

    def test_launch_rejects_runtime_or_model_that_differs_from_desired_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, model = self._physical_supply(root)
            config = _config(
                runtime_root=runtime.root,
                runtime_launcher=runtime.launcher,
                runtime_capabilities=runtime.capabilities,
            )
            mismatched_runtime = SuppliedRuntimeInstallation(
                installation_id=runtime.installation_id,
                runtime=runtime.runtime,
                version="0.2.19",
                provenance=runtime.provenance,
                root=runtime.root,
                launcher=runtime.launcher,
                capabilities=runtime.capabilities,
            )
            supply = ExactRuntimeLaunchSupply(
                load_config=lambda: config,
                runtime_installations={runtime.installation_id: mismatched_runtime},
                model_installations={"qwen-exact": model},
                launch_builder=RuntimeLaunchBuilder(RuntimeCatalogue.load_builtin()),
                model_security=_AllowingModelSecurity(),
                model_verifier=_verified_model,
            )
            with self.assertRaisesRegex(CapabilityValidationError, "runtime version"):
                supply.prepare_launch(config.services["coding"], "127.0.0.1", 49152)

            wrong_revision_identity = SuppliedRevision(
                repo_id=model.revision.repo_id,
                commit_sha="1" * 40,
                requested_revision="1" * 40,
                evidence="test",
            )
            wrong_revision = SuppliedModelInstallation(
                installation_id=wrong_revision_identity.revision_id,
                revision=wrong_revision_identity,
                cached_revision_id=wrong_revision_identity.revision_id,
                snapshot_path=model.snapshot_path,
                provenance=model.provenance,
            )
            supply = ExactRuntimeLaunchSupply(
                load_config=lambda: config,
                runtime_installations={runtime.installation_id: runtime},
                model_installations={"qwen-exact": wrong_revision},
                launch_builder=RuntimeLaunchBuilder(RuntimeCatalogue.load_builtin()),
                model_security=_AllowingModelSecurity(),
                model_verifier=_verified_model,
            )
            with self.assertRaisesRegex(CapabilityValidationError, "model revision"):
                supply.prepare_launch(config.services["coding"], "127.0.0.1", 49152)

    def test_launch_rejects_missing_cache_artifacts_and_unobserved_capabilities(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, model = self._physical_supply(root)
            config = _config(
                runtime_root=runtime.root,
                runtime_launcher=runtime.launcher,
                runtime_capabilities=runtime.capabilities,
            )
            (model.snapshot_path / "kv_config.json").unlink()
            supply = ExactRuntimeLaunchSupply(
                load_config=lambda: config,
                runtime_installations={runtime.installation_id: runtime},
                model_installations={"qwen-exact": model},
                launch_builder=RuntimeLaunchBuilder(RuntimeCatalogue.load_builtin()),
                model_security=_AllowingModelSecurity(),
                model_verifier=_verified_model,
            )
            with self.assertRaisesRegex(CapabilityValidationError, "kv_config"):
                supply.prepare_launch(config.services["coding"], "127.0.0.1", 49152)

            runtime_without_mtp = SuppliedRuntimeInstallation(
                installation_id=runtime.installation_id,
                runtime=runtime.runtime,
                version=runtime.version,
                provenance=runtime.provenance,
                root=runtime.root,
                launcher=runtime.launcher,
                capabilities=runtime.capabilities - {"mtp"},
            )
            config_without_mtp = _config(
                runtime_root=runtime_without_mtp.root,
                runtime_launcher=runtime_without_mtp.launcher,
                runtime_capabilities=runtime_without_mtp.capabilities,
            )
            (model.snapshot_path / "kv_config.json").write_text("{}")
            supply = ExactRuntimeLaunchSupply(
                load_config=lambda: config_without_mtp,
                runtime_installations={runtime.installation_id: runtime_without_mtp},
                model_installations={"qwen-exact": model},
                launch_builder=RuntimeLaunchBuilder(RuntimeCatalogue.load_builtin()),
                model_security=_AllowingModelSecurity(),
                model_verifier=_verified_model,
            )
            with self.assertRaises(UnsupportedLaunchOption):
                supply.prepare_launch(
                    config_without_mtp.services["coding"], "127.0.0.1", 49152
                )

    @staticmethod
    def _physical_supply(root: Path):
        runtime_root = root / "runtime"
        launcher = runtime_root / "bin/optiq"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o700)
        snapshot = root / "cache" / _REVISION
        snapshot.mkdir(parents=True)
        (snapshot / "kv_config.json").write_text("{}")
        runtime = SuppliedRuntimeInstallation(
            installation_id="optiq@0.2.18",
            runtime="optiq",
            version="0.2.18",
            provenance="tested",
            root=runtime_root,
            launcher=(str(launcher), "serve"),
            capabilities=frozenset({"model", "host", "port", "kv_config", "mtp"}),
        )
        revision = SuppliedRevision(
            repo_id="mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit",
            commit_sha=_REVISION,
            requested_revision=_REVISION,
            evidence="test",
        )
        model = SuppliedModelInstallation(
            installation_id=revision.revision_id,
            revision=revision,
            cached_revision_id=revision.revision_id,
            snapshot_path=snapshot,
            provenance=SuppliedProvenance(
                requested_revision=_REVISION,
                resolved_sha=_REVISION,
                source="hugging-face-cache",
            ),
        )
        return runtime, model


if __name__ == "__main__":
    unittest.main()
