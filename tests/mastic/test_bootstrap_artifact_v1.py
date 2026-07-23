import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "scripts" / "build-bootstrap.zsh"
CLOSURE_BUILDER = ROOT / "scripts" / "build-bootstrap-closure.zsh"
_SUBPROCESS_TIMEOUT = 30


class BootstrapArtifactV1Tests(unittest.TestCase):
    def test_every_distribution_build_uses_the_exact_hashed_backend(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(project["build-system"]["requires"], ["hatchling==1.31.0"])

        build_lock = (ROOT / "packaging" / "build-backend.lock").read_text()
        self.assertIn("hatchling==1.31.0", build_lock)
        self.assertIn("--hash=sha256:", build_lock)
        for workflow_name in ("bootstrap-artifact.yml", "python-quality.yml"):
            workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text()
            self.assertIn("--build-constraints packaging/build-backend.lock", workflow)
            self.assertIn("--require-hashes", workflow)

        for workflow_name in ("bootstrap-artifact.yml", "python-quality.yml"):
            workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text()
            self.assertIn(
                "enable-cache: ${{ github.event_name != 'pull_request' }}",
                workflow,
            )

    def test_host_contract_reinstalls_over_a_running_supervisor(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "bootstrap-artifact.yml"
        ).read_text()

        self.assertIn("Reinstall over a running Supervisor", workflow)
        self.assertIn("stale-generation-sentinel", workflow)
        self.assertIn("[[ $after_pid != $before_pid ]]", workflow)
        self.assertIn('"$mastic" supervisor restart', workflow)

    def test_closure_builder_bounds_and_retries_all_direct_downloads(self) -> None:
        builder = CLOSURE_BUILDER.read_text()

        self.assertEqual(
            builder.count("curl --fail --silent --show-error --location"), 3
        )
        self.assertEqual(builder.count("--connect-timeout 30 --max-time 1800"), 3)
        self.assertEqual(builder.count("--retry 3 --retry-delay 2"), 3)

    def test_builder_embeds_exact_release_closure_and_produces_valid_zsh(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            wheel = root / "mastic-0.1.0-py3-none-any.whl"
            wheel.write_bytes(b"exact wheel bytes")
            closure = root / "mastic-bootstrap-closure-0.1.0-macos-arm64.tar.gz"
            closure.write_bytes(b"exact closure bytes")
            output = root / "bootstrap-mastic.zsh"

            completed = subprocess.run(
                ["zsh", str(BUILDER), str(wheel), str(closure), str(output)],
                capture_output=True,
                text=True,
                check=False,
                timeout=_SUBPROCESS_TIMEOUT,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            script = output.read_text(encoding="utf-8")
            self.assertIn("readonly MASTIC_VERSION='0.1.0'", script)
            self.assertIn(hashlib.sha256(wheel.read_bytes()).hexdigest(), script)
            self.assertIn(hashlib.sha256(closure.read_bytes()).hexdigest(), script)
            self.assertIn("--connect-timeout 30", script)
            self.assertIn("--max-time 1800", script)
            self.assertIn("--retry 3", script)
            self.assertNotIn("@MASTIC_", script)
            self.assertTrue(output.stat().st_mode & stat.S_IXUSR)
            syntax = subprocess.run(
                ["zsh", "-n", str(output)],
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
            self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_builder_rejects_a_release_tag_that_mismatches_the_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            wheel = root / "mastic-0.1.0-py3-none-any.whl"
            wheel.write_bytes(b"exact wheel bytes")
            closure = root / "mastic-bootstrap-closure-0.1.0-macos-arm64.tar.gz"
            closure.write_bytes(b"exact closure bytes")
            output = root / "bootstrap-mastic.zsh"

            completed = subprocess.run(
                [
                    "zsh",
                    str(BUILDER),
                    str(wheel),
                    str(closure),
                    str(output),
                    "v9.9.9",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=_SUBPROCESS_TIMEOUT,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("does not match wheel version 0.1.0", completed.stderr)
            self.assertFalse(output.exists())

    def test_offline_artifact_set_reports_every_missing_release_input(self) -> None:
        with self._artifact() as (root, artifact, _release):
            tools = root / "tools"
            tools.mkdir()
            empty = root / "empty"
            empty.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")

            completed = self._run(
                artifact, tools, "--artifact-dir", str(empty), home=root / "home"
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "mastic-bootstrap-closure-0.1.0-macos-arm64.tar.gz",
                completed.stderr,
            )

    def test_empty_artifact_directory_fails_before_network_access(self) -> None:
        with self._artifact() as (root, artifact, _release):
            tools = root / "tools"
            tools.mkdir()
            curl_log = root / "curl.log"
            self._host_tools(tools, machine="arm64", version="15.7")
            self._tool(
                tools / "curl",
                f"print -r -- invoked >{curl_log}\nexit 99",
            )

            completed = self._run(
                artifact,
                tools,
                "--artifact-dir",
                "",
                "--yes",
                home=root / "home",
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "--artifact-dir requires a non-empty value",
                completed.stderr,
            )
            self.assertFalse(curl_log.exists())

    def test_dry_run_validates_supported_host_without_network_or_mutation(self) -> None:
        with self._artifact() as (root, artifact, _release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="26.5")
            self._tool(tools / "curl", "print -ru2 -- 'curl must not run'; exit 99")

            completed = self._run(artifact, tools, "--dry-run")

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Host validated: macOS 26.5 (arm64)", completed.stdout)
            self.assertIn(
                "no files, tools, or network resources were changed", completed.stdout
            )

    def test_wrong_architecture_fails_before_network_access(self) -> None:
        with self._artifact() as (root, artifact, _release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="x86_64", version="15.7")
            self._tool(tools / "curl", "exit 99")

            completed = self._run(artifact, tools, "--dry-run")

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("requires an Apple-silicon Mac", completed.stderr)

    def test_archive_traversal_is_rejected_before_extraction(self) -> None:
        with self._artifact() as (root, _artifact, release):
            closure = release / "mastic-bootstrap-closure-0.1.0-macos-arm64.tar.gz"
            with tarfile.open(closure, "w:gz") as archive:
                member = tarfile.TarInfo("../escaped")
                member.size = len(b"unsafe")
                archive.addfile(member, io.BytesIO(b"unsafe"))
            artifact = root / "unsafe-bootstrap.zsh"
            subprocess.run(
                [
                    "zsh",
                    str(BUILDER),
                    str(release / "mastic-0.1.0-py3-none-any.whl"),
                    str(closure),
                    str(artifact),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")

            completed = self._run(
                artifact, tools, "--artifact-dir", str(release), home=root / "home"
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("unsafe member", completed.stderr)
            self.assertFalse((root / "escaped").exists())

    def test_undeclared_closure_file_fails_the_exact_set_check(self) -> None:
        with self._artifact() as (root, _artifact, release):
            closure = release / "mastic-bootstrap-closure-0.1.0-macos-arm64.tar.gz"
            unpacked = root / "unpacked"
            with tarfile.open(closure, "r:gz") as archive:
                archive.extractall(unpacked, filter="data")
            (unpacked / "undeclared.whl").write_bytes(b"not in SHA256SUMS")
            with tarfile.open(closure, "w:gz") as archive:
                for path in sorted(unpacked.rglob("*")):
                    archive.add(path, arcname=path.relative_to(unpacked))
            artifact = root / "undeclared-bootstrap.zsh"
            subprocess.run(
                [
                    "zsh",
                    str(BUILDER),
                    str(release / "mastic-0.1.0-py3-none-any.whl"),
                    str(closure),
                    str(artifact),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")

            completed = self._run(
                artifact, tools, "--artifact-dir", str(release), home=root / "home"
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("undeclared:undeclared.whl", completed.stderr)

    def test_wheel_digest_failure_aborts_before_install(self) -> None:
        with self._artifact() as (root, artifact, _release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            self._tool(
                tools / "curl",
                "local output=''\nwhile (( $# )); do\n  [[ $1 == --output ]] && { output=$2; break; }\n  shift\ndone\nprint -rn -- tampered >\"$output\"",
            )
            self._tool(tools / "uv", "print -ru2 -- 'uv must not install'; exit 98")

            completed = self._run(artifact, tools)

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("digest verification failed", completed.stderr)
            self.assertNotIn("uv must not install", completed.stderr)

    def test_successful_install_exits_zero_and_removes_its_temporary_directory(
        self,
    ) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            temporary = root / "tmp"
            temporary.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            self._tool(tools / "uv", "print -ru2 -- 'ambient uv ran'; exit 97")
            home = root / "home"
            home.mkdir()
            user_uv = home / ".local/bin/uv"
            user_uv.parent.mkdir(parents=True)
            user_uv.write_bytes(b"user-owned uv")
            user_uv.chmod(0o755)
            uv_log = root / "uv.log"

            with patch.dict(
                os.environ,
                {"TMPDIR": str(temporary), "BOOTSTRAP_UV_LOG": str(uv_log)},
            ):
                completed = self._run(
                    artifact,
                    tools,
                    "--artifact-dir",
                    str(release),
                    home=home,
                )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Installed MASTIC 0.1.0", completed.stdout)
            self.assertNotIn("ambient uv ran", completed.stderr)
            invocation = uv_log.read_text(encoding="utf-8")
            self.assertIn(f"UV_TOOL_BIN_DIR={home / '.local/bin'}", invocation)
            self.assertIn("tool install", invocation)
            self.assertIn("--offline", invocation)
            self.assertIn("--no-index", invocation)
            self.assertIn("--no-python-downloads", invocation)
            self.assertIn("--find-links", invocation)
            self.assertEqual(user_uv.read_bytes(), b"user-owned uv")
            persisted_uv = home / ".local/share/mastic/bootstrap-uv/uv"
            self.assertTrue(persisted_uv.is_file())
            self.assertTrue(os.access(persisted_uv, os.X_OK))
            persisted_python = (
                home / ".local/share/mastic/bootstrap-python/bin/python3.11"
            )
            self.assertTrue(persisted_python.is_file())
            self.assertTrue(os.access(persisted_python, os.X_OK))
            self.assertIn(f"--python {persisted_python}", invocation)
            cached_targets = (
                home / ".local/share/mastic/bootstrap-artifacts/application-targets-v1"
            )
            self.assertEqual(
                (cached_targets / "manifest.json").read_text(encoding="utf-8"),
                '{"schema_version":1}\n',
            )
            self.assertTrue(
                (cached_targets / "artifacts/hindsight-darwin-arm64").is_file()
            )
            receipt_path = cached_targets.parent / "bootstrap-receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["schema_version"], 1)
            self.assertEqual(
                receipt["application_manifest_sha256"],
                hashlib.sha256(
                    (cached_targets / "manifest.json").read_bytes()
                ).hexdigest(),
            )
            self.assertEqual(stat.S_IMODE(receipt_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(cached_targets.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(cached_targets.parent.stat().st_mode), 0o700)
            self.assertEqual(list(temporary.iterdir()), [])

    def test_upgrade_recycles_a_running_supervisor_through_both_generations(
        self,
    ) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            home = root / "home"
            state, lifecycle_log, _old_launcher = self._supervisor_fixture(
                home, tools, running=True
            )
            environment = {
                "BOOTSTRAP_SUPERVISOR_STATE": str(state),
                "BOOTSTRAP_SUPERVISOR_LOG": str(lifecycle_log),
            }

            with patch.dict(os.environ, environment):
                completed = self._run(
                    artifact, tools, "--artifact-dir", str(release), home=home
                )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                lifecycle_log.read_text(encoding="utf-8").splitlines(),
                [
                    "old:supervisor.stop",
                    f"launchctl:bootout:gui/{os.getuid()}/io.nisavid.masticd",
                    "new:supervisor.start",
                ],
            )
            self.assertEqual(state.read_text(encoding="utf-8"), "running\n")

            follow_up = subprocess.run(
                [str(home / ".local/bin/mastic"), "service", "start", "probe"],
                env={**os.environ, **environment},
                capture_output=True,
                text=True,
                check=False,
                timeout=_SUBPROCESS_TIMEOUT,
            )

            self.assertEqual(follow_up.returncode, 0, follow_up.stderr)
            self.assertEqual(
                lifecycle_log.read_text(encoding="utf-8").splitlines()[-1],
                "new:service.start",
            )

    def test_upgrade_accepts_uvs_user_owned_cli_symlink(self) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            home = root / "home"
            state, lifecycle_log, _old_launcher = self._supervisor_fixture(
                home, tools, running=True
            )
            launcher = home / ".local/bin/mastic"
            installed_launcher = home / ".local/share/mastic/tools/mastic/bin/mastic"
            installed_launcher.parent.mkdir(parents=True)
            launcher.replace(installed_launcher)
            launcher.symlink_to(installed_launcher)

            with patch.dict(
                os.environ,
                {
                    "BOOTSTRAP_SUPERVISOR_STATE": str(state),
                    "BOOTSTRAP_SUPERVISOR_LOG": str(lifecycle_log),
                },
            ):
                completed = self._run(
                    artifact, tools, "--artifact-dir", str(release), home=home
                )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                lifecycle_log.read_text(encoding="utf-8").splitlines(),
                [
                    "old:supervisor.stop",
                    f"launchctl:bootout:gui/{os.getuid()}/io.nisavid.masticd",
                    "new:supervisor.start",
                ],
            )
            self.assertEqual(state.read_text(encoding="utf-8"), "running\n")

    def test_upgrade_rejects_cli_symlink_to_another_owners_executable(self) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            home = root / "home"
            state, lifecycle_log, _old_launcher = self._supervisor_fixture(
                home, tools, running=True
            )
            launcher = home / ".local/bin/mastic"
            launcher.unlink()
            launcher.symlink_to("/usr/bin/true")

            with patch.dict(
                os.environ,
                {
                    "BOOTSTRAP_SUPERVISOR_STATE": str(state),
                    "BOOTSTRAP_SUPERVISOR_LOG": str(lifecycle_log),
                },
            ):
                completed = self._run(
                    artifact, tools, "--artifact-dir", str(release), home=home
                )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "installed mastic CLI is unavailable",
                completed.stderr,
            )
            self.assertFalse(lifecycle_log.exists())
            self.assertEqual(state.read_text(encoding="utf-8"), "running\n")

    def test_upgrade_preserves_an_inactive_supervisor(self) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            home = root / "home"
            state, lifecycle_log, _old_launcher = self._supervisor_fixture(
                home, tools, running=False
            )

            with patch.dict(
                os.environ,
                {
                    "BOOTSTRAP_SUPERVISOR_STATE": str(state),
                    "BOOTSTRAP_SUPERVISOR_LOG": str(lifecycle_log),
                },
            ):
                completed = self._run(
                    artifact, tools, "--artifact-dir", str(release), home=home
                )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(state.read_text(encoding="utf-8"), "stopped\n")
            self.assertFalse(lifecycle_log.exists())

    def test_upgrade_holds_the_shared_transition_lock_while_replacing_paths(
        self,
    ) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            home = root / "home"
            state, lifecycle_log, _old_launcher = self._supervisor_fixture(
                home, tools, running=False
            )

            with patch.dict(
                os.environ,
                {
                    "BOOTSTRAP_EXPECT_TRANSITION_LOCK": str(
                        home / ".local/state/.mastic-locks/setup-removal.lock"
                    ),
                    "BOOTSTRAP_SUPERVISOR_STATE": str(state),
                    "BOOTSTRAP_SUPERVISOR_LOG": str(lifecycle_log),
                },
            ):
                completed = self._run(
                    artifact, tools, "--artifact-dir", str(release), home=home
                )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(lifecycle_log.exists())

    def test_superseded_bootstrap_does_not_report_or_leave_its_daemon_running(
        self,
    ) -> None:
        with (
            self._artifact(version="0.1.0", tool_marker="new tool 0.1") as (
                first_root,
                first_artifact,
                first_release,
            ),
            self._artifact(version="0.2.0", tool_marker="new tool 0.2") as (
                _second_root,
                second_artifact,
                second_release,
            ),
        ):
            tools = first_root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            home = first_root / "home"
            state, lifecycle_log, _old_launcher = self._supervisor_fixture(
                home, tools, running=True
            )
            start_entered = first_root / "first-start-entered"
            start_continue = first_root / "first-start-continue"
            start_released = first_root / "first-start-released"
            start_return_continue = first_root / "first-start-return-continue"
            stop_released = first_root / "second-stop-released"
            stop_continue = first_root / "second-stop-continue"
            base_environment = dict(os.environ)
            base_environment["PATH"] = f"{tools}:{base_environment['PATH']}"
            base_environment["HOME"] = str(home)
            base_environment["BOOTSTRAP_SUPERVISOR_STATE"] = str(state)
            base_environment["BOOTSTRAP_SUPERVISOR_LOG"] = str(lifecycle_log)
            base_environment.pop("XDG_DATA_HOME", None)
            first_environment = {
                **base_environment,
                "BOOTSTRAP_NEW_MASTIC_START_ENTERED": str(start_entered),
                "BOOTSTRAP_NEW_MASTIC_START_CONTINUE": str(start_continue),
                "BOOTSTRAP_NEW_MASTIC_START_RELEASED": str(start_released),
                "BOOTSTRAP_NEW_MASTIC_START_RETURN_CONTINUE": str(
                    start_return_continue
                ),
            }
            second_environment = {
                **base_environment,
                "BOOTSTRAP_NEW_MASTIC_STOP_RELEASED": str(stop_released),
                "BOOTSTRAP_NEW_MASTIC_STOP_CONTINUE": str(stop_continue),
            }
            first = subprocess.Popen(
                [
                    str(first_artifact),
                    "--artifact-dir",
                    str(first_release),
                ],
                env=first_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            second_process = None
            try:
                deadline = time.monotonic() + _SUBPROCESS_TIMEOUT
                while not start_entered.exists() and first.poll() is None:
                    if time.monotonic() >= deadline:
                        self.fail("first bootstrap did not reach Supervisor restart")
                    time.sleep(0.02)

                self.assertTrue(start_entered.exists())
                start_continue.touch()
                while not start_released.exists() and first.poll() is None:
                    if time.monotonic() >= deadline:
                        self.fail(
                            "first bootstrap did not release its Supervisor start"
                        )
                    time.sleep(0.02)

                self.assertTrue(start_released.exists())
                second_process = subprocess.Popen(
                    [
                        str(second_artifact),
                        "--artifact-dir",
                        str(second_release),
                    ],
                    env=second_environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                while not stop_released.exists() and second_process.poll() is None:
                    if time.monotonic() >= deadline:
                        self.fail("second bootstrap did not drain the first Supervisor")
                    time.sleep(0.02)

                self.assertTrue(stop_released.exists())
                start_return_continue.touch()
                stop_continue.touch()
                second_stdout, second_stderr = second_process.communicate(
                    timeout=_SUBPROCESS_TIMEOUT
                )
                first_stdout, first_stderr = first.communicate(
                    timeout=_SUBPROCESS_TIMEOUT
                )
            finally:
                start_continue.touch()
                start_return_continue.touch()
                stop_continue.touch()
                if first.poll() is None:
                    try:
                        first.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        first.kill()
                        first.wait()
                if second_process is not None and second_process.poll() is None:
                    try:
                        second_process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        second_process.kill()
                        second_process.wait()

            self.assertEqual(second_process.returncode, 0, second_stderr)
            self.assertIn("Installed MASTIC 0.2.0", second_stdout)
            self.assertNotEqual(first.returncode, 0)
            self.assertNotIn("Installed MASTIC 0.1.0", first_stdout)
            self.assertTrue(
                "another installation replaced the exact MASTIC generation"
                in first_stderr
                or "the restarted Supervisor was not running at commit" in first_stderr,
                first_stderr,
            )
            receipt = json.loads(
                (
                    home
                    / ".local/share/mastic/bootstrap-artifacts/bootstrap-receipt.json"
                ).read_text()
            )
            self.assertEqual(receipt["version"], "0.2.0")
            self.assertEqual(
                (home / ".local/share/mastic/tools/mastic/release.txt").read_text(),
                "new tool 0.2",
            )
            self.assertEqual(state.read_text(encoding="utf-8"), "running\n")

    def test_cleanup_does_not_rollback_a_generation_installed_after_stop(
        self,
    ) -> None:
        with (
            self._artifact(version="0.1.0", tool_marker="new tool 0.1") as (
                first_root,
                first_artifact,
                first_release,
            ),
            self._artifact(version="0.2.0", tool_marker="new tool 0.2") as (
                _second_root,
                second_artifact,
                second_release,
            ),
        ):
            tools = first_root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            home = first_root / "home"
            state, lifecycle_log, _old_launcher = self._supervisor_fixture(
                home, tools, running=True
            )
            commit_inspection = first_root / "commit-inspection"
            stop_released = first_root / "stop-released"
            stop_continue = first_root / "stop-continue"
            base_environment = dict(os.environ)
            base_environment["PATH"] = f"{tools}:{base_environment['PATH']}"
            base_environment["HOME"] = str(home)
            base_environment["BOOTSTRAP_SUPERVISOR_STATE"] = str(state)
            base_environment["BOOTSTRAP_SUPERVISOR_LOG"] = str(lifecycle_log)
            base_environment.pop("XDG_DATA_HOME", None)
            first_environment = {
                **base_environment,
                "BOOTSTRAP_NEW_MASTIC_START_SUCCESS_MARKER": str(commit_inspection),
                "BOOTSTRAP_LAUNCHCTL_PRINT_FAIL_ONCE_MARKER": str(commit_inspection),
                "BOOTSTRAP_NEW_MASTIC_STOP_RELEASED": str(stop_released),
                "BOOTSTRAP_NEW_MASTIC_STOP_CONTINUE": str(stop_continue),
            }
            first = subprocess.Popen(
                [
                    str(first_artifact),
                    "--artifact-dir",
                    str(first_release),
                ],
                env=first_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + _SUBPROCESS_TIMEOUT
                while not stop_released.exists() and first.poll() is None:
                    if time.monotonic() >= deadline:
                        self.fail("first bootstrap did not release its cleanup stop")
                    time.sleep(0.02)

                second = subprocess.run(
                    [
                        str(second_artifact),
                        "--artifact-dir",
                        str(second_release),
                    ],
                    env=base_environment,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=_SUBPROCESS_TIMEOUT,
                )
                stop_continue.touch()
                first_stdout, first_stderr = first.communicate(
                    timeout=_SUBPROCESS_TIMEOUT
                )
            finally:
                if first.poll() is None:
                    first.kill()
                    first.wait()

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotEqual(first.returncode, 0)
            self.assertNotIn("Installed MASTIC 0.1.0", first_stdout)
            self.assertIn(
                "another installation replaced the expected MASTIC generation",
                first_stderr,
            )
            receipt = json.loads(
                (
                    home
                    / ".local/share/mastic/bootstrap-artifacts/bootstrap-receipt.json"
                ).read_text()
            )
            self.assertEqual(receipt["version"], "0.2.0")
            self.assertEqual(
                (home / ".local/share/mastic/tools/mastic/release.txt").read_text(),
                "new tool 0.2",
            )
            self.assertEqual(state.read_text(encoding="utf-8"), "stopped\n")

    def test_launchd_inspection_failure_aborts_before_replacing_the_release(
        self,
    ) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            home = root / "home"
            state, lifecycle_log, old_launcher = self._supervisor_fixture(
                home, tools, running=True
            )

            with patch.dict(
                os.environ,
                {
                    "BOOTSTRAP_LAUNCHCTL_PRINT_FAIL": "1",
                    "BOOTSTRAP_SUPERVISOR_STATE": str(state),
                    "BOOTSTRAP_SUPERVISOR_LOG": str(lifecycle_log),
                },
            ):
                completed = self._run(
                    artifact, tools, "--artifact-dir", str(release), home=home
                )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("could not inspect the Supervisor", completed.stderr)
            self.assertNotIn("Installed MASTIC", completed.stdout)
            self.assertFalse(lifecycle_log.exists())
            self.assertEqual(state.read_text(encoding="utf-8"), "running\n")
            self.assertEqual(
                (home / ".local/bin/mastic").read_bytes(),
                old_launcher,
            )

    def test_termination_during_drain_recovers_the_previous_supervisor(self) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            home = root / "home"
            state, lifecycle_log, old_launcher = self._supervisor_fixture(
                home, tools, running=True
            )

            with patch.dict(
                os.environ,
                {
                    "BOOTSTRAP_OLD_MASTIC_TERM_AFTER_STOP": "1",
                    "BOOTSTRAP_OLD_MASTIC_TERM_MARKER": str(
                        root / "old-mastic-term-marker"
                    ),
                    "BOOTSTRAP_SUPERVISOR_STATE": str(state),
                    "BOOTSTRAP_SUPERVISOR_LOG": str(lifecycle_log),
                },
            ):
                completed = self._run(
                    artifact, tools, "--artifact-dir", str(release), home=home
                )

            self.assertEqual(completed.returncode, 143, completed.stderr)
            self.assertEqual(
                lifecycle_log.read_text(encoding="utf-8").splitlines(),
                [
                    "old:supervisor.stop",
                    f"launchctl:bootout:gui/{os.getuid()}/io.nisavid.masticd",
                    "old:supervisor.start",
                ],
            )
            self.assertEqual(state.read_text(encoding="utf-8"), "running\n")
            self.assertEqual(
                (home / ".local/bin/mastic").read_bytes(),
                old_launcher,
            )

    def test_drain_failure_aborts_before_replacing_the_installed_release(self) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            home = root / "home"
            state, lifecycle_log, old_launcher = self._supervisor_fixture(
                home, tools, running=True
            )

            with patch.dict(
                os.environ,
                {
                    "BOOTSTRAP_OLD_MASTIC_STOP_FAIL": "1",
                    "BOOTSTRAP_SUPERVISOR_STATE": str(state),
                    "BOOTSTRAP_SUPERVISOR_LOG": str(lifecycle_log),
                },
            ):
                completed = self._run(
                    artifact, tools, "--artifact-dir", str(release), home=home
                )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "could not be drained before installation",
                completed.stderr,
            )
            self.assertNotIn("Installed MASTIC", completed.stdout)
            self.assertEqual(
                lifecycle_log.read_text(encoding="utf-8").splitlines(),
                ["old:supervisor.stop"],
            )
            self.assertEqual(state.read_text(encoding="utf-8"), "running\n")
            self.assertEqual(
                (home / ".local/bin/mastic").read_bytes(),
                old_launcher,
            )
            self.assertEqual(
                (home / ".local/share/mastic/tools/mastic/release.txt").read_bytes(),
                b"old tool",
            )

    def test_unregister_failure_recovers_without_replacing_the_release(self) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            home = root / "home"
            state, lifecycle_log, old_launcher = self._supervisor_fixture(
                home, tools, running=True
            )

            with patch.dict(
                os.environ,
                {
                    "BOOTSTRAP_LAUNCHCTL_BOOTOUT_FAIL": "1",
                    "BOOTSTRAP_SUPERVISOR_STATE": str(state),
                    "BOOTSTRAP_SUPERVISOR_LOG": str(lifecycle_log),
                },
            ):
                completed = self._run(
                    artifact, tools, "--artifact-dir", str(release), home=home
                )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "could not be unregistered before installation",
                completed.stderr,
            )
            self.assertNotIn("Installed MASTIC", completed.stdout)
            self.assertEqual(
                lifecycle_log.read_text(encoding="utf-8").splitlines(),
                [
                    "old:supervisor.stop",
                    f"launchctl:bootout:gui/{os.getuid()}/io.nisavid.masticd",
                    "old:supervisor.start",
                ],
            )
            self.assertEqual(state.read_text(encoding="utf-8"), "running\n")
            self.assertEqual(
                (home / ".local/bin/mastic").read_bytes(),
                old_launcher,
            )

    def test_incomplete_rollback_does_not_restart_a_mixed_release(self) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            self._tool(
                tools / "rm",
                '[[ "$*" == *"/tools/mastic"* ]] && exit 98\nexec /bin/rm "$@"',
            )
            home = root / "home"
            state, lifecycle_log, _old_launcher = self._supervisor_fixture(
                home, tools, running=True
            )

            with patch.dict(
                os.environ,
                {
                    "BOOTSTRAP_NEW_MASTIC_START_FAIL": "1",
                    "BOOTSTRAP_SUPERVISOR_STATE": str(state),
                    "BOOTSTRAP_SUPERVISOR_LOG": str(lifecycle_log),
                },
            ):
                completed = self._run(
                    artifact, tools, "--artifact-dir", str(release), home=home
                )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "rollback was incomplete; the Supervisor remains stopped",
                completed.stderr,
            )
            self.assertNotIn("old:supervisor.start", lifecycle_log.read_text())
            self.assertEqual(state.read_text(encoding="utf-8"), "absent\n")

    def test_unknown_state_retains_the_exact_release_and_recovery_backups(
        self,
    ) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            home = root / "home"
            state, lifecycle_log, _old_launcher = self._supervisor_fixture(
                home, tools, running=True
            )
            inspection_failure = root / "launchctl-print-failure"

            with patch.dict(
                os.environ,
                {
                    "BOOTSTRAP_NEW_MASTIC_START_FAIL": "1",
                    "BOOTSTRAP_NEW_MASTIC_START_FAILURE_MARKER": str(
                        inspection_failure
                    ),
                    "BOOTSTRAP_LAUNCHCTL_PRINT_FAIL_MARKER": str(inspection_failure),
                    "BOOTSTRAP_SUPERVISOR_STATE": str(state),
                    "BOOTSTRAP_SUPERVISOR_LOG": str(lifecycle_log),
                },
            ):
                completed = self._run(
                    artifact, tools, "--artifact-dir", str(release), home=home
                )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "the exact installed release and recovery backups were retained",
                completed.stderr,
            )
            self.assertNotIn("old:supervisor.start", lifecycle_log.read_text())
            self.assertEqual(
                (home / ".local/share/mastic/tools/mastic/release.txt").read_bytes(),
                b"new tool",
            )
            self.assertGreater(
                len(list((home / ".local").rglob(".*.mastic-backup.*"))),
                0,
            )

    def test_commit_inspection_failure_quiesces_before_rollback(self) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            home = root / "home"
            state, lifecycle_log, old_launcher = self._supervisor_fixture(
                home, tools, running=True
            )
            inspection_failure = root / "commit-inspection-failure"

            with patch.dict(
                os.environ,
                {
                    "BOOTSTRAP_NEW_MASTIC_START_SUCCESS_MARKER": str(
                        inspection_failure
                    ),
                    "BOOTSTRAP_LAUNCHCTL_PRINT_FAIL_ONCE_MARKER": str(
                        inspection_failure
                    ),
                    "BOOTSTRAP_SUPERVISOR_STATE": str(state),
                    "BOOTSTRAP_SUPERVISOR_LOG": str(lifecycle_log),
                },
            ):
                completed = self._run(
                    artifact, tools, "--artifact-dir", str(release), home=home
                )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "could not inspect the restarted Supervisor before commit",
                completed.stderr,
            )
            self.assertEqual(
                lifecycle_log.read_text(encoding="utf-8").splitlines(),
                [
                    "old:supervisor.stop",
                    f"launchctl:bootout:gui/{os.getuid()}/io.nisavid.masticd",
                    "new:supervisor.start",
                    "new:supervisor.stop",
                    f"launchctl:bootout:gui/{os.getuid()}/io.nisavid.masticd",
                    "old:supervisor.start",
                ],
            )
            self.assertEqual(state.read_text(encoding="utf-8"), "running\n")
            self.assertEqual(
                (home / ".local/bin/mastic").read_bytes(),
                old_launcher,
            )

    def test_post_restart_lock_failure_retains_new_files_after_quiescing(self) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            home = root / "home"
            state, lifecycle_log, _old_launcher = self._supervisor_fixture(
                home, tools, running=True
            )
            lock_path = home / ".local/state/.mastic-locks/setup-removal.lock"

            with patch.dict(
                os.environ,
                {
                    "BOOTSTRAP_NEW_MASTIC_CORRUPT_LOCK": str(lock_path),
                    "BOOTSTRAP_SUPERVISOR_STATE": str(state),
                    "BOOTSTRAP_SUPERVISOR_LOG": str(lifecycle_log),
                },
            ):
                completed = self._run(
                    artifact, tools, "--artifact-dir", str(release), home=home
                )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "transition lock must be a user-owned regular file",
                completed.stderr,
            )
            self.assertIn(
                "the exact installed release and recovery backups were retained",
                completed.stderr,
            )
            self.assertNotIn("old:supervisor.start", lifecycle_log.read_text())
            self.assertNotIn("new:supervisor.stop", lifecycle_log.read_text())
            self.assertEqual(state.read_text(encoding="utf-8"), "running\n")
            self.assertEqual(
                (home / ".local/share/mastic/tools/mastic/release.txt").read_bytes(),
                b"new tool",
            )

    def test_restart_failure_rolls_back_and_recovers_the_running_supervisor(
        self,
    ) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            home = root / "home"
            state, lifecycle_log, old_launcher = self._supervisor_fixture(
                home, tools, running=True
            )

            with patch.dict(
                os.environ,
                {
                    "BOOTSTRAP_NEW_MASTIC_START_FAIL": "1",
                    "BOOTSTRAP_SUPERVISOR_STATE": str(state),
                    "BOOTSTRAP_SUPERVISOR_LOG": str(lifecycle_log),
                },
            ):
                completed = self._run(
                    artifact, tools, "--artifact-dir", str(release), home=home
                )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("could not restart the running Supervisor", completed.stderr)
            self.assertNotIn("Installed MASTIC", completed.stdout)
            self.assertEqual(
                lifecycle_log.read_text(encoding="utf-8").splitlines(),
                [
                    "old:supervisor.stop",
                    f"launchctl:bootout:gui/{os.getuid()}/io.nisavid.masticd",
                    "new:supervisor.start",
                    f"launchctl:bootout:gui/{os.getuid()}/io.nisavid.masticd",
                    "old:supervisor.start",
                ],
            )
            self.assertEqual(state.read_text(encoding="utf-8"), "running\n")
            self.assertEqual(
                (home / ".local/bin/mastic").read_bytes(),
                old_launcher,
            )

    def test_verified_closure_quarantine_is_removed_without_erasing_other_metadata(
        self,
    ) -> None:
        with self._artifact() as (root, artifact, release):
            quarantine = b"0281;6a5fa391;;"
            preserved = b"\x00preserve-me\n"
            closure_root = root / "closure"
            manifest = closure_root / "application-targets-v1/manifest.json"
            nested_artifact = (
                closure_root / "application-targets-v1/artifacts/hindsight-darwin-arm64"
            )
            self._set_xattr(manifest, "com.apple.quarantine", quarantine)
            self._set_xattr(nested_artifact, "com.apple.quarantine", quarantine)
            self._set_xattr(manifest, "io.nisavid.mastic.test", preserved)
            closure = release / "mastic-bootstrap-closure-0.1.0-macos-arm64.tar.gz"
            subprocess.run(
                ["/usr/bin/tar", "-czf", str(closure), "-C", str(closure_root), "."],
                check=True,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
            subprocess.run(
                [
                    "zsh",
                    str(BUILDER),
                    str(release / "mastic-0.1.0-py3-none-any.whl"),
                    str(closure),
                    str(artifact),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
            self._set_xattr(closure, "com.apple.quarantine", quarantine)
            closure_bytes = closure.read_bytes()
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            home = root / "home"

            completed = self._run(
                artifact, tools, "--artifact-dir", str(release), home=home
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            persisted_manifest = (
                home
                / ".local/share/mastic/bootstrap-artifacts/application-targets-v1/manifest.json"
            )
            self.assertIsNone(
                self._get_xattr(persisted_manifest, "com.apple.quarantine")
            )
            self.assertEqual(
                self._get_xattr(persisted_manifest, "io.nisavid.mastic.test"),
                preserved,
            )
            persisted_artifact = (
                home
                / ".local/share/mastic/bootstrap-artifacts/application-targets-v1/artifacts/hindsight-darwin-arm64"
            )
            self.assertIsNone(
                self._get_xattr(persisted_artifact, "com.apple.quarantine")
            )
            self.assertEqual(closure.read_bytes(), closure_bytes)
            self.assertEqual(
                self._get_xattr(closure, "com.apple.quarantine"), quarantine
            )

    def test_failed_closure_verification_does_not_remove_quarantine(self) -> None:
        with self._artifact() as (root, _artifact, release):
            quarantine = b"0281;6a5fa391;;"
            closure_root = root / "closure"
            manifest = closure_root / "application-targets-v1/manifest.json"
            nested_artifact = (
                closure_root / "application-targets-v1/artifacts/hindsight-darwin-arm64"
            )
            self._set_xattr(manifest, "com.apple.quarantine", quarantine)
            self._set_xattr(nested_artifact, "com.apple.quarantine", quarantine)
            nested_artifact.write_bytes(b"tampered after the internal manifest")

            closure = release / "mastic-bootstrap-closure-0.1.0-macos-arm64.tar.gz"
            subprocess.run(
                ["/usr/bin/tar", "-czf", str(closure), "-C", str(closure_root), "."],
                check=True,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )
            artifact = root / "invalid-closure-bootstrap.zsh"
            subprocess.run(
                [
                    "zsh",
                    str(BUILDER),
                    str(release / "mastic-0.1.0-py3-none-any.whl"),
                    str(closure),
                    str(artifact),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT,
            )

            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            self._tool(tools / "rm", "exit 0")
            temporary = root / "temporary"
            temporary.mkdir()
            with patch.dict(os.environ, {"TMPDIR": str(temporary)}):
                completed = self._run(
                    artifact, tools, "--artifact-dir", str(release), home=root / "home"
                )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("closure digest mismatches", completed.stderr)
            work_directories = list(temporary.glob("mastic-bootstrap.*"))
            self.assertEqual(len(work_directories), 1)
            failed_closure = work_directories[0] / "closure"
            self.assertIsNotNone(
                self._get_xattr(
                    failed_closure / "application-targets-v1/manifest.json",
                    "com.apple.quarantine",
                )
            )
            self.assertIsNotNone(
                self._get_xattr(
                    failed_closure
                    / "application-targets-v1/artifacts/hindsight-darwin-arm64",
                    "com.apple.quarantine",
                )
            )

    def test_install_honors_explicit_mastic_data_directory(self) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            home = root / "home"
            home.mkdir()
            data_dir = root / "custom-mastic-data"
            uv_log = root / "uv.log"

            with patch.dict(
                os.environ,
                {
                    "BOOTSTRAP_UV_LOG": str(uv_log),
                    "MASTIC_DATA_DIR": str(data_dir),
                },
            ):
                completed = self._run(
                    artifact,
                    tools,
                    "--artifact-dir",
                    str(release),
                    home=home,
                )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((data_dir / "bootstrap-uv/uv").is_file())
            self.assertTrue((data_dir / "bootstrap-python/bin/python3.11").is_file())
            self.assertTrue(
                (
                    data_dir
                    / "bootstrap-artifacts/application-targets-v1/manifest.json"
                ).is_file()
            )
            invocation = uv_log.read_text(encoding="utf-8")
            self.assertIn(
                f"--python {data_dir / 'bootstrap-python/bin/python3.11'}", invocation
            )
            self.assertFalse((home / ".local/share/mastic").exists())

    def test_termination_exits_and_removes_the_temporary_directory(self) -> None:
        with self._artifact() as (root, artifact, _release):
            tools = root / "tools"
            tools.mkdir()
            temporary = root / "tmp"
            temporary.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            self._tool(tools / "curl", "kill -TERM $PPID\nsleep 1")
            self._tool(tools / "uv", "exit 0")

            with patch.dict(os.environ, {"TMPDIR": str(temporary)}):
                completed = self._run(artifact, tools)

            self.assertEqual(completed.returncode, 143, completed.stderr)
            self.assertEqual(list(temporary.iterdir()), [])

    def test_failed_cache_swap_restores_the_existing_verified_cache(self) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            self._tool(
                tools / "mv",
                "if [[ $1 == *.application-targets-v1.mastic-bootstrap.* ]]; then\n"
                "  exit 91\n"
                "fi\n"
                'exec /bin/mv "$@"',
            )
            home = root / "home"
            previous_uv = home / ".local/share/mastic/bootstrap-uv/uv"
            previous_uv.parent.mkdir(parents=True)
            previous_uv.write_bytes(b"existing verified uv")
            previous_python = (
                home / ".local/share/mastic/bootstrap-python/bin/python3.11"
            )
            previous_python.parent.mkdir(parents=True)
            previous_python.write_bytes(b"existing verified python")
            existing = (
                home / ".local/share/mastic/bootstrap-artifacts/application-targets-v1"
            )
            existing.mkdir(parents=True)
            (existing / "manifest.json").write_text("existing\n", encoding="utf-8")

            completed = self._run(
                artifact, tools, "--artifact-dir", str(release), home=home
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("could not replace destination", completed.stderr)
            self.assertEqual(
                (existing / "manifest.json").read_text(encoding="utf-8"),
                "existing\n",
            )
            self.assertEqual(previous_uv.read_bytes(), b"existing verified uv")
            self.assertEqual(previous_python.read_bytes(), b"existing verified python")

    def test_failed_final_install_restores_the_entire_previous_release(self) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            home = root / "home"
            data = home / ".local/share/mastic"
            previous = {
                data / "bootstrap-uv/uv": b"previous uv",
                data / "bootstrap-python/bin/python3.11": b"previous python",
                data
                / "bootstrap-artifacts/application-targets-v1/manifest.json": b"previous manifest",
                data
                / "bootstrap-artifacts/bootstrap-receipt.json": b"previous receipt",
                data / "tools/mastic/release.txt": b"previous tool",
                home / ".local/bin/mastic": b"previous mastic launcher",
                home / ".local/bin/masticd": b"previous masticd launcher",
            }
            for path, content in previous.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            uv_log = root / "uv.log"

            with patch.dict(
                os.environ,
                {
                    "BOOTSTRAP_UV_FAIL_AFTER_MUTATION": "1",
                    "BOOTSTRAP_UV_LOG": str(uv_log),
                },
            ):
                completed = self._run(
                    artifact, tools, "--artifact-dir", str(release), home=home
                )

            self.assertNotEqual(completed.returncode, 0)
            for path, content in previous.items():
                self.assertEqual(path.read_bytes(), content)
            self.assertEqual(list(data.rglob(".*.mastic-backup.*")), [])
            self.assertEqual(list(data.rglob(".*.mastic-bootstrap.*")), [])

    def test_termination_after_tool_mutation_restores_the_previous_tool_release(
        self,
    ) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            home = root / "home"
            data = home / ".local/share/mastic"
            previous = {
                data / "tools/mastic/release.txt": b"previous tool",
                home / ".local/bin/mastic": b"previous mastic launcher",
                home / ".local/bin/masticd": b"previous masticd launcher",
            }
            for path, content in previous.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

            with patch.dict(
                os.environ,
                {
                    "BOOTSTRAP_UV_LOG": str(root / "uv.log"),
                    "BOOTSTRAP_UV_TERM_AFTER_MUTATION": "1",
                },
            ):
                completed = self._run(
                    artifact, tools, "--artifact-dir", str(release), home=home
                )

            self.assertEqual(completed.returncode, 143, completed.stderr)
            for path, content in previous.items():
                self.assertEqual(path.read_bytes(), content)
            self.assertEqual(list(data.rglob(".*.mastic-backup.*")), [])
            self.assertEqual(list(data.rglob(".*.mastic-bootstrap.*")), [])

    def test_termination_during_swap_restores_destination_and_cleans_staging(
        self,
    ) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            self._tool(
                tools / "mv",
                "if [[ $1 == */bootstrap-uv && $2 == *.bootstrap-uv.mastic-backup.* ]]; then\n"
                '  /bin/mv "$@"\n'
                "  kill -TERM $PPID\n"
                "  sleep 1\n"
                "fi\n"
                'exec /bin/mv "$@"',
            )
            home = root / "home"
            destination = home / ".local/share/mastic/bootstrap-uv"
            destination.mkdir(parents=True)
            (destination / "uv").write_bytes(b"existing verified uv")

            completed = self._run(
                artifact, tools, "--artifact-dir", str(release), home=home
            )

            self.assertEqual(completed.returncode, 143, completed.stderr)
            self.assertTrue(destination.is_dir(), completed.stderr)
            self.assertEqual((destination / "uv").read_bytes(), b"existing verified uv")
            self.assertEqual(
                list(destination.parent.glob(".bootstrap-uv.mastic-*")), []
            )

    def test_termination_during_swap_copy_removes_partial_stage(self) -> None:
        with self._artifact() as (root, artifact, release):
            tools = root / "tools"
            tools.mkdir()
            self._host_tools(tools, machine="arm64", version="15.7")
            self._tool(
                tools / "cp",
                "if [[ $3 == *.bootstrap-uv.mastic-bootstrap.* ]]; then\n"
                '  mkdir -p -- "$3"\n'
                '  print -rn -- partial >"$3/partial"\n'
                "  kill -TERM $PPID\n"
                "  sleep 1\n"
                "fi\n"
                'exec /bin/cp "$@"',
            )
            home = root / "home"

            completed = self._run(
                artifact, tools, "--artifact-dir", str(release), home=home
            )

            self.assertEqual(completed.returncode, 143, completed.stderr)
            parent = home / ".local/share/mastic"
            self.assertEqual(list(parent.glob(".bootstrap-uv.mastic-*")), [])

    def _artifact(self, *, version: str = "0.1.0", tool_marker: str = "new tool"):
        return _ArtifactFixture(version=version, tool_marker=tool_marker)

    def _run(
        self,
        artifact: Path,
        tools: Path,
        *arguments: str,
        home: Path | None = None,
    ):
        environment = dict(os.environ)
        environment["PATH"] = f"{tools}:{environment['PATH']}"
        environment.pop("XDG_DATA_HOME", None)
        scoped_home = home if home is not None else artifact.parent / "home"
        scoped_home.mkdir(parents=True, exist_ok=True)
        environment["HOME"] = str(scoped_home)
        return subprocess.run(
            [str(artifact), *arguments],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=_SUBPROCESS_TIMEOUT,
        )

    def _host_tools(self, tools: Path, *, machine: str, version: str) -> None:
        self._tool(
            tools / "uname",
            f"[[ $1 == -s ]] && print -r -- Darwin || print -r -- {machine}",
        )
        self._tool(tools / "sw_vers", f"print -r -- {version}")

    def _supervisor_fixture(
        self, home: Path, tools: Path, *, running: bool
    ) -> tuple[Path, Path, bytes]:
        state = home / "supervisor-state"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("running\n" if running else "stopped\n", encoding="utf-8")
        lifecycle_log = home / "supervisor-lifecycle.log"
        self._tool(
            tools / "launchctl",
            "if [[ $1 == print ]]; then\n"
            "  [[ -n ${BOOTSTRAP_LAUNCHCTL_PRINT_FAIL:-} ]] && exit 93\n"
            "  if [[ -n ${BOOTSTRAP_LAUNCHCTL_PRINT_FAIL_ONCE_MARKER:-} "
            "&& -e $BOOTSTRAP_LAUNCHCTL_PRINT_FAIL_ONCE_MARKER ]]; then\n"
            "    integer remaining="
            '$(<"$BOOTSTRAP_LAUNCHCTL_PRINT_FAIL_ONCE_MARKER")\n'
            "    if (( remaining == 0 )); then\n"
            '      /bin/rm -f "$BOOTSTRAP_LAUNCHCTL_PRINT_FAIL_ONCE_MARKER"\n'
            "      exit 93\n"
            "    fi\n"
            "    print -r -- $(( remaining - 1 )) "
            '>"$BOOTSTRAP_LAUNCHCTL_PRINT_FAIL_ONCE_MARKER"\n'
            "  fi\n"
            "  [[ -n ${BOOTSTRAP_LAUNCHCTL_PRINT_FAIL_MARKER:-} "
            "&& -e $BOOTSTRAP_LAUNCHCTL_PRINT_FAIL_MARKER ]] && exit 93\n"
            '  if [[ $(<"$BOOTSTRAP_SUPERVISOR_STATE") == absent ]]; then\n'
            "    exit 113\n"
            '  elif [[ $(<"$BOOTSTRAP_SUPERVISOR_STATE") == running ]]; then\n'
            "    print -r -- $'state = running\\npid = 123'\n"
            "  else\n"
            "    print -r -- 'state = exited'\n"
            "  fi\n"
            "  exit 0\n"
            "elif [[ $1 == bootout ]]; then\n"
            '  print -r -- "launchctl:bootout:$2" '
            '>>"$BOOTSTRAP_SUPERVISOR_LOG"\n'
            "  [[ -n ${BOOTSTRAP_LAUNCHCTL_BOOTOUT_FAIL:-} ]] && exit 92\n"
            '  print -r -- absent >"$BOOTSTRAP_SUPERVISOR_STATE"\n'
            "  exit 0\n"
            "fi\n"
            "exit 91",
        )
        old_mastic = home / ".local/bin/mastic"
        self._tool(
            old_mastic,
            'print -r -- "old:$1.$2" >>"$BOOTSTRAP_SUPERVISOR_LOG"\n'
            "if [[ $1 == supervisor && $2 == stop ]]; then\n"
            "  [[ -n ${BOOTSTRAP_OLD_MASTIC_STOP_FAIL:-} ]] && exit 77\n"
            '  print -r -- stopped >"$BOOTSTRAP_SUPERVISOR_STATE"\n'
            "  if [[ -n ${BOOTSTRAP_OLD_MASTIC_TERM_AFTER_STOP:-} "
            "&& ! -e $BOOTSTRAP_OLD_MASTIC_TERM_MARKER ]]; then\n"
            '    : >"$BOOTSTRAP_OLD_MASTIC_TERM_MARKER"\n'
            "    kill -TERM $PPID\n"
            "  fi\n"
            "elif [[ $1 == supervisor && $2 == start ]]; then\n"
            '  print -r -- running >"$BOOTSTRAP_SUPERVISOR_STATE"\n'
            "fi",
        )
        old_masticd = home / ".local/bin/masticd"
        self._tool(old_masticd, "exit 0")
        old_tool = home / ".local/share/mastic/tools/mastic/release.txt"
        old_tool.parent.mkdir(parents=True)
        old_tool.write_bytes(b"old tool")
        return state, lifecycle_log, old_mastic.read_bytes()

    def _tool(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/usr/bin/env zsh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)

    def _set_xattr(self, path: Path, name: str, value: bytes) -> None:
        subprocess.run(
            ["/usr/bin/xattr", "-w", "-x", name, value.hex(), str(path)],
            check=True,
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )

    def _get_xattr(self, path: Path, name: str) -> bytes | None:
        completed = subprocess.run(
            ["/usr/bin/xattr", "-p", "-x", name, str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        return bytes.fromhex(completed.stdout) if completed.returncode == 0 else None


class _ArtifactFixture:
    def __init__(self, *, version: str, tool_marker: str) -> None:
        self._version = version
        self._tool_marker = tool_marker

    def __enter__(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.__enter__())
        wheel = root / f"mastic-{self._version}-py3-none-any.whl"
        wheel.write_bytes(b"trusted wheel")
        release = root / "release"
        release.mkdir()
        release_wheel = release / wheel.name
        release_wheel.write_bytes(wheel.read_bytes())
        closure = (
            release / f"mastic-bootstrap-closure-{self._version}-macos-arm64.tar.gz"
        )
        closure_root = root / "closure"
        (closure_root / "uv").mkdir(parents=True)
        (closure_root / "python/bin").mkdir(parents=True)
        (closure_root / "wheels").mkdir(parents=True)
        (closure_root / "application-targets-v1/artifacts").mkdir(parents=True)
        uv = closure_root / "uv/uv"
        uv.write_text(
            "#!/bin/zsh\n"
            "[[ -n ${BOOTSTRAP_UV_FAIL:-} ]] && exit 97\n"
            'print -r -- "UV_TOOL_BIN_DIR=$UV_TOOL_BIN_DIR" '
            '"$*" >"${BOOTSTRAP_UV_LOG:-/dev/null}"\n'
            'mkdir -p -- "$UV_TOOL_DIR/mastic" "$UV_TOOL_BIN_DIR"\n'
            f'print -rn -- "{self._tool_marker}" '
            '>"$UV_TOOL_DIR/mastic/release.txt"\n'
            "cat >\"$UV_TOOL_BIN_DIR/mastic\" <<'MASTIC'\n"
            "#!/bin/zsh\n"
            "typeset -gi bootstrap_fence_fd=-1\n"
            "if [[ -n ${MASTIC_BOOTSTRAP_EXPECTED_RECEIPT_SHA256:-} ]]; then\n"
            "  zmodload zsh/system || exit 90\n"
            '  bootstrap_fence="$HOME/.local/state/.mastic-locks/setup-removal.lock"\n'
            "  zsystem flock -t 30 -f bootstrap_fence_fd "
            '"$bootstrap_fence" || exit 91\n'
            "  bootstrap_receipt="
            '"${MASTIC_DATA_DIR:-$HOME/.local/share/mastic}/bootstrap-artifacts/bootstrap-receipt.json"\n'
            '  bootstrap_actual=$(shasum -a 256 "$bootstrap_receipt") || exit 92\n'
            "  bootstrap_actual=${bootstrap_actual%% *}\n"
            "  if [[ $bootstrap_actual != "
            "$MASTIC_BOOTSTRAP_EXPECTED_RECEIPT_SHA256 ]]; then\n"
            "    zsystem flock -u $bootstrap_fence_fd\n"
            "    bootstrap_fence_fd=-1\n"
            "    print -ru2 -- 'Another installation replaced this MASTIC generation.'\n"
            "    exit 89\n"
            "  fi\n"
            "fi\n"
            'print -r -- "new:$1.$2" >>"${BOOTSTRAP_SUPERVISOR_LOG:-/dev/null}"\n'
            "if [[ $1 == supervisor && $2 == start ]]; then\n"
            "  if [[ -n ${BOOTSTRAP_NEW_MASTIC_START_ENTERED:-} ]]; then\n"
            '    : >"$BOOTSTRAP_NEW_MASTIC_START_ENTERED"\n'
            "    while [[ ! -e $BOOTSTRAP_NEW_MASTIC_START_CONTINUE ]]; do\n"
            "      sleep 0.05\n"
            "    done\n"
            "  fi\n"
            "  if [[ -n ${BOOTSTRAP_NEW_MASTIC_START_FAIL:-} ]]; then\n"
            '    print -r -- stopped >"$BOOTSTRAP_SUPERVISOR_STATE"\n'
            "    [[ -z ${BOOTSTRAP_NEW_MASTIC_START_FAILURE_MARKER:-} ]] "
            '|| : >"$BOOTSTRAP_NEW_MASTIC_START_FAILURE_MARKER"\n'
            "    exit 88\n"
            "  fi\n"
            '  print -r -- running >"$BOOTSTRAP_SUPERVISOR_STATE"\n'
            "  [[ -z ${BOOTSTRAP_NEW_MASTIC_START_SUCCESS_MARKER:-} ]] "
            '|| print -r -- 1 >"$BOOTSTRAP_NEW_MASTIC_START_SUCCESS_MARKER"\n'
            "  if [[ -n ${BOOTSTRAP_NEW_MASTIC_CORRUPT_LOCK:-} ]]; then\n"
            '    /bin/rm -f "$BOOTSTRAP_NEW_MASTIC_CORRUPT_LOCK"\n'
            '    /bin/ln -s /dev/null "$BOOTSTRAP_NEW_MASTIC_CORRUPT_LOCK"\n'
            "  fi\n"
            "elif [[ $1 == supervisor && $2 == stop ]]; then\n"
            '  print -r -- stopped >"$BOOTSTRAP_SUPERVISOR_STATE"\n'
            "fi\n"
            "if (( bootstrap_fence_fd >= 0 )); then\n"
            "  zsystem flock -u $bootstrap_fence_fd\n"
            "  bootstrap_fence_fd=-1\n"
            "fi\n"
            "if [[ $1 == supervisor && $2 == start "
            "&& -n ${BOOTSTRAP_NEW_MASTIC_START_RELEASED:-} ]]; then\n"
            '  : >"$BOOTSTRAP_NEW_MASTIC_START_RELEASED"\n'
            "  while [[ ! -e $BOOTSTRAP_NEW_MASTIC_START_RETURN_CONTINUE ]]; do\n"
            "    sleep 0.05\n"
            "  done\n"
            "fi\n"
            "if [[ $1 == supervisor && $2 == stop "
            "&& -n ${BOOTSTRAP_NEW_MASTIC_STOP_RELEASED:-} ]]; then\n"
            '  : >"$BOOTSTRAP_NEW_MASTIC_STOP_RELEASED"\n'
            "  while [[ ! -e $BOOTSTRAP_NEW_MASTIC_STOP_CONTINUE ]]; do\n"
            "    sleep 0.05\n"
            "  done\n"
            "fi\n"
            'sleep "${BOOTSTRAP_NEW_MASTIC_POST_RELEASE_DELAY:-0}"\n'
            "MASTIC\n"
            'print -rn -- "new masticd" >"$UV_TOOL_BIN_DIR/masticd"\n'
            'chmod 0755 "$UV_TOOL_BIN_DIR/mastic" "$UV_TOOL_BIN_DIR/masticd"\n'
            "if [[ -n ${BOOTSTRAP_EXPECT_TRANSITION_LOCK:-} ]]; then\n"
            "  BOOTSTRAP_LOCK_PATH=$BOOTSTRAP_EXPECT_TRANSITION_LOCK "
            "zsh -fc 'zmodload zsh/system || exit 2; "
            "if zsystem flock -t 0.1 -f contender $BOOTSTRAP_LOCK_PATH; "
            "then exit 1; fi'\n"
            "fi\n"
            "[[ -n ${BOOTSTRAP_UV_FAIL_AFTER_MUTATION:-} ]] && exit 97\n"
            "if [[ -n ${BOOTSTRAP_UV_TERM_AFTER_MUTATION:-} ]]; then\n"
            "  kill -TERM $PPID\n"
            "  sleep 1\n"
            "fi\n",
            encoding="utf-8",
        )
        uv.chmod(0o755)
        python = closure_root / "python/bin/python3.11"
        python.write_text("#!/bin/zsh\nexit 0\n", encoding="utf-8")
        python.chmod(0o755)
        (closure_root / "wheels/dependency-1.0-py3-none-any.whl").write_bytes(
            b"dependency"
        )
        (closure_root / "wheels" / wheel.name).write_bytes(wheel.read_bytes())
        (closure_root / "application-targets-v1/manifest.json").write_text(
            '{"schema_version":1}\n', encoding="utf-8"
        )
        (
            closure_root / "application-targets-v1/artifacts/hindsight-darwin-arm64"
        ).write_bytes(b"hindsight")
        (
            closure_root
            / "application-targets-v1/artifacts/codex-aarch64-apple-darwin.tar.gz"
        ).write_bytes(b"codex")
        (
            closure_root
            / "application-targets-v1/artifacts/hindsight-api-0.8.4-macos-arm64.tar.gz"
        ).write_bytes(b"hindsight-api")
        members = sorted(path for path in closure_root.rglob("*") if path.is_file())
        manifest = "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(closure_root)}\n"
            for path in members
        )
        (closure_root / "SHA256SUMS").write_text(manifest, encoding="utf-8")
        with tarfile.open(closure, "w:gz") as archive:
            for path in sorted(closure_root.rglob("*")):
                archive.add(path, arcname=path.relative_to(closure_root))
        artifact = root / "bootstrap-mastic.zsh"
        subprocess.run(
            ["zsh", str(BUILDER), str(wheel), str(closure), str(artifact)],
            check=True,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        return root, artifact, release

    def __exit__(self, exc_type, exc_value, traceback):
        return self._temporary.__exit__(exc_type, exc_value, traceback)
