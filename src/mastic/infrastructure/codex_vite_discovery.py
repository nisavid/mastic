"""Fail-closed discovery for Codex installations owned through Vite+."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable, Iterator, Mapping, Protocol, Sequence

from mastic.application.external_application_lifecycle import (
    InstallationDiscoveryError,
)
from mastic.domain.canonical import canonical_fingerprint
from mastic.domain.external_applications import InstallationObservation


_MAX_METADATA_BYTES = 1_048_576
_MAX_PACKAGE_FILES = 20_000
_MAX_PACKAGE_BYTES = 768 * 1024 * 1024
_ANSI_SGR = re.compile(r"\x1b\[[0-9;:]*m")
_ALLOWED_OUTPUT_CONTROLS = frozenset("\t\n\r")
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
_VITE_INSTALL_ID = re.compile(
    r"#[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        executable_path: Sequence[Path] = (),
    ) -> CommandResult: ...


class CodexViteDiscoveryFailure(StrEnum):
    OWNER_UNRESOLVED = "owner_unresolved"
    OWNER_AMBIGUOUS = "owner_ambiguous"
    OWNER_METADATA_INVALID = "owner_metadata_invalid"
    PACKAGE_IDENTITY_MISMATCH = "package_identity_mismatch"
    INSTALLED_VERSION_MISMATCH = "installed_version_mismatch"
    OWNER_RUNTIME_UNAVAILABLE = "owner_runtime_unavailable"
    ACTIVE_INVOCATION_MISMATCH = "active_invocation_mismatch"
    REACHABLE_INVOCATION_CONFLICT = "reachable_invocation_conflict"
    OWNER_UPDATE_TARGET_MISMATCH = "owner_update_target_mismatch"
    DOCTOR_CONTRACT_UNAVAILABLE = "doctor_contract_unavailable"


class CodexViteDiscoveryError(InstallationDiscoveryError):
    def __init__(self, reason_code: str | CodexViteDiscoveryFailure) -> None:
        super().__init__(str(reason_code))
        self.reason_code = str(reason_code)


@dataclass(frozen=True, slots=True)
class _OwnerState:
    source: str
    node_version: str
    configured_version: str
    bin_config_bytes: bytes
    vite_metadata_bytes: bytes | None
    package_root: Path | None


@dataclass(frozen=True, slots=True)
class _ViteWhich:
    executable: Path
    package: str
    source: str | None
    node_version: str


class CodexViteDiscovery:
    def __init__(
        self,
        *,
        vp_home: Path,
        path: Sequence[Path],
        runner: CommandRunner,
        observed_at: Callable[[], datetime],
        platform: str,
        architecture: str,
    ) -> None:
        if platform != "darwin" or architecture != "arm64":
            raise ValueError("Codex Vite+ discovery supports darwin/arm64")
        self._vp_home = Path(vp_home)
        self._path = tuple(Path(item) for item in path)
        self._runner = runner
        self._observed_at = observed_at
        self._platform = platform
        self._architecture = architecture

    def discover(
        self,
        *,
        selected_installation_identity: str,
        selected_release_channel: str,
    ) -> InstallationObservation:
        _identity(selected_installation_identity)
        _identity(selected_release_channel)
        initial_invocations = self._invocations()
        if not initial_invocations:
            raise CodexViteDiscoveryError(CodexViteDiscoveryFailure.OWNER_UNRESOLVED)
        active_path, active_target = initial_invocations[0]
        expected_active = self._vp_home / "bin" / "codex"
        if active_path != expected_active:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.ACTIVE_INVOCATION_MISMATCH
            )
        if any(target != active_target for _path, target in initial_invocations[1:]):
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.REACHABLE_INVOCATION_CONFLICT
            )

        initial_owner = self._load_owner_state()
        owner_executable_path = self._owner_executable_path(initial_owner.node_version)
        vite_which = self._vite_which(executable_path=owner_executable_path)
        if vite_which.executable != active_target:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.ACTIVE_INVOCATION_MISMATCH
            )
        if vite_which.node_version != initial_owner.node_version:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.OWNER_METADATA_INVALID
            )

        if initial_owner.source == "npm":
            if vite_which.source != "npm" or vite_which.package != "@openai/codex":
                raise CodexViteDiscoveryError(CodexViteDiscoveryFailure.OWNER_AMBIGUOUS)
            package_root = self._npm_package_root(executable_path=owner_executable_path)
            owner_identity = "vite-plus/npm-global"
        else:
            if (
                vite_which.source is not None
                or vite_which.package
                != f"@openai/codex@{initial_owner.configured_version}"
            ):
                raise CodexViteDiscoveryError(CodexViteDiscoveryFailure.OWNER_AMBIGUOUS)
            if initial_owner.package_root is None:
                raise CodexViteDiscoveryError(
                    CodexViteDiscoveryFailure.OWNER_METADATA_INVALID
                )
            package_root = initial_owner.package_root
            owner_identity = "vite-plus/global-package"

        trusted_package_base = (
            self._vp_home / "packages" if initial_owner.source == "vp" else None
        )

        with _open_package_root(package_root, trusted_package_base) as package_fd:
            package_bytes, package_version, package_bin = self._package_metadata(
                package_root, directory_fd=package_fd
            )
            initial_tree_digest = _package_tree_digest_fd(package_fd)
        if package_bin != active_target:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.PACKAGE_IDENTITY_MISMATCH
            )
        if (
            initial_owner.configured_version
            and initial_owner.configured_version != package_version
        ):
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.INSTALLED_VERSION_MISMATCH
            )
        runtime_version = self._runtime_version(
            active_path, executable_path=owner_executable_path
        )
        if runtime_version != package_version:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.INSTALLED_VERSION_MISMATCH
            )
        self._validate_doctor(
            active_path,
            package_version,
            package_root,
            owner_identity,
            executable_path=owner_executable_path,
        )

        try:
            final_owner = self._load_owner_state()
            final_invocations = self._invocations()
            with _open_package_root(package_root, trusted_package_base) as package_fd:
                final_package_bytes, final_version, final_package_bin = (
                    self._package_metadata(package_root, directory_fd=package_fd)
                )
                final_tree_digest = _package_tree_digest_fd(package_fd)
        except (CodexViteDiscoveryError, OSError) as error:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.OWNER_AMBIGUOUS
            ) from error
        if (
            final_owner != initial_owner
            or final_invocations != initial_invocations
            or final_package_bytes != package_bytes
            or final_version != package_version
            or final_package_bin != package_bin
            or final_tree_digest != initial_tree_digest
        ):
            raise CodexViteDiscoveryError(CodexViteDiscoveryFailure.OWNER_AMBIGUOUS)

        owner_installation_identity = self._owner_installation_identity(
            initial_owner,
            owner_identity=owner_identity,
            package_bytes=package_bytes,
            package_root=package_root,
        )
        return InstallationObservation(
            application_identity="external-application:codex",
            installation_identity=selected_installation_identity,
            owner_identity=owner_identity,
            owner_installation_identity=owner_installation_identity,
            owner_runtime_identity=f"node:{initial_owner.node_version}",
            release_channel=selected_release_channel,
            platform=self._platform,
            architecture=self._architecture,
            installed_release=package_version,
            installed_artifact_digest=initial_tree_digest,
            active_invocation=str(active_path),
            reachable_invocations=tuple(
                str(path) for path, _target in initial_invocations
            ),
            observed_at=self._observed_at(),
        )

    def payload_roots(self, observation: InstallationObservation) -> Mapping[str, Path]:
        """Locate the exact wrapper and platform payload roots for byte proof."""

        if observation.application_identity != "external-application:codex":
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.OWNER_METADATA_INVALID
            )
        state = self._load_owner_state()
        if observation.owner_runtime_identity != f"node:{state.node_version}":
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.OWNER_METADATA_INVALID
            )
        if observation.owner_identity == "vite-plus/npm-global":
            if state.source != "npm":
                raise CodexViteDiscoveryError(CodexViteDiscoveryFailure.OWNER_AMBIGUOUS)
            primary = self._npm_package_root(
                executable_path=self._owner_executable_path(state.node_version)
            )
            identity_root = primary
            trusted_base = None
        elif observation.owner_identity == "vite-plus/global-package":
            if state.source != "vp" or state.package_root is None:
                raise CodexViteDiscoveryError(CodexViteDiscoveryFailure.OWNER_AMBIGUOUS)
            trusted_base = self._vp_home / "packages"
            identity_root = state.package_root
        else:
            raise CodexViteDiscoveryError(CodexViteDiscoveryFailure.OWNER_AMBIGUOUS)
        with _open_package_root(identity_root, trusted_base) as package_fd:
            if trusted_base is not None:
                relative = identity_root.relative_to(trusted_base)
                primary = trusted_base.resolve(strict=True) / relative
            package_bytes, package_version, _package_bin = self._package_metadata(
                identity_root, directory_fd=package_fd
            )
        if package_version != observation.installed_release:
            raise CodexViteDiscoveryError(CodexViteDiscoveryFailure.OWNER_AMBIGUOUS)
        current_identity = self._owner_installation_identity(
            state,
            owner_identity=observation.owner_identity,
            package_bytes=package_bytes,
            package_root=identity_root,
        )
        if current_identity != observation.owner_installation_identity:
            raise CodexViteDiscoveryError(CodexViteDiscoveryFailure.OWNER_AMBIGUOUS)
        return {
            "primary": primary,
            "platform": (primary / "node_modules" / "@openai" / "codex-darwin-arm64"),
        }

    def locate(self, observation: object) -> Mapping[str, Path]:
        """Satisfy the installed-payload root port after typed validation."""

        if not isinstance(observation, InstallationObservation):
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.OWNER_METADATA_INVALID
            )
        return self.payload_roots(observation)

    def _invocations(self) -> tuple[tuple[Path, Path], ...]:
        found: list[tuple[Path, Path]] = []
        for directory in self._path:
            candidate = directory / "codex"
            try:
                metadata = candidate.stat()
                target = candidate.resolve(strict=True)
            except OSError:
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o111 == 0:
                continue
            found.append((candidate.absolute(), target))
        return tuple(found)

    def _owner_installation_identity(
        self,
        state: _OwnerState,
        *,
        owner_identity: str,
        package_bytes: bytes,
        package_root: Path,
    ) -> str:
        return canonical_fingerprint(
            {
                "bin_config_digest": _bytes_digest(state.bin_config_bytes),
                "node_version": state.node_version,
                "owner_identity": owner_identity,
                "package_metadata_digest": _bytes_digest(package_bytes),
                "package_root": str(package_root),
                "vite_metadata_digest": (
                    _bytes_digest(state.vite_metadata_bytes)
                    if state.vite_metadata_bytes is not None
                    else None
                ),
                "vp_home": str(self._vp_home),
            }
        )

    def _load_owner_state(self) -> _OwnerState:
        bin_path = self._vp_home / "bins" / "codex.json"
        try:
            raw = _read_bounded(
                bin_path,
                CodexViteDiscoveryFailure.OWNER_METADATA_INVALID,
            )
        except FileNotFoundError as error:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.OWNER_UNRESOLVED
            ) from error
        data = _json_object(raw, CodexViteDiscoveryFailure.OWNER_METADATA_INVALID)
        if data.get("name") != "codex" or data.get("package") != "@openai/codex":
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.PACKAGE_IDENTITY_MISMATCH
            )
        source = data.get("source", "vp")
        version = data.get("version")
        node_version = data.get("nodeVersion")
        if (
            source not in {"npm", "vp"}
            or not isinstance(version, str)
            or not _nonempty(node_version)
            or (source == "npm" and version)
            or (source == "vp" and not version)
        ):
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.OWNER_METADATA_INVALID
            )

        vite_metadata_bytes: bytes | None = None
        package_root: Path | None = None
        if source == "vp":
            metadata_path = self._vp_home / "packages" / "@openai" / "codex.json"
            try:
                vite_metadata_bytes = _read_bounded(
                    metadata_path,
                    CodexViteDiscoveryFailure.OWNER_METADATA_INVALID,
                )
            except FileNotFoundError as error:
                raise CodexViteDiscoveryError(
                    CodexViteDiscoveryFailure.OWNER_METADATA_INVALID
                ) from error
            metadata = _json_object(
                vite_metadata_bytes,
                CodexViteDiscoveryFailure.OWNER_METADATA_INVALID,
            )
            platform = metadata.get("platform")
            install_id = metadata.get("installId", "")
            if (
                metadata.get("name") != "@openai/codex"
                or metadata.get("version") != version
                or not isinstance(platform, Mapping)
                or platform.get("node") != node_version
                or not isinstance(metadata.get("bins"), list)
                or "codex" not in metadata["bins"]
                or not isinstance(install_id, str)
                or (bool(install_id) and _VITE_INSTALL_ID.fullmatch(install_id) is None)
            ):
                raise CodexViteDiscoveryError(
                    CodexViteDiscoveryFailure.OWNER_METADATA_INVALID
                )
            install_root = self._vp_home / "packages" / "@openai" / f"codex{install_id}"
            package_root = install_root / "lib" / "node_modules" / "@openai" / "codex"

        return _OwnerState(
            source=source,
            node_version=str(node_version),
            configured_version=version,
            bin_config_bytes=raw,
            vite_metadata_bytes=vite_metadata_bytes,
            package_root=package_root,
        )

    def _owner_executable_path(self, node_version: str) -> tuple[Path, ...]:
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", node_version) is None:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.OWNER_METADATA_INVALID
            )
        runtime_bin = self._vp_home / "js_runtime" / "node" / node_version / "bin"
        try:
            node_metadata = (runtime_bin / "node").stat()
        except OSError as error:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.OWNER_RUNTIME_UNAVAILABLE
            ) from error
        if (
            not stat.S_ISREG(node_metadata.st_mode)
            or node_metadata.st_mode & 0o111 == 0
        ):
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.OWNER_RUNTIME_UNAVAILABLE
            )
        return (
            runtime_bin,
            self._vp_home / "bin",
            Path("/usr/bin"),
            Path("/bin"),
            Path("/usr/sbin"),
            Path("/sbin"),
        )

    def _vite_which(self, *, executable_path: Sequence[Path]) -> _ViteWhich:
        result = self._runner.run(
            (str(self._vp_home / "bin" / "vp"), "env", "which", "codex"),
            executable_path=executable_path,
        )
        if result.returncode != 0:
            raise CodexViteDiscoveryError(CodexViteDiscoveryFailure.OWNER_UNRESOLVED)
        output = _ANSI_SGR.sub("", result.stdout)
        if any(
            character not in _ALLOWED_OUTPUT_CONTROLS
            and unicodedata.category(character) in _CONTROL_CATEGORIES
            for character in output
        ):
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.OWNER_METADATA_INVALID
            )
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        executables = [Path(line) for line in lines if line.startswith("/")]
        labels: dict[str, str] = {}
        for line in lines:
            match = re.fullmatch(r"([A-Za-z]+):\s+(.+)", line)
            if match is not None:
                label = match.group(1).lower()
                if label in labels:
                    raise CodexViteDiscoveryError(
                        CodexViteDiscoveryFailure.OWNER_METADATA_INVALID
                    )
                labels[label] = match.group(2).strip()
        if (
            len(executables) != 1
            or not _nonempty(labels.get("package"))
            or not _nonempty(labels.get("node"))
        ):
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.OWNER_METADATA_INVALID
            )
        executable = executables[0]
        try:
            canonical = executable.resolve(strict=True)
        except OSError as error:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.ACTIVE_INVOCATION_MISMATCH
            ) from error
        return _ViteWhich(
            executable=canonical,
            package=labels["package"],
            source=labels.get("source"),
            node_version=labels["node"],
        )

    def _npm_package_root(self, *, executable_path: Sequence[Path]) -> Path:
        result = self._runner.run(
            (str(self._vp_home / "bin" / "npm"), "root", "-g"),
            executable_path=executable_path,
        )
        if result.returncode != 0:
            raise CodexViteDiscoveryError(CodexViteDiscoveryFailure.OWNER_UNRESOLVED)
        lines = result.stdout.splitlines()
        if len(lines) != 1 or not Path(lines[0]).is_absolute():
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.OWNER_METADATA_INVALID
            )
        root = Path(lines[0]) / "@openai" / "codex"
        try:
            return root.resolve(strict=True)
        except OSError as error:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.OWNER_UNRESOLVED
            ) from error

    def _package_metadata(
        self, root: Path, *, directory_fd: int
    ) -> tuple[bytes, str, Path]:
        try:
            raw = _read_bounded_at(
                "package.json",
                CodexViteDiscoveryFailure.PACKAGE_IDENTITY_MISMATCH,
                directory_fd=directory_fd,
            )
        except FileNotFoundError as error:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.PACKAGE_IDENTITY_MISMATCH
            ) from error
        data = _json_object(raw, CodexViteDiscoveryFailure.PACKAGE_IDENTITY_MISMATCH)
        version = data.get("version")
        if data.get("name") != "@openai/codex" or not _nonempty(version):
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.PACKAGE_IDENTITY_MISMATCH
            )
        bin_value = data.get("bin")
        if isinstance(bin_value, Mapping):
            bin_relative = bin_value.get("codex")
        elif isinstance(bin_value, str):
            bin_relative = bin_value
        else:
            bin_relative = None
        if not _nonempty(bin_relative):
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.PACKAGE_IDENTITY_MISMATCH
            )
        relative = Path(str(bin_relative))
        if relative.is_absolute() or ".." in relative.parts:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.PACKAGE_IDENTITY_MISMATCH
            )
        try:
            executable = (root / relative).resolve(strict=True)
            executable.relative_to(root.resolve(strict=True))
        except (OSError, ValueError) as error:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.PACKAGE_IDENTITY_MISMATCH
            ) from error
        return raw, str(version), executable

    def _runtime_version(self, active: Path, *, executable_path: Sequence[Path]) -> str:
        result = self._runner.run(
            (str(active), "--version"), executable_path=executable_path
        )
        if result.returncode != 0:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.OWNER_RUNTIME_UNAVAILABLE
            )
        match = re.fullmatch(r"codex-cli ([^\s]+)\n?", result.stdout)
        if match is None:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.OWNER_RUNTIME_UNAVAILABLE
            )
        return match.group(1)

    def _validate_doctor(
        self,
        active: Path,
        version: str,
        package_root: Path,
        owner_identity: str,
        *,
        executable_path: Sequence[Path],
    ) -> None:
        result = self._runner.run(
            (str(active), "doctor", "--json"),
            executable_path=executable_path,
        )
        if result.returncode != 0:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.DOCTOR_CONTRACT_UNAVAILABLE
            )
        try:
            report = json.loads(result.stdout)
            checks = report["checks"]
            installation = checks["installation"]
            details = installation["details"]
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.DOCTOR_CONTRACT_UNAVAILABLE
            ) from error
        if report.get("schemaVersion") != 1 or installation.get("status") != "ok":
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.DOCTOR_CONTRACT_UNAVAILABLE
            )
        if report.get("codexVersion") != version:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.INSTALLED_VERSION_MISMATCH
            )
        if owner_identity != "vite-plus/npm-global":
            return
        try:
            update_status = checks["updates.status"]
            update_details = update_status["details"]
            npm_target = Path(details["npm update target"])
        except (KeyError, TypeError) as error:
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.DOCTOR_CONTRACT_UNAVAILABLE
            ) from error
        if update_status.get("status") != "ok":
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.DOCTOR_CONTRACT_UNAVAILABLE
            )
        if not npm_target.is_absolute():
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.DOCTOR_CONTRACT_UNAVAILABLE
            )
        try:
            npm_target = npm_target.resolve(strict=True)
        except OSError:
            npm_target = npm_target.absolute()
        if (
            details.get("managed by npm") != "true"
            or not str(details.get("install context", "")).startswith("npm")
            or update_details.get("update action") != "npm install -g @openai/codex"
            or npm_target != package_root
        ):
            raise CodexViteDiscoveryError(
                CodexViteDiscoveryFailure.OWNER_UPDATE_TARGET_MISMATCH
            )


def _read_bounded(path: Path, reason: CodexViteDiscoveryFailure) -> bytes:
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_METADATA_BYTES:
            raise CodexViteDiscoveryError(reason)
        with path.open("rb") as stream:
            payload = stream.read(_MAX_METADATA_BYTES + 1)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise CodexViteDiscoveryError(reason) from error
    if len(payload) > _MAX_METADATA_BYTES:
        raise CodexViteDiscoveryError(reason)
    return payload


def _read_bounded_at(
    name: str,
    reason: CodexViteDiscoveryFailure,
    *,
    directory_fd: int,
) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_METADATA_BYTES:
            raise CodexViteDiscoveryError(reason)
        chunks: list[bytes] = []
        size = 0
        while size <= _MAX_METADATA_BYTES:
            chunk = os.read(descriptor, _MAX_METADATA_BYTES - size + 1)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        final = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(
            getattr(metadata, field) != getattr(final, field) for field in stable_fields
        ):
            raise CodexViteDiscoveryError(reason)
        payload = b"".join(chunks)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise CodexViteDiscoveryError(reason) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > _MAX_METADATA_BYTES:
        raise CodexViteDiscoveryError(reason)
    return payload


def _json_object(
    payload: bytes, reason: CodexViteDiscoveryFailure
) -> Mapping[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodexViteDiscoveryError(reason) from error
    if not isinstance(value, Mapping):
        raise CodexViteDiscoveryError(reason)
    return value


def _package_tree_digest(root: Path) -> str:
    with _open_package_root(root, None) as directory_fd:
        return _package_tree_digest_fd(directory_fd)


@contextmanager
def _open_package_root(root: Path, trusted_base: Path | None) -> Iterator[int]:
    descriptor = -1
    try:
        flags = (
            os.O_RDONLY
            | os.O_CLOEXEC
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        if trusted_base is None:
            descriptor = os.open(root, flags)
        else:
            relative = root.relative_to(trusted_base)
            descriptor = os.open(trusted_base, flags)
            for part in relative.parts:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
        yield descriptor
    except (OSError, ValueError) as error:
        raise CodexViteDiscoveryError(
            CodexViteDiscoveryFailure.PACKAGE_IDENTITY_MISMATCH
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _package_tree_digest_fd(root_fd: int) -> str:
    try:
        records: list[dict[str, object]] = []
        size_bytes = 0
        for directory, directory_names, file_names, directory_fd in os.fwalk(
            ".",
            topdown=True,
            follow_symlinks=False,
            onerror=_raise_walk_error,
            dir_fd=root_fd,
        ):
            directory_names.sort()
            file_names.sort()
            directory_path = Path(directory)
            for name in (*directory_names, *file_names):
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                expected_type = (
                    stat.S_ISDIR(metadata.st_mode)
                    if name in directory_names
                    else stat.S_ISREG(metadata.st_mode)
                )
                if not expected_type:
                    raise CodexViteDiscoveryError(
                        CodexViteDiscoveryFailure.PACKAGE_IDENTITY_MISMATCH
                    )
            for name in file_names:
                if len(records) >= _MAX_PACKAGE_FILES:
                    raise CodexViteDiscoveryError(
                        CodexViteDiscoveryFailure.PACKAGE_IDENTITY_MISMATCH
                    )
                digest, file_size = _file_digest(
                    name,
                    directory_fd=directory_fd,
                    maximum_bytes=_MAX_PACKAGE_BYTES - size_bytes,
                )
                size_bytes += file_size
                records.append(
                    {
                        "path": ((directory_path / name).relative_to(".").as_posix()),
                        "sha256": digest,
                        "size": file_size,
                    }
                )
    except OSError as error:
        raise CodexViteDiscoveryError(
            CodexViteDiscoveryFailure.PACKAGE_IDENTITY_MISMATCH
        ) from error
    return canonical_fingerprint(records)


def _raise_walk_error(error: OSError) -> None:
    raise error


def _bytes_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _file_digest(
    name: str,
    *,
    directory_fd: int,
    maximum_bytes: int,
) -> tuple[str, int]:
    if maximum_bytes < 0:
        raise OSError("package tree exceeds verification bound")
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_fd,
    )
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode) or initial.st_size > maximum_bytes:
            raise OSError("package file is unavailable or exceeds verification bound")
        digest = hashlib.sha256()
        size = 0
        while size <= maximum_bytes:
            chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > maximum_bytes:
                raise OSError("package file exceeds verification bound")
            digest.update(chunk)
        final = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if size != initial.st_size or any(
            getattr(initial, field) != getattr(final, field) for field in stable_fields
        ):
            raise OSError("package file changed during verification")
        return "sha256:" + digest.hexdigest(), size
    finally:
        os.close(descriptor)


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _identity(value: object) -> str:
    if not _nonempty(value) or any(character.isspace() for character in str(value)):
        raise ValueError("selected identity must be nonempty")
    return str(value)
