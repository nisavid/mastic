import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
import tempfile
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

    def _artifact(self):
        return _ArtifactFixture()

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

    def _tool(self, path: Path, body: str) -> None:
        path.write_text(f"#!/usr/bin/env zsh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)


class _ArtifactFixture:
    def __enter__(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.__enter__())
        wheel = root / "mastic-0.1.0-py3-none-any.whl"
        wheel.write_bytes(b"trusted wheel")
        release = root / "release"
        release.mkdir()
        release_wheel = release / wheel.name
        release_wheel.write_bytes(wheel.read_bytes())
        closure = release / "mastic-bootstrap-closure-0.1.0-macos-arm64.tar.gz"
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
            'print -rn -- "new tool" >"$UV_TOOL_DIR/mastic/release.txt"\n'
            'print -rn -- "new mastic" >"$UV_TOOL_BIN_DIR/mastic"\n'
            'print -rn -- "new masticd" >"$UV_TOOL_BIN_DIR/masticd"\n'
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
