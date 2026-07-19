import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mastic.application.dispatch import ApplicationError
from mastic.infrastructure.application_supply import ApplicationSupply
from mastic.infrastructure import application_supply

HINDSIGHT_API_LAUNCHERS = (
    "hindsight-admin",
    "hindsight-api",
    "hindsight-local-mcp",
    "hindsight-worker",
)


class ApplicationSupplyTests(unittest.TestCase):
    def test_incomplete_cache_reports_complete_set_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cache = root / "cache/application-targets-v1"
            cache.mkdir(parents=True)
            (cache / "manifest.json").write_text(
                json.dumps(
                    {"schema_version": 1, "platform": "macos-arm64", "artifacts": []}
                ),
                encoding="utf-8",
            )
            supply = ApplicationSupply(root / "home", cache, root / "state")

            with self.assertRaises(ApplicationError) as raised:
                supply.execute(
                    "application.install",
                    {
                        "application_targets": ("codex", "hindsight"),
                        "offline": True,
                        "confirmed": True,
                    },
                )

            self.assertEqual(raised.exception.code, "application_artifacts_missing")
            self.assertIn("codex-cli", str(raised.exception))
            self.assertIn("hindsight-cli", str(raised.exception))
            self.assertIn("hindsight-api", str(raised.exception))
            self.assertFalse((root / "home/.local/bin").exists())
            self.assertFalse((root / "state/application-installations.json").exists())

    def test_exact_preinstalled_clis_are_adopted_and_api_is_installed_offline(
        self,
    ) -> None:
        calls = []
        fail_api_install = [True]

        def run(command, **kwargs):
            calls.append((tuple(str(item) for item in command), kwargs))
            if tuple(command[1:3]) == ("tool", "install") and fail_api_install[0]:
                fail_api_install[0] = False
                raise subprocess.CalledProcessError(1, command)
            if tuple(command[1:3]) == ("tool", "install"):
                (application_tool_dir / "hindsight-api").mkdir(parents=True)
                application_bin_dir.mkdir(parents=True)
                for name in HINDSIGHT_API_LAUNCHERS:
                    (application_bin_dir / name).write_bytes(f"owned {name}".encode())
            if tuple(command[1:3]) == ("tool", "uninstall"):
                shutil.rmtree(application_tool_dir / "hindsight-api")
                for name in HINDSIGHT_API_LAUNCHERS:
                    (application_bin_dir / name).unlink()
            executable = str(command[0])
            if command[-1] == "--version":
                output = (
                    "codex-cli 0.144.1\n"
                    if executable.endswith("/codex")
                    else "hindsight 0.8.4\n"
                )
            elif any("importlib.metadata" in str(item) for item in command):
                output = "0.8.4\n"
            else:
                output = ""
            return subprocess.CompletedProcess(command, 0, output, "")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            application_tool_dir = root / "mastic-data/application-tools"
            application_bin_dir = root / "mastic-data/application-bin"
            cache = root / "cache/application-targets-v1"
            artifacts = cache / "artifacts"
            artifacts.mkdir(parents=True)
            codex_bytes = b"exact codex binary"
            codex_archive = artifacts / "codex-aarch64-apple-darwin.tar.gz"
            with tarfile.open(codex_archive, "w:gz") as archive:
                info = tarfile.TarInfo("codex-aarch64-apple-darwin")
                info.mode = 0o755
                info.size = len(codex_bytes)
                archive.addfile(info, io.BytesIO(codex_bytes))
            hindsight = artifacts / "hindsight-darwin-arm64"
            hindsight.write_bytes(b"exact hindsight binary")
            api = artifacts / "hindsight-api-0.8.4-macos-arm64.tar.gz"
            with tarfile.open(api, "w:gz") as archive:
                wheel = b"wheel bytes"
                info = tarfile.TarInfo("wheels/hindsight_api-0.8.4-py3-none-any.whl")
                info.size = len(wheel)
                archive.addfile(info, io.BytesIO(wheel))
                lock = b"hindsight-api==0.8.4\n"
                info = tarfile.TarInfo("requirements.lock")
                info.size = len(lock)
                archive.addfile(info, io.BytesIO(lock))
                checksums = (
                    hashlib.sha256(lock).hexdigest().encode()
                    + b"  requirements.lock\n"
                    + hashlib.sha256(wheel).hexdigest().encode()
                    + b"  wheels/hindsight_api-0.8.4-py3-none-any.whl\n"
                )
                info = tarfile.TarInfo("SHA256SUMS")
                info.size = len(checksums)
                archive.addfile(info, io.BytesIO(checksums))
            manifest = {
                "schema_version": 1,
                "platform": "macos-arm64",
                "artifacts": [
                    _artifact("codex-cli", "0.144.1", codex_archive),
                    _artifact("hindsight-cli", "0.8.4", hindsight),
                    _artifact("hindsight-api", "0.8.4", api),
                ],
            }
            (cache / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (cache.parent / "bootstrap-receipt.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "closure_sha256": "c" * 64,
                        "application_manifest_sha256": hashlib.sha256(
                            (cache / "manifest.json").read_bytes()
                        ).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            home_bin = home / ".local/bin"
            home_bin.mkdir(parents=True)
            (home_bin / "codex").write_bytes(codex_bytes)
            (home_bin / "hindsight").write_bytes(hindsight.read_bytes())
            with patch.dict(
                application_supply._OFFICIAL_DIGESTS,
                {
                    "codex-cli": hashlib.sha256(codex_archive.read_bytes()).hexdigest(),
                    "hindsight-cli": hashlib.sha256(hindsight.read_bytes()).hexdigest(),
                },
            ):
                supply = ApplicationSupply(
                    home,
                    cache,
                    root / "state",
                    uv_executable=root / "uv",
                    python_executable=root / "python",
                    application_tool_dir=application_tool_dir,
                    application_bin_dir=application_bin_dir,
                    run_command=run,
                )
                parameters = {
                    "application_targets": ("codex", "hindsight"),
                    "offline": True,
                    "confirmed": True,
                }
                with self.assertRaises(subprocess.CalledProcessError):
                    supply.execute("application.install", parameters)
                interrupted = json.loads(
                    (root / "state/application-installations.json").read_text()
                )
                self.assertEqual(interrupted["state"], "interrupted")
                self.assertEqual(
                    interrupted["applications"]["hindsight"]["api_ownership"],
                    "mastic",
                )
                result = supply.execute("application.install", parameters)

            self.assertEqual(result["applications"]["codex"]["provenance"], "adopted")
            self.assertEqual(
                result["applications"]["hindsight"]["provenance"], "installed"
            )
            command = next(command for command, _ in calls if "install" in command)
            self.assertEqual(command[:3], (str(root / "uv"), "tool", "install"))
            self.assertIn("--offline", command)
            self.assertIn("--no-index", command)
            self.assertIn("--no-python-downloads", command)
            install_call = next(
                kwargs for candidate, kwargs in calls if "install" in candidate
            )
            self.assertEqual(
                install_call["env"]["UV_TOOL_DIR"], str(application_tool_dir)
            )
            self.assertEqual(
                install_call["env"]["UV_TOOL_BIN_DIR"], str(application_bin_dir)
            )
            api_probe = next(
                kwargs
                for candidate, kwargs in calls
                if candidate[-1:] == ("--help",)
                and candidate[0] == str(application_bin_dir / "hindsight-api")
            )
            self.assertEqual(api_probe["timeout"], 120)
            self.assertEqual(sum("install" in candidate for candidate, _ in calls), 2)
            journal = json.loads(
                (root / "state/application-installations.json").read_text()
            )
            self.assertEqual(
                journal["applications"]["codex"]["ownership"], "third-party"
            )
            self.assertEqual(journal["applications"]["hindsight"]["ownership"], "mixed")
            self.assertEqual(
                journal["applications"]["hindsight"]["api_tool_root"],
                str(application_tool_dir / "hindsight-api"),
            )
            self.assertEqual(
                journal["applications"]["hindsight"]["api_bin_paths"],
                {
                    name: str(application_bin_dir / name)
                    for name in HINDSIGHT_API_LAUNCHERS
                },
            )

            (home_bin / "codex").write_bytes(b"external replacement")
            installs_before = sum("install" in candidate for candidate, _ in calls)
            with patch.dict(
                application_supply._OFFICIAL_DIGESTS,
                {
                    "codex-cli": hashlib.sha256(codex_archive.read_bytes()).hexdigest(),
                    "hindsight-cli": hashlib.sha256(hindsight.read_bytes()).hexdigest(),
                },
            ):
                with self.assertRaises(ApplicationError) as conflict:
                    supply.execute("application.install", parameters)
            self.assertEqual(conflict.exception.code, "application_install_conflict")
            self.assertEqual(
                sum("install" in candidate for candidate, _ in calls), installs_before
            )
            (home_bin / "codex").write_bytes(codex_bytes)

            inventory = ApplicationSupply(
                home,
                cache,
                root / "state",
                uv_executable=root / "uv",
                python_executable=root / "python",
                application_tool_dir=application_tool_dir,
                application_bin_dir=application_bin_dir,
                run_command=run,
            ).inventory()
            self.assertEqual(
                inventory, {"owned": ("hindsight",), "retained": ("codex",)}
            )
            removal = ApplicationSupply(
                home,
                cache,
                root / "state",
                uv_executable=root / "uv",
                python_executable=root / "python",
                application_tool_dir=application_tool_dir,
                application_bin_dir=application_bin_dir,
                run_command=run,
            ).execute(
                "application.remove",
                {"applications": ("hindsight",), "confirmed": True},
            )
            self.assertEqual(removal["removed"], ["hindsight"])
            self.assertEqual(removal["retained"], [str(home_bin / "hindsight")])
            self.assertTrue((home_bin / "codex").exists())
            self.assertTrue((home_bin / "hindsight").exists())
            self.assertIn(
                (str(root / "uv"), "tool", "uninstall", "hindsight-api"),
                [command for command, _ in calls],
            )
            uninstall_call = next(
                kwargs for candidate, kwargs in calls if "uninstall" in candidate
            )
            self.assertEqual(
                uninstall_call["env"]["UV_TOOL_DIR"], str(application_tool_dir)
            )
            self.assertEqual(
                uninstall_call["env"]["UV_TOOL_BIN_DIR"], str(application_bin_dir)
            )

            application_bin_dir.mkdir(parents=True, exist_ok=True)
            existing_launcher = application_bin_dir / "hindsight-worker"
            existing_launcher.write_bytes(b"third-party launcher")
            with patch.dict(
                application_supply._OFFICIAL_DIGESTS,
                {
                    "codex-cli": hashlib.sha256(codex_archive.read_bytes()).hexdigest(),
                    "hindsight-cli": hashlib.sha256(hindsight.read_bytes()).hexdigest(),
                },
            ):
                with self.assertRaises(ApplicationError) as api_conflict:
                    supply.execute(
                        "application.install",
                        {
                            "application_targets": ("hindsight",),
                            "offline": True,
                            "confirmed": True,
                        },
                    )
            self.assertEqual(
                api_conflict.exception.code, "application_install_conflict"
            )
            self.assertEqual(existing_launcher.read_bytes(), b"third-party launcher")

    def test_process_death_after_owned_codex_write_resumes_as_owned(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            cache = root / "cache/application-targets-v1"
            artifacts = cache / "artifacts"
            artifacts.mkdir(parents=True)
            binary = b"exact codex binary"
            archive_path = artifacts / "codex-aarch64-apple-darwin.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                info = tarfile.TarInfo("codex-aarch64-apple-darwin")
                info.mode = 0o755
                info.size = len(binary)
                archive.addfile(info, io.BytesIO(binary))
            manifest = {
                "schema_version": 1,
                "platform": "macos-arm64",
                "artifacts": [_artifact("codex-cli", "0.144.1", archive_path)],
            }
            manifest_path = cache / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (cache.parent / "bootstrap-receipt.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "closure_sha256": "c" * 64,
                        "application_manifest_sha256": hashlib.sha256(
                            manifest_path.read_bytes()
                        ).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            with patch.dict(
                application_supply._OFFICIAL_DIGESTS, {"codex-cli": digest}
            ):
                with self.assertRaises(SystemExit):
                    ApplicationSupply(
                        home,
                        cache,
                        root / "state",
                        run_command=lambda *args, **kwargs: (_ for _ in ()).throw(
                            SystemExit()
                        ),
                    ).execute(
                        "application.install",
                        {
                            "application_targets": ("codex",),
                            "offline": True,
                            "confirmed": True,
                        },
                    )
                pending = json.loads(
                    (root / "state/application-installations.json").read_text()
                )
                self.assertEqual(pending["state"], "installing")
                self.assertEqual(
                    pending["applications"]["codex"]["ownership"], "mastic"
                )
                (home / ".local/bin/codex").write_bytes(b"third-party replacement")
                with self.assertRaises(ApplicationError) as conflict:
                    ApplicationSupply(
                        home,
                        cache,
                        root / "state",
                        run_command=lambda command, **kwargs: (
                            subprocess.CompletedProcess(
                                command, 0, "codex-cli 0.144.1\n", ""
                            )
                        ),
                    ).execute(
                        "application.install",
                        {
                            "application_targets": ("codex",),
                            "offline": True,
                            "confirmed": True,
                        },
                    )
                self.assertEqual(
                    conflict.exception.code, "application_install_conflict"
                )
                self.assertEqual(
                    (home / ".local/bin/codex").read_bytes(),
                    b"third-party replacement",
                )
                (home / ".local/bin/codex").unlink()
                resumed = ApplicationSupply(
                    home,
                    cache,
                    root / "state",
                    run_command=lambda command, **kwargs: subprocess.CompletedProcess(
                        command, 0, "codex-cli 0.144.1\n", ""
                    ),
                ).execute(
                    "application.install",
                    {
                        "application_targets": ("codex",),
                        "offline": True,
                        "confirmed": True,
                    },
                )
            self.assertEqual(resumed["applications"]["codex"]["ownership"], "mastic")
            self.assertEqual(
                resumed["applications"]["codex"]["provenance"], "installed"
            )

    def test_interrupted_owned_codex_removal_resumes_from_pending_journal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            executable = home / ".local/bin/codex"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"owned codex")
            state = root / "state"
            state.mkdir()
            journal_path = state / "application-installations.json"
            journal_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "state": "complete",
                        "applications": {
                            "codex": {
                                "version": "0.144.1",
                                "provenance": "installed",
                                "ownership": "mastic",
                                "path": str(executable),
                                "sha256": hashlib.sha256(b"owned codex").hexdigest(),
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            supply = ApplicationSupply(
                home, root / "cache/application-targets-v1", state
            )
            replace = os.replace
            failed = False

            def fail_after_unlink(source, destination):
                nonlocal failed
                if (
                    not failed
                    and Path(destination) == journal_path
                    and not executable.exists()
                ):
                    failed = True
                    raise OSError("simulated journal replacement failure")
                return replace(source, destination)

            with patch.object(
                application_supply.os, "replace", side_effect=fail_after_unlink
            ):
                with self.assertRaises(OSError):
                    supply.execute(
                        "application.remove",
                        {"applications": ("codex",), "confirmed": True},
                    )

            self.assertFalse(executable.exists())
            pending = json.loads(journal_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["state"], "removing")
            self.assertEqual(
                pending["applications"]["codex"]["removal_state"], "pending"
            )

            removed = supply.execute(
                "application.remove",
                {"applications": ("codex",), "confirmed": True},
            )

            self.assertEqual(removed, {"removed": ["codex"], "retained": []})
            completed = json.loads(journal_path.read_text(encoding="utf-8"))
            self.assertEqual(completed["state"], "removed")
            self.assertEqual(completed["applications"], {})

    def test_interrupted_hindsight_removal_resumes_each_owned_component(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            cli = home / ".local/bin/hindsight"
            cli.parent.mkdir(parents=True)
            cli.write_bytes(b"owned hindsight")
            codex = home / ".local/bin/codex"
            codex.write_bytes(b"owned codex")
            tool_dir = root / "data/application-tools"
            api_root = tool_dir / "hindsight-api"
            api_root.mkdir(parents=True)
            bin_dir = root / "data/application-bin"
            bin_dir.mkdir(parents=True)
            api_launchers = {name: bin_dir / name for name in HINDSIGHT_API_LAUNCHERS}
            for name, path in api_launchers.items():
                path.write_bytes(f"owned {name}".encode())
            state = root / "state"
            state.mkdir()
            journal_path = state / "application-installations.json"
            journal_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "state": "complete",
                        "applications": {
                            "codex": {
                                "version": "0.144.1",
                                "provenance": "installed",
                                "ownership": "mastic",
                                "path": str(codex),
                                "sha256": hashlib.sha256(b"owned codex").hexdigest(),
                            },
                            "hindsight": {
                                "version": "0.8.4",
                                "provenance": "installed",
                                "ownership": "mastic",
                                "cli_path": str(cli),
                                "cli_sha256": hashlib.sha256(
                                    b"owned hindsight"
                                ).hexdigest(),
                                "cli_ownership": "mastic",
                                "api_ownership": "mastic",
                                "api_tool_root": str(api_root),
                                "api_bin_paths": {
                                    name: str(path)
                                    for name, path in api_launchers.items()
                                },
                                "api_bin_sha256": {
                                    name: hashlib.sha256(
                                        f"owned {name}".encode()
                                    ).hexdigest()
                                    for name in api_launchers
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            calls = []

            def interrupted_uninstall(command, **kwargs):
                calls.append((command, kwargs))
                shutil.rmtree(api_root)
                for path in api_launchers.values():
                    path.unlink()
                raise subprocess.CalledProcessError(1, command)

            supply = ApplicationSupply(
                home,
                root / "data/bootstrap-artifacts/application-targets-v1",
                state,
                uv_executable=root / "data/bootstrap-uv/uv",
                application_tool_dir=tool_dir,
                application_bin_dir=bin_dir,
                run_command=interrupted_uninstall,
            )
            with self.assertRaises(subprocess.CalledProcessError):
                supply.execute(
                    "application.remove",
                    {
                        "applications": ("codex", "hindsight"),
                        "confirmed": True,
                    },
                )

            pending = json.loads(journal_path.read_text(encoding="utf-8"))
            hindsight = pending["applications"]["hindsight"]
            self.assertEqual(hindsight["cli_removal_state"], "removed")
            self.assertEqual(hindsight["api_removal_state"], "pending")
            self.assertNotIn("codex", pending["applications"])
            self.assertEqual(pending["removed_applications"], ["codex"])
            self.assertFalse(codex.exists())
            self.assertFalse(cli.exists())
            self.assertFalse(api_root.exists())
            self.assertTrue(all(not path.exists() for path in api_launchers.values()))

            resumed = ApplicationSupply(
                home,
                root / "data/bootstrap-artifacts/application-targets-v1",
                state,
                uv_executable=root / "data/bootstrap-uv/uv",
                application_tool_dir=tool_dir,
                application_bin_dir=bin_dir,
                run_command=lambda *args, **kwargs: self.fail(
                    "completed pending removal must not invoke uv again"
                ),
            )
            removed = resumed.execute(
                "application.remove",
                {
                    "applications": ("codex", "hindsight"),
                    "confirmed": True,
                },
            )

            self.assertEqual(
                removed,
                {"removed": ["codex", "hindsight"], "retained": []},
            )
            self.assertEqual(len(calls), 1)


def _artifact(identity: str, version: str, path: Path) -> dict[str, object]:
    metadata = {
        "codex-cli": (
            "https://github.com/openai/codex/releases/download/rust-v0.144.1/codex-aarch64-apple-darwin.tar.gz",
            "standalone-tar",
            ["--version"],
            "codex-cli 0.144.1",
        ),
        "hindsight-cli": (
            "https://github.com/vectorize-io/hindsight/releases/download/v0.8.4/hindsight-darwin-arm64",
            "standalone",
            ["--version"],
            "hindsight 0.8.4",
        ),
        "hindsight-api": (
            "https://github.com/nisavid/mastic/releases/download/v0.1.0/" + path.name,
            "uv-tool-offline",
            ["python-metadata", "hindsight-api"],
            "0.8.4",
        ),
    }[identity]
    return {
        "id": identity,
        "version": version,
        "filename": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_url": metadata[0],
        "install_kind": metadata[1],
        "probe_argv": metadata[2],
        "probe_output": metadata[3],
    }


if __name__ == "__main__":
    unittest.main()
