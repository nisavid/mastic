from __future__ import annotations

import stat
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
import tomlkit

from mlxctl.application.config_schema import validate_config
from mlxctl.domain.admission import PressureLevel
from mlxctl.infrastructure.system_adapters import (
    ConfigDesiredState,
    ExactRuntimeLaunchSupply,
    MacOSMemoryPressure,
    MacOSProcessLauncher,
    MacOSProcessProbe,
    SystemClock,
)
from mlxctl.infrastructure.model_supply import (
    ModelInstallation as SuppliedModelInstallation,
)
from mlxctl.infrastructure.model_supply import ModelProvenance as SuppliedProvenance
from mlxctl.infrastructure.model_supply import ModelRevision as SuppliedRevision
from mlxctl.infrastructure.runtime_supply import (
    RuntimeCatalogue,
    RuntimeInstallation as SuppliedRuntimeInstallation,
    RuntimeLaunchBuilder,
    UnsupportedLaunchOption,
)
from mlxctl.infrastructure.supervisor_v1 import CapabilityValidationError


_REVISION = "70a3aa32c7feef511182bf16aa332f37e8d82014"


def _config(
    *,
    service_name: str = "coding",
    runtime_root: Path = Path("/opt/mlxctl/runtimes/optiq@0.2.18"),
    runtime_launcher: tuple[str, ...] = (
        "/opt/mlxctl/runtimes/optiq@0.2.18/bin/optiq",
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

    def is_running(self):
        return self.running

    def status(self):
        return "running"

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


class ProcessLauncherTests(unittest.TestCase):
    def test_launch_uses_exact_argv_merged_environment_and_private_service_log(
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
                base_environment={"PATH": "/usr/bin", "INHERITED": "yes"},
                popen=popen,
            )

            process = launcher.launch(
                ("/runtime/bin/optiq", "serve", "--port", "49152"),
                {"MLXCTL_SERVICE_NAME": "coding", "EXTRA": "one"},
            )

            self.assertEqual(process.pid, 4123)
            argv, options = calls[0]
            self.assertEqual(argv, ("/runtime/bin/optiq", "serve", "--port", "49152"))
            self.assertIs(options["shell"], False)
            self.assertEqual(
                options["env"],
                {
                    "PATH": "/usr/bin",
                    "INHERITED": "yes",
                    "MLXCTL_SERVICE_NAME": "coding",
                    "EXTRA": "one",
                },
            )
            log = log_dir / "coding.log"
            self.assertTrue(log.is_file())
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(log_dir.stat().st_mode), 0o700)

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
                    ("/runtime/bin/optiq",), {"MLXCTL_SERVICE_NAME": "coding"}
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
            supply = ExactRuntimeLaunchSupply(
                load_config=lambda: config,
                runtime_installations={runtime.installation_id: runtime},
                model_installations={"qwen-exact": model},
                launch_builder=RuntimeLaunchBuilder(RuntimeCatalogue.load_builtin()),
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
                    "accepted_risks": ["remote_code"],
                }
            )
            prepared = supply.prepare_launch(
                config.services["coding"], "127.0.0.1", 49152
            )

            self.assertIn("--trust-remote-code", prepared.argv)

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
                    "MLXCTL_SERVICE_NAME": "coding",
                    "HF_HUB_OFFLINE": "1",
                },
            )
            self.assertEqual(
                prepared.required_capabilities,
                frozenset({"model", "host", "port", "kv_config", "mtp"}),
            )

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
