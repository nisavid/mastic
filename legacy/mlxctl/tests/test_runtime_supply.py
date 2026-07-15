import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from mlxctl.infrastructure.runtime_supply import (
    RuntimeCatalogue,
    RuntimeChangePlanner,
    RuntimeInstallation,
    RuntimeManager,
    RuntimeProbeResult,
    SubprocessRuntimeProbe,
    RuntimeLaunchBuilder,
    TestedRuntimeBundle,
    UnsupportedLaunchOption,
)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]) -> None:
        self.calls.append(argv)
        if argv[:2] == ("uv", "venv"):
            Path(argv[-1], "bin").mkdir(parents=True)


class FakeProbe:
    def __init__(self, version: str, flags: set[str]) -> None:
        self.version = version
        self.flags = flags
        self.roots: list[Path] = []

    def probe(self, definition, root: Path) -> RuntimeProbeResult:
        self.roots.append(root)
        executable = "optiq" if definition.key == "optiq" else definition.launcher[0]
        suffix = definition.launcher[1:]
        return RuntimeProbeResult(
            version=self.version,
            launcher_relative=(f"bin/{executable}", *suffix),
            supported_flags=frozenset(self.flags),
        )


class RuntimeCatalogueTests(unittest.TestCase):
    def test_builtin_runtime_definitions_are_discoverable_without_installation(
        self,
    ) -> None:
        catalogue = RuntimeCatalogue.load_builtin()

        self.assertEqual(
            [definition.key for definition in catalogue.definitions],
            ["mlx_lm", "mlx_vlm", "optiq"],
        )
        self.assertEqual(
            [bundle.runtime for bundle in catalogue.tested_bundles],
            ["mlx_lm", "mlx_vlm", "optiq"],
        )
        self.assertTrue(
            all(
                sha256(Path(bundle.lock_path).read_bytes()).hexdigest()
                == bundle.lock_sha256
                for bundle in catalogue.tested_bundles
            )
        )
        self.assertEqual(catalogue.definition("optiq").launcher, ("optiq", "serve"))

    def test_capabilities_are_normalized_from_the_exact_installation_flags(
        self,
    ) -> None:
        catalogue = RuntimeCatalogue.load_builtin()

        self.assertEqual(
            catalogue.normalize_capabilities(
                "optiq",
                {"--model", "--host", "--port", "--kv-config", "--mtp"},
            ),
            frozenset({"model", "host", "port", "kv_config", "mtp"}),
        )

    def test_launch_argv_contains_only_capabilities_observed_on_installation(
        self,
    ) -> None:
        catalogue = RuntimeCatalogue.load_builtin()
        installation = RuntimeInstallation(
            installation_id="optiq-0.2.18",
            runtime="optiq",
            version="0.2.18",
            provenance="custom",
            root=Path("/runtimes/optiq-0.2.18"),
            launcher=("/runtimes/optiq-0.2.18/bin/optiq", "serve"),
            capabilities=frozenset(
                {"model", "host", "port", "kv_config", "mtp", "adapter"}
            ),
        )

        argv = RuntimeLaunchBuilder(catalogue).build(
            installation,
            model="/models/qwen",
            host="127.0.0.1",
            port=49152,
            options={
                "kv_config": "/models/qwen/kv_config.json",
                "mtp": True,
                "adapter": ["/models/a", "/models/b"],
            },
        )

        self.assertEqual(
            argv,
            (
                "/runtimes/optiq-0.2.18/bin/optiq",
                "serve",
                "--model",
                "/models/qwen",
                "--host",
                "127.0.0.1",
                "--port",
                "49152",
                "--kv-config",
                "/models/qwen/kv_config.json",
                "--mtp",
                "--adapter",
                "/models/a",
                "--adapter",
                "/models/b",
            ),
        )

        with self.assertRaisesRegex(
            UnsupportedLaunchOption,
            "does not support launch option 'max_context'",
        ):
            RuntimeLaunchBuilder(catalogue).build(
                installation,
                model="/models/qwen",
                host="127.0.0.1",
                port=49152,
                options={"max_context": 32768},
            )


