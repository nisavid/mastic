"""Exact, ownership-aware supply for Phase 1 consuming applications."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from mastic.application.dispatch import ApplicationError


_VERSIONS = {"codex": "0.144.1", "hindsight": "0.8.4"}
_REQUIRED = {"codex": ("codex-cli",), "hindsight": ("hindsight-cli", "hindsight-api")}
_HINDSIGHT_API_ENTRY_POINTS = {
    "hindsight-admin": "hindsight_api.admin.cli:main",
    "hindsight-api": "hindsight_api.main:main",
    "hindsight-local-mcp": "hindsight_api.mcp_local:main",
    "hindsight-worker": "hindsight_api.worker.main:main",
}
_OFFICIAL_DIGESTS = {
    "codex-cli": "88e72ac8bd30815f7d18e62dac333dc20ce3ad1cba94be1649a1977dd9bfdbb8",
    "hindsight-cli": "defe5d281f79098bbda54ab7c51e8c47575d15e33cdfffb1713ac48e182192df",
}
_OFFICIAL_METADATA = {
    "codex-cli": (
        "https://github.com/openai/codex/releases/download/rust-v0.144.1/codex-aarch64-apple-darwin.tar.gz",
        "standalone-tar",
        ("--version",),
        "codex-cli 0.144.1",
    ),
    "hindsight-cli": (
        "https://github.com/vectorize-io/hindsight/releases/download/v0.8.4/hindsight-darwin-arm64",
        "standalone",
        ("--version",),
        "hindsight 0.8.4",
    ),
}


@dataclass(frozen=True, slots=True)
class _Artifact:
    identity: str
    version: str
    filename: str
    sha256: str
    source_url: str
    install_kind: str
    probe_argv: tuple[str, ...]
    probe_output: str


class ApplicationSupply:
    """Adopt or install the exact applications selected by a setup Plan."""

    def __init__(
        self,
        home: Path,
        cache_dir: Path,
        state_dir: Path,
        *,
        uv_executable: Path | None = None,
        python_executable: Path | None = None,
        application_tool_dir: Path | None = None,
        application_bin_dir: Path | None = None,
        run_command: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self._home = home.expanduser().absolute()
        self._cache = cache_dir.expanduser().absolute()
        self._state = state_dir.expanduser().absolute()
        self._uv = (
            (uv_executable or self._home / ".local/share/mastic/bootstrap-uv/uv")
            .expanduser()
            .absolute()
        )
        self._python = (
            (
                python_executable
                or self._home / ".local/share/mastic/bootstrap-python/bin/python3.11"
            )
            .expanduser()
            .absolute()
        )
        data_dir = self._cache.parent.parent
        self._application_tool_dir = (
            (application_tool_dir or data_dir / "application-tools")
            .expanduser()
            .absolute()
        )
        self._application_bin_dir = (
            (application_bin_dir or data_dir / "application-bin")
            .expanduser()
            .absolute()
        )
        self._run = run_command

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        if operation == "application.remove":
            return self._remove(parameters)
        if operation != "application.install":
            raise ApplicationError("operation_unavailable", operation)
        if parameters.get("confirmed") is not True:
            raise ApplicationError(
                "confirmation_required",
                "application installation requires confirmation",
            )
        targets = _targets(parameters.get("application_targets", ()))
        artifacts = self._verified_artifacts(targets)
        journal = self._load_journal()
        prior = journal.get("applications", {})
        if not isinstance(prior, Mapping):
            raise ApplicationError(
                "application_ownership_invalid",
                "application installation journal is invalid",
            )
        adopted = self._adoptable(artifacts, targets)
        codex_owned = _journal_owns(prior, "codex")
        hindsight_owned = _journal_owns_cli(prior)
        api_owned = _journal_owns_api(prior)
        conflicts: list[str] = []
        codex_path = self._home / ".local/bin/codex"
        hindsight_path = self._home / ".local/bin/hindsight"
        if "codex" in targets and _path_present(codex_path) and not adopted["codex"]:
            conflicts.append(str(codex_path))
        if (
            "hindsight" in targets
            and _path_present(hindsight_path)
            and not adopted["hindsight-cli"]
        ):
            conflicts.append(str(hindsight_path))
        api_root = self._api_tool_root()
        api_bins = self._api_bin_paths()
        if (
            "hindsight" in targets
            and (
                _path_present(api_root)
                or any(_path_present(path) for path in api_bins.values())
            )
            and not adopted["hindsight-api"]
        ):
            conflicts.extend(
                str(path)
                for path in (api_root, *api_bins.values())
                if _path_present(path)
            )
        if conflicts:
            raise ApplicationError(
                "application_install_conflict",
                "Refusing to overwrite nonmatching third-party applications: "
                + ", ".join(conflicts),
                next_actions=(
                    "remove or relocate the conflicting application explicitly",
                ),
            )
        installed: dict[str, Mapping[str, object]] = {
            str(name): dict(value)
            for name, value in prior.items()
            if isinstance(name, str) and isinstance(value, Mapping)
        }
        removed_applications = _journal_names(
            journal.get("removed_applications", ()), "removed_applications"
        )
        journal["removed_applications"] = sorted(
            set(removed_applications) - set(targets)
        )
        journal["state"] = "installing"
        journal["selected_targets"] = list(targets)
        self._write_journal(journal)
        try:
            if "codex" in targets:
                if adopted["codex"]:
                    ownership = "mastic" if codex_owned else "third-party"
                    provenance = "installed" if codex_owned else "adopted"
                else:
                    expected = hashlib.sha256(
                        _codex_binary(self._artifact_path(artifacts["codex-cli"]))
                    ).hexdigest()
                    installed["codex"] = {
                        "version": _VERSIONS["codex"],
                        "provenance": "installing",
                        "ownership": "mastic",
                        "path": str(codex_path),
                        "sha256": expected,
                    }
                    journal["applications"] = installed
                    self._write_journal(journal)
                    self._install_codex(artifacts["codex-cli"])
                    ownership = "mastic"
                    provenance = "installed"
                installed["codex"] = {
                    "version": _VERSIONS["codex"],
                    "provenance": provenance,
                    "ownership": ownership,
                    "path": str(self._home / ".local/bin/codex"),
                    "sha256": _required_regular_digest(self._home / ".local/bin/codex"),
                }
                journal["applications"] = installed
                self._write_journal(journal)
            if "hindsight" in targets:
                cli_adopted = adopted["hindsight-cli"]
                api_adopted = adopted["hindsight-api"]
                cli_is_owned = hindsight_owned or not cli_adopted
                api_is_owned = api_owned or not api_adopted
                installed["hindsight"] = {
                    "version": _VERSIONS["hindsight"],
                    "provenance": "installing",
                    "ownership": (
                        "mastic"
                        if cli_is_owned and api_is_owned
                        else "third-party"
                        if not cli_is_owned and not api_is_owned
                        else "mixed"
                    ),
                    "cli_path": str(hindsight_path),
                    "cli_sha256": artifacts["hindsight-cli"].sha256,
                    "cli_ownership": "mastic" if cli_is_owned else "third-party",
                    "api_ownership": "mastic" if api_is_owned else "third-party",
                    "api_tool_root": str(api_root),
                    "api_bin_paths": {
                        name: str(path) for name, path in api_bins.items()
                    },
                }
                journal["applications"] = installed
                self._write_journal(journal)
                if not cli_adopted:
                    self._install_hindsight(artifacts["hindsight-cli"])
                if not api_adopted:
                    self._install_hindsight_api(artifacts["hindsight-api"])
                installed["hindsight"] = {
                    "version": _VERSIONS["hindsight"],
                    "provenance": (
                        "adopted" if cli_adopted and api_adopted else "installed"
                    ),
                    "ownership": (
                        "mastic"
                        if cli_is_owned and api_is_owned
                        else "third-party"
                        if not cli_is_owned and not api_is_owned
                        else "mixed"
                    ),
                    "cli_path": str(self._home / ".local/bin/hindsight"),
                    "cli_sha256": _required_regular_digest(
                        self._home / ".local/bin/hindsight"
                    ),
                    "cli_ownership": "mastic" if cli_is_owned else "third-party",
                    "api_ownership": "mastic" if api_is_owned else "third-party",
                    "api_tool_root": str(api_root),
                    "api_bin_paths": {
                        name: str(path) for name, path in api_bins.items()
                    },
                    "api_bin_sha256": {
                        name: _required_regular_digest(path)
                        for name, path in api_bins.items()
                    },
                }
        except Exception:
            journal["state"] = "interrupted"
            journal["applications"] = installed
            self._write_journal(journal)
            raise
        journal["state"] = "complete"
        journal["applications"] = installed
        self._write_journal(journal)
        return {
            "applications": {name: installed[name] for name in targets},
            "artifact_manifest": str(self._cache / "manifest.json"),
        }

    def inventory(self) -> Mapping[str, tuple[str, ...]]:
        journal = self._load_journal()
        applications = journal.get("applications", {})
        if not isinstance(applications, Mapping):
            raise ApplicationError(
                "application_ownership_invalid",
                "application installation journal is invalid",
            )
        owned = tuple(
            sorted(
                str(name)
                for name, value in applications.items()
                if isinstance(value, Mapping)
                and value.get("ownership") in {"mastic", "mixed"}
            )
        )
        retained = tuple(
            sorted(
                str(name)
                for name, value in applications.items()
                if isinstance(value, Mapping)
                and value.get("ownership") == "third-party"
            )
        )
        return {"owned": owned, "retained": retained}

    def _remove(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        if parameters.get("confirmed") is not True:
            raise ApplicationError(
                "confirmation_required", "application removal requires confirmation"
            )
        requested = _targets(parameters.get("applications", ()))
        journal = self._load_journal()
        applications = journal.get("applications", {})
        if not isinstance(applications, dict):
            raise ApplicationError(
                "application_ownership_invalid",
                "application installation journal is invalid",
            )
        removable = {
            str(name)
            for name, value in applications.items()
            if isinstance(value, Mapping)
            and value.get("ownership") in {"mastic", "mixed"}
        }
        already_removed = set(
            _journal_names(
                journal.get("removed_applications", ()), "removed_applications"
            )
        )
        if set(requested) - removable - already_removed:
            raise ApplicationError(
                "application_ownership_invalid",
                "removal names an application MASTIC does not own",
            )
        removed: list[str] = []
        retained: list[str] = []
        for name in requested:
            if name in already_removed and name not in applications:
                removed.append(name)
                continue
            value = dict(applications[name])
            if name == "codex":
                self._remove_owned_file_component(
                    journal,
                    applications,
                    name,
                    value,
                    state_key="removal_state",
                    path=Path(str(value["path"])),
                    expected_digest=str(value["sha256"]),
                )
            else:
                if value.get("cli_ownership") == "mastic":
                    self._remove_owned_file_component(
                        journal,
                        applications,
                        name,
                        value,
                        state_key="cli_removal_state",
                        path=Path(str(value["cli_path"])),
                        expected_digest=str(value["cli_sha256"]),
                    )
                else:
                    retained.append(str(value["cli_path"]))
                if value.get("api_ownership") == "mastic":
                    self._remove_owned_api_component(journal, applications, name, value)
            applications.pop(name)
            already_removed.add(name)
            removed.append(name)
            journal["applications"] = applications
            journal["removed_applications"] = sorted(already_removed)
            journal["state"] = "removed" if not applications else "complete"
            self._write_journal(journal)
        return {"removed": removed, "retained": retained}

    def _remove_owned_file_component(
        self,
        journal: dict[str, object],
        applications: dict[str, object],
        name: str,
        value: dict[str, object],
        *,
        state_key: str,
        path: Path,
        expected_digest: str,
    ) -> None:
        state = value.get(state_key)
        if state == "removed":
            return
        if state != "pending":
            _validate_owned_file(path, expected_digest, allow_missing=False)
            value[state_key] = "pending"
            self._write_removal_transition(journal, applications, name, value)
        if _validate_owned_file(path, expected_digest, allow_missing=True):
            path.unlink()
        value[state_key] = "removed"
        self._write_removal_transition(journal, applications, name, value)

    def _remove_owned_api_component(
        self,
        journal: dict[str, object],
        applications: dict[str, object],
        name: str,
        value: dict[str, object],
    ) -> None:
        state = value.get("api_removal_state")
        if state == "removed":
            return
        if state != "pending":
            self._validate_owned_api(value, allow_missing=False)
            value["api_removal_state"] = "pending"
            self._write_removal_transition(journal, applications, name, value)
        if self._validate_owned_api(value, allow_missing=True):
            self._run(
                (str(self._uv), "tool", "uninstall", "hindsight-api"),
                check=True,
                shell=False,
                timeout=10 * 60,
                env=self._uv_environment(),
            )
            self._validate_owned_api(value, allow_missing=True)
            if _path_present(self._api_tool_root()) or any(
                _path_present(path) for path in self._api_bin_paths().values()
            ):
                raise RuntimeError(
                    "Hindsight API uninstall left owned tool resources behind"
                )
        value["api_removal_state"] = "removed"
        self._write_removal_transition(journal, applications, name, value)

    def _validate_owned_api(
        self, value: Mapping[str, object], *, allow_missing: bool
    ) -> bool:
        root = Path(str(value.get("api_tool_root", "")))
        raw_paths = value.get("api_bin_paths")
        raw_digests = value.get("api_bin_sha256")
        if not isinstance(raw_paths, Mapping) or not isinstance(raw_digests, Mapping):
            raise ApplicationError(
                "application_ownership_invalid",
                "Hindsight API launcher ownership is incomplete",
            )
        launchers = {str(name): Path(str(path)) for name, path in raw_paths.items()}
        expected = self._api_bin_paths()
        if root != self._api_tool_root() or launchers != expected:
            raise ApplicationError(
                "application_ownership_invalid",
                "Hindsight API ownership paths do not match the managed tool paths",
            )
        root_present = _path_present(root)
        launcher_presence = {
            name: _path_present(path) for name, path in launchers.items()
        }
        if allow_missing and not root_present and not any(launcher_presence.values()):
            return False
        if (
            not root_present
            or not all(launcher_presence.values())
            or not _trusted_directory(root)
            or set(raw_digests) != set(launchers)
            or any(
                not _digest(raw_digests[name])
                or _regular_digest(path) != raw_digests[name]
                for name, path in launchers.items()
            )
        ):
            raise ApplicationError(
                "application_ownership_drift",
                "refusing to remove changed or missing owned Hindsight API",
            )
        return True

    def _write_removal_transition(
        self,
        journal: dict[str, object],
        applications: dict[str, object],
        name: str,
        value: Mapping[str, object],
    ) -> None:
        applications[name] = dict(value)
        journal["state"] = "removing"
        journal["applications"] = applications
        self._write_journal(journal)

    def _verified_artifacts(self, targets: Sequence[str]) -> dict[str, _Artifact]:
        errors: list[str] = []
        manifest_path = self._cache / "manifest.json"
        receipt_path = self._cache.parent / "bootstrap-receipt.json"
        payload: object = None
        try:
            if not _trusted_directory(self._cache.parent) or not _trusted_directory(
                self._cache
            ):
                raise OSError(
                    "bootstrap cache directories are not private and user-owned"
                )
            if not _trusted_regular_file(receipt_path):
                raise OSError("missing bootstrap receipt")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if (
                not isinstance(receipt, Mapping)
                or receipt.get("schema_version") != 1
                or receipt.get("application_manifest_sha256") != _sha256(manifest_path)
                or not _digest(receipt.get("closure_sha256"))
            ):
                raise OSError("bootstrap receipt does not authenticate the manifest")
            if not _trusted_regular_file(manifest_path):
                raise OSError("missing regular file")
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            errors.append(f"manifest.json ({error})")
        entries: dict[str, _Artifact] = {}
        if isinstance(payload, Mapping):
            if (
                payload.get("schema_version") != 1
                or payload.get("platform") != "macos-arm64"
            ):
                errors.append("manifest.json (unsupported schema or platform)")
            raw_entries = payload.get("artifacts")
            if isinstance(raw_entries, list):
                for raw in raw_entries:
                    try:
                        artifact = _parse_artifact(raw)
                    except ValueError as error:
                        errors.append(f"manifest.json ({error})")
                        continue
                    if artifact.identity in entries:
                        errors.append(f"{artifact.identity} (duplicate manifest entry)")
                    entries[artifact.identity] = artifact
            else:
                errors.append("manifest.json (artifacts must be an array)")
        required = tuple(
            dict.fromkeys(item for target in targets for item in _REQUIRED[target])
        )
        verified: dict[str, _Artifact] = {}
        for identity in required:
            artifact = entries.get(identity)
            if artifact is None:
                errors.append(f"{identity} (missing manifest entry)")
                continue
            path = self._cache / "artifacts" / artifact.filename
            try:
                if not _trusted_regular_file(path):
                    raise OSError("missing regular artifact")
                actual = _sha256(path)
                if actual != artifact.sha256:
                    raise OSError("digest mismatch")
                if identity == "hindsight-api":
                    _verify_api_bundle(path)
            except (OSError, tarfile.TarError, ValueError) as error:
                errors.append(f"{identity} ({error})")
                continue
            verified[identity] = artifact
        if errors:
            raise ApplicationError(
                "application_artifacts_missing",
                "Exact application artifacts are incomplete or invalid: "
                + "; ".join(sorted(set(errors))),
                next_actions=("restore the attested MASTIC bootstrap artifact cache",),
            )
        return verified

    def _adoptable(
        self, artifacts: Mapping[str, _Artifact], targets: Sequence[str]
    ) -> dict[str, bool]:
        result: dict[str, bool] = {}
        if "codex" in targets:
            expected = _codex_binary(self._artifact_path(artifacts["codex-cli"]))
            path = self._home / ".local/bin/codex"
            result["codex"] = _regular_digest(path) == hashlib.sha256(
                expected
            ).hexdigest() and self._probe(path, artifacts["codex-cli"])
        if "hindsight" in targets:
            path = self._home / ".local/bin/hindsight"
            result["hindsight-cli"] = _regular_digest(path) == artifacts[
                "hindsight-cli"
            ].sha256 and self._probe(path, artifacts["hindsight-cli"])
            result["hindsight-api"] = self._api_matches_bundle(
                self._artifact_path(artifacts["hindsight-api"])
            ) and self._probe_api(artifacts["hindsight-api"])
        return result

    def _install_codex(self, artifact: _Artifact) -> None:
        path = self._home / ".local/bin/codex"
        self._install_bytes(path, _codex_binary(self._artifact_path(artifact)))
        if not self._probe(path, artifact):
            raise RuntimeError("installed Codex failed its exact version probe")

    def _install_hindsight(self, artifact: _Artifact) -> None:
        path = self._home / ".local/bin/hindsight"
        self._install_bytes(path, self._artifact_path(artifact).read_bytes())
        if not self._probe(path, artifact):
            raise RuntimeError("installed Hindsight CLI failed its exact version probe")

    def _install_hindsight_api(self, artifact: _Artifact) -> None:
        if not self._uv.is_absolute() or not self._python.is_absolute():
            raise RuntimeError(
                "offline application tools require absolute uv and Python paths"
            )
        with tempfile.TemporaryDirectory(prefix="mastic-hindsight-api-") as raw:
            root = Path(raw)
            _extract_safe(self._artifact_path(artifact), root)
            wheels = root / "wheels"
            requirements = root / "requirements.lock"
            self._run(
                (
                    str(self._uv),
                    "tool",
                    "install",
                    "--offline",
                    "--no-index",
                    "--find-links",
                    str(wheels),
                    "--with-requirements",
                    str(requirements),
                    "--python",
                    str(self._python),
                    "--python-preference",
                    "only-system",
                    "--no-python-downloads",
                    "--force",
                    "hindsight-api==0.8.4",
                ),
                check=True,
                shell=False,
                timeout=30 * 60,
                env=self._uv_environment(),
            )
            if not self._probe_api(artifact):
                raise RuntimeError(
                    "installed Hindsight API failed its exact version probe"
                )

    def _probe(self, executable: Path, artifact: _Artifact) -> bool:
        try:
            completed = self._run(
                (str(executable), *artifact.probe_argv),
                check=False,
                shell=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return (
            completed.returncode == 0
            and completed.stdout.strip() == artifact.probe_output
        )

    def _probe_api(self, artifact: _Artifact) -> bool:
        python = self._api_tool_root() / "bin/python"
        try:
            completed = self._run(
                (
                    str(python),
                    "-c",
                    "import importlib.metadata; print(importlib.metadata.version('hindsight-api'))",
                ),
                check=False,
                shell=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            capability = self._run(
                (str(self._api_bin_paths()["hindsight-api"]), "--help"),
                check=False,
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return (
            completed.returncode == 0
            and completed.stdout.strip() == artifact.probe_output
            and capability.returncode == 0
        )

    def _api_tool_root(self) -> Path:
        return self._application_tool_dir / "hindsight-api"

    def _api_bin_paths(self) -> dict[str, Path]:
        return {
            name: self._application_bin_dir / name
            for name in _HINDSIGHT_API_ENTRY_POINTS
        }

    def _uv_environment(self) -> dict[str, str]:
        return {
            **os.environ,
            "UV_TOOL_DIR": str(self._application_tool_dir),
            "UV_TOOL_BIN_DIR": str(self._application_bin_dir),
        }

    def _api_matches_bundle(self, archive: Path) -> bool:
        site = self._api_tool_root() / "lib/python3.11/site-packages"
        if not site.is_dir() or site.is_symlink() or not self._api_launchers_match():
            return False
        try:
            with tempfile.TemporaryDirectory(prefix="mastic-api-adopt-") as raw:
                root = Path(raw)
                _extract_safe(archive, root)
                for wheel in (root / "wheels").glob("*.whl"):
                    with zipfile.ZipFile(wheel) as package:
                        for member in package.infolist():
                            if (
                                member.is_dir()
                                or ".data/" in member.filename
                                or member.filename.endswith(".dist-info/RECORD")
                            ):
                                continue
                            installed = site / member.filename
                            if (
                                installed.is_symlink()
                                or not installed.is_file()
                                or installed.read_bytes() != package.read(member)
                            ):
                                return False
                return any((root / "wheels").glob("*.whl"))
        except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile):
            return False

    def _api_launchers_match(self) -> bool:
        for name, target in _HINDSIGHT_API_ENTRY_POINTS.items():
            launcher = self._api_bin_paths()[name]
            module, function = target.split(":", 1)
            try:
                text = launcher.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                return False
            expected = (
                f"#!{self._api_tool_root() / 'bin/python'}\n"
                "# -*- coding: utf-8 -*-\n"
                "import sys\n"
                f"from {module} import {function}\n"
                'if __name__ == "__main__":\n'
                '    if sys.argv[0].endswith("-script.pyw"):\n'
                "        sys.argv[0] = sys.argv[0][:-11]\n"
                '    elif sys.argv[0].endswith(".exe"):\n'
                "        sys.argv[0] = sys.argv[0][:-4]\n"
                f"    sys.exit({function}())\n"
            )
            if launcher.is_symlink() or text != expected:
                return False
        return True

    def _install_bytes(self, destination: Path, content: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        if destination.is_symlink():
            raise ApplicationError(
                "application_install_conflict",
                f"refusing to replace symlink: {destination}",
            )
        temporary = destination.with_name(f".{destination.name}.mastic-{os.getpid()}")
        temporary.write_bytes(content)
        temporary.chmod(0o755)
        os.replace(temporary, destination)

    def _artifact_path(self, artifact: _Artifact) -> Path:
        return self._cache / "artifacts" / artifact.filename

    def _load_journal(self) -> dict[str, object]:
        path = self._state / "application-installations.json"
        if not path.exists():
            return {"schema_version": 1, "applications": {}}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ApplicationError(
                "application_ownership_invalid",
                "application installation journal is invalid",
            ) from error
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ApplicationError(
                "application_ownership_invalid",
                "application installation journal is invalid",
            )
        return value

    def _write_journal(self, value: Mapping[str, object]) -> None:
        self._state.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._state / "application-installations.json"
        temporary = path.with_name(f".{path.name}.mastic-{os.getpid()}")
        temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)


def _targets(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or any(
        type(item) is not str for item in value
    ):
        raise ApplicationError(
            "invalid_parameter", "application_targets must be an array of names"
        )
    targets = tuple(value)
    unknown = sorted(set(targets) - set(_VERSIONS))
    if unknown or len(set(targets)) != len(targets):
        raise ApplicationError(
            "invalid_parameter", "application_targets must be unique Phase 1 targets"
        )
    return targets


def _journal_names(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or any(
        type(item) is not str for item in value
    ):
        raise ApplicationError(
            "application_ownership_invalid",
            f"application installation journal field {field!r} is invalid",
        )
    names = tuple(value)
    if len(set(names)) != len(names) or set(names) - set(_VERSIONS):
        raise ApplicationError(
            "application_ownership_invalid",
            f"application installation journal field {field!r} is invalid",
        )
    return names


def _journal_owns(applications: Mapping[object, object], name: str) -> bool:
    value = applications.get(name)
    return isinstance(value, Mapping) and value.get("ownership") in {
        "mastic",
        "mixed",
    }


def _journal_owns_api(applications: Mapping[object, object]) -> bool:
    value = applications.get("hindsight")
    return isinstance(value, Mapping) and value.get("api_ownership") == "mastic"


def _journal_owns_cli(applications: Mapping[object, object]) -> bool:
    value = applications.get("hindsight")
    return isinstance(value, Mapping) and value.get("cli_ownership") == "mastic"


def _parse_artifact(value: object) -> _Artifact:
    if not isinstance(value, Mapping):
        raise ValueError("artifact entry must be an object")
    fields = (
        "id",
        "version",
        "filename",
        "sha256",
        "source_url",
        "install_kind",
        "probe_output",
    )
    if any(
        type(value.get(field)) is not str or not value.get(field) for field in fields
    ):
        raise ValueError("artifact entry has missing string fields")
    identity = str(value["id"])
    filename = str(value["filename"])
    digest = str(value["sha256"])
    probe = value.get("probe_argv")
    if (
        Path(filename).name != filename
        or len(digest) != 64
        or any(item not in "0123456789abcdef" for item in digest)
    ):
        raise ValueError(f"artifact {identity} has an unsafe filename or digest")
    if not isinstance(probe, list) or any(
        type(item) is not str or not item for item in probe
    ):
        raise ValueError(f"artifact {identity} has an invalid probe")
    expected_version = "0.144.1" if identity == "codex-cli" else "0.8.4"
    if (
        identity not in {"codex-cli", "hindsight-cli", "hindsight-api"}
        or value["version"] != expected_version
    ):
        raise ValueError(f"artifact {identity} is not an exact Phase 1 artifact")
    official = _OFFICIAL_DIGESTS.get(identity)
    if official is not None and digest != official:
        raise ValueError(f"artifact {identity} does not match its official digest")
    metadata = _OFFICIAL_METADATA.get(identity)
    if (
        metadata is not None
        and (
            str(value["source_url"]),
            str(value["install_kind"]),
            tuple(probe),
            str(value["probe_output"]),
        )
        != metadata
    ):
        raise ValueError(
            f"artifact {identity} does not match official release metadata"
        )
    if identity == "hindsight-api" and (
        str(value["install_kind"]) != "uv-tool-offline"
        or tuple(probe) != ("python-metadata", "hindsight-api")
        or str(value["probe_output"]) != "0.8.4"
        or not str(value["source_url"]).startswith(
            "https://github.com/nisavid/mastic/releases/download/v"
        )
        or not str(value["source_url"]).endswith("/" + filename)
    ):
        raise ValueError(
            "artifact hindsight-api does not match official bundle metadata"
        )
    return _Artifact(
        identity,
        str(value["version"]),
        filename,
        digest,
        str(value["source_url"]),
        str(value["install_kind"]),
        tuple(probe),
        str(value["probe_output"]),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(item in "0123456789abcdef" for item in value)
    )


def _regular_digest(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    return _sha256(path)


def _required_regular_digest(path: Path) -> str:
    digest = _regular_digest(path)
    if digest is None:
        raise RuntimeError(f"installed application is not a regular file: {path}")
    return digest


def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _validate_owned_file(
    path: Path, expected_digest: str, *, allow_missing: bool
) -> bool:
    actual = _regular_digest(path)
    if actual is None and allow_missing and not _path_present(path):
        return False
    if actual != expected_digest:
        raise ApplicationError(
            "application_ownership_drift",
            f"refusing to remove changed or missing owned application: {path}",
        )
    return True


def _trusted_regular_file(path: Path) -> bool:
    try:
        metadata = path.stat()
    except OSError:
        return False
    return (
        not path.is_symlink()
        and path.is_file()
        and metadata.st_uid == os.getuid()
        and metadata.st_mode & 0o022 == 0
    )


def _trusted_directory(path: Path) -> bool:
    try:
        metadata = path.stat()
    except OSError:
        return False
    return (
        not path.is_symlink()
        and path.is_dir()
        and metadata.st_uid == os.getuid()
        and metadata.st_mode & 0o022 == 0
    )


def _codex_binary(archive: Path) -> bytes:
    with tarfile.open(archive, "r:gz") as source:
        members = [member for member in source.getmembers() if member.isfile()]
        if (
            len(members) != 1
            or Path(members[0].name).name != "codex-aarch64-apple-darwin"
        ):
            raise ValueError("Codex archive has an unexpected layout")
        extracted = source.extractfile(members[0])
        if extracted is None:
            raise ValueError("Codex archive lacks its executable")
        return extracted.read()


def _extract_safe(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as source:
        for member in source.getmembers():
            path = Path(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or member.issym()
                or member.islnk()
            ):
                raise ValueError("archive contains an unsafe member")
        source.extractall(destination, filter="data")


def _verify_api_bundle(archive: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="mastic-api-verify-") as raw:
        root = Path(raw)
        _extract_safe(archive, root)
        sums = root / "SHA256SUMS"
        wheels = root / "wheels"
        requirements = root / "requirements.lock"
        if (
            sums.is_symlink()
            or not sums.is_file()
            or not wheels.is_dir()
            or requirements.is_symlink()
            or not requirements.is_file()
        ):
            raise ValueError(
                "API bundle lacks SHA256SUMS, requirements.lock, or wheels"
            )
        expected: dict[str, str] = {}
        for line in sums.read_text(encoding="utf-8").splitlines():
            digest, separator, name = line.partition("  ")
            if (
                separator != "  "
                or Path(name).is_absolute()
                or ".." in Path(name).parts
            ):
                raise ValueError("API bundle has an invalid checksum entry")
            expected[name] = digest
        actual_files = sorted(
            path for path in root.rglob("*") if path.is_file() and path != sums
        )
        actual_names = {str(path.relative_to(root)) for path in actual_files}
        if actual_names != set(expected):
            raise ValueError("API bundle checksum set is incomplete")
        for path in actual_files:
            name = str(path.relative_to(root))
            if _sha256(path) != expected[name]:
                raise ValueError(f"API bundle digest mismatch: {name}")