class SubprocessRuntimeProbeTests(unittest.TestCase):
    def test_probes_module_runtime_version_launcher_and_exact_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / "bin/python"
            python.parent.mkdir(parents=True)
            python.touch()
            calls = []

            def run(argv, **options):
                calls.append((tuple(argv), options))
                if "importlib.metadata" in argv[-1]:
                    return SimpleNamespace(returncode=0, stdout="0.31.3\n", stderr="")
                return SimpleNamespace(
                    returncode=0,
                    stdout="usage: server [--model MODEL] [--host HOST] [--port PORT]",
                    stderr="",
                )

            definition = RuntimeCatalogue.load_builtin().definition("mlx_lm")
            result = SubprocessRuntimeProbe(run=run).probe(definition, root)

            self.assertEqual(result.version, "0.31.3")
            self.assertEqual(
                result.launcher_relative,
                ("bin/python", "-m", "mlx_lm.server"),
            )
            self.assertEqual(
                result.supported_flags, frozenset({"--model", "--host", "--port"})
            )
            self.assertTrue(all(options["shell"] is False for _, options in calls))

    def test_probes_optiq_console_script_and_rejects_failed_help(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            (root / "bin/python").touch()
            (root / "bin/optiq").touch()

            def run(argv, **_options):
                if "importlib.metadata" in argv[-1]:
                    return SimpleNamespace(returncode=0, stdout="0.3.3\n", stderr="")
                return SimpleNamespace(returncode=2, stdout="", stderr="broken")

            definition = RuntimeCatalogue.load_builtin().definition("optiq")
            with self.assertRaisesRegex(ValueError, "help probe failed"):
                SubprocessRuntimeProbe(run=run).probe(definition, root)


class RuntimeManagerTests(unittest.TestCase):
    def test_tested_bundle_is_installed_immutably_from_an_exact_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "optiq.lock"
            lock.write_text("mlx-optiq==0.2.18 --hash=sha256:example\n")
            bundle = TestedRuntimeBundle(
                bundle_id="optiq-0.2.18-py313-macos-arm64",
                runtime="optiq",
                version="0.2.18",
                python="3.13",
                platform="macos-arm64",
                lock_path=str(lock),
                lock_sha256=sha256(lock.read_bytes()).hexdigest(),
            )
            catalogue = RuntimeCatalogue.load_builtin(tested_bundles=(bundle,))
            runner = FakeRunner()
            probe = FakeProbe(
                "0.2.18", {"--model", "--host", "--port", "--kv-config", "--mtp"}
            )
            manager = RuntimeManager(
                catalogue,
                runner=runner,
                probe=probe,
                staging_token=lambda: "test",
            )

            installation = manager.install_tested(bundle.bundle_id, root / "installed")

            install_root = (root / "installed").resolve()
            final = install_root / bundle.bundle_id
            stage = install_root / f".{bundle.bundle_id}.staging-test"
            self.assertEqual(
                runner.calls,
                [
                    ("uv", "venv", "--python", "3.13", str(stage)),
                    (
                        "uv",
                        "pip",
                        "sync",
                        "--python",
                        str(stage / "bin/python"),
                        str(lock),
                    ),
                ],
            )
            self.assertEqual(installation.root, final)
            self.assertEqual(installation.launcher, (str(final / "bin/optiq"), "serve"))
            self.assertEqual(installation.provenance, "tested")
            self.assertEqual(installation.bundle_id, bundle.bundle_id)
            self.assertFalse(stage.exists())

            with self.assertRaisesRegex(FileExistsError, "immutable installation"):
                manager.install_tested(bundle.bundle_id, root / "installed")

    def test_custom_version_is_exactly_installed_and_probed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeRunner()
            probe = FakeProbe("0.3.3", {"--model", "--host", "--port"})
            manager = RuntimeManager(
                RuntimeCatalogue.load_builtin(),
                runner=runner,
                probe=probe,
                staging_token=lambda: "test",
            )

            installation = manager.install_custom(
                "optiq", "0.3.3", python="3.13", installation_root=root
            )

            install_root = root.resolve()
            final = install_root / "optiq-0.3.3-custom"
            stage = install_root / ".optiq-0.3.3-custom.staging-test"
            self.assertEqual(
                runner.calls,
                [
                    ("uv", "venv", "--python", "3.13", str(stage)),
                    (
                        "uv",
                        "pip",
                        "install",
                        "--python",
                        str(stage / "bin/python"),
                        "mlx-optiq==0.3.3",
                    ),
                ],
            )
            self.assertEqual(installation.root, final)
            self.assertEqual(installation.provenance, "custom")

    def test_existing_custom_environment_can_be_adopted_after_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "external"
            root.mkdir()
            manager = RuntimeManager(
                RuntimeCatalogue.load_builtin(),
                runner=FakeRunner(),
                probe=FakeProbe("0.3.3", {"--model", "--host", "--port"}),
            )

            installation = manager.adopt_custom("optiq", root)

            self.assertEqual(installation.version, "0.3.3")
            self.assertEqual(installation.provenance, "adopted")
            self.assertEqual(installation.root, root.resolve())


class RuntimeChangePlannerTests(unittest.TestCase):
    def test_remove_is_blocked_while_services_reference_installation(self) -> None:
        installation = RuntimeInstallation(
            installation_id="optiq-old",
            runtime="optiq",
            version="0.2.18",
            provenance="tested",
            root=Path("/runtime/old"),
            launcher=("/runtime/old/bin/optiq", "serve"),
            capabilities=frozenset(),
        )

        plan = RuntimeChangePlanner().plan_remove(
            installation, referenced_services=("coding", "memory")
        )

        self.assertFalse(plan.allowed)
        self.assertEqual(plan.referenced_services, ("coding", "memory"))
        self.assertIn("reassign referenced services", plan.steps[0])

    def test_update_and_rollback_retain_the_other_installation(self) -> None:
        current = self._installation("optiq-old", "0.2.18")
        target = self._installation("optiq-new", "0.3.3")
        planner = RuntimeChangePlanner()

        update = planner.plan_update(current, target, referenced_services=("coding",))
        rollback = planner.plan_rollback(
            target, current, referenced_services=("coding",)
        )

        self.assertTrue(update.allowed)
        self.assertIn("retain optiq-old", update.steps[-1])
        self.assertTrue(rollback.allowed)
        self.assertIn("retain optiq-new", rollback.steps[-1])

    @staticmethod
    def _installation(installation_id: str, version: str) -> RuntimeInstallation:
        return RuntimeInstallation(
            installation_id=installation_id,
            runtime="optiq",
            version=version,
            provenance="tested",
            root=Path(f"/runtime/{installation_id}"),
            launcher=(f"/runtime/{installation_id}/bin/optiq", "serve"),
            capabilities=frozenset(),
        )


if __name__ == "__main__":
    unittest.main()
