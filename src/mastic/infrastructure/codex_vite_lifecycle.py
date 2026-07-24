"""Exact, artifact-verified mutation for Vite+-managed Codex installations."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import json
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from mastic.application.external_application_lifecycle import (
    InstallationDiscovery,
    InstallationDiscoveryError,
    OwnerUpgradeAction,
    OwnerUpgradeCommandError,
    OwnerUpgradeExecutionEvidence,
    OwnerUpgradeNotAttemptedFailure,
    OwnerUpgradeNotAttemptedError,
    OwnerUpgradeRequest,
    OwnerUpgradePreview,
    VerifiedArtifactClosure,
)
from mastic.domain.external_applications import (
    ExternalApplicationInstallation,
    InstallationObservation,
)
from mastic.infrastructure.owner_command_tracker import OwnerCommandTracker


_NODE_RUNTIME = re.compile(r"node:([0-9]+\.[0-9]+\.[0-9]+)\Z")
_NPM_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)
_ALLOWED_ENVIRONMENT = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "TMPDIR",
        "USER",
        "XDG_CACHE_HOME",
    }
)
_MAX_OWNER_COMMAND_CONTROL_BYTES = 64 * 1024


class OwnerUpgradeExecutionError(OwnerUpgradeCommandError):
    """Content-free operational failure from a Codex owner mutation."""


class ExactCommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> int: ...


class ArtifactClosureVerifier(Protocol):
    def prepare(
        self,
        closure: VerifiedArtifactClosure,
        owner_runtime_identity: str,
    ) -> None: ...

    def verify_staged(self, closure: VerifiedArtifactClosure) -> None: ...

    def verify_installed(
        self,
        closure: VerifiedArtifactClosure,
        observation: InstallationObservation,
    ) -> None: ...


class SubprocessExactCommandRunner:
    """Run one private-output command with finite process-group cleanup."""

    def __init__(
        self,
        *,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        tracker: OwnerCommandTracker | None = None,
    ) -> None:
        self._popen = popen
        self._tracker = tracker

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> int:
        exact_argv = _exact_argv(argv)
        exact_environment = _exact_environment(environment)
        if not cwd.is_absolute():
            raise ValueError("owner command working directory must be absolute")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("owner command timeout must be positive")
        marker = None
        process: subprocess.Popen[bytes] | None = None
        if self._tracker is None:
            process = self._spawn(
                exact_argv,
                cwd=cwd,
                environment=exact_environment,
            )
        else:
            release_payload = _owner_command_control_payload(exact_argv)
            control_read, control_write = os.pipe()
            launcher_argv = (
                sys.executable,
                "-m",
                "mastic.infrastructure.owner_command_helper",
                str(control_read),
            )
            try:
                process = self._spawn(
                    launcher_argv,
                    cwd=cwd,
                    environment=exact_environment,
                    pass_fds=(control_read,),
                )
                marker = self._tracker.record_prepared(
                    process.pid,
                    exact_argv,
                    cwd=cwd,
                    launcher_argv=launcher_argv,
                )
                _release_owner_command(control_write, release_payload)
            except OwnerUpgradeExecutionError:
                raise
            except Exception as error:
                if process is not None:
                    _terminate_process_group(process)
                raise OwnerUpgradeExecutionError(
                    "owner_command_tracking_failed"
                ) from error
            finally:
                os.close(control_read)
                os.close(control_write)
        if process is None:
            raise OwnerUpgradeExecutionError("owner_command_unavailable")
        try:
            returncode = int(process.wait(timeout=float(timeout_seconds)))
        except subprocess.TimeoutExpired as error:
            reaped = _terminate_process_group(process)
            if reaped and marker is not None:
                try:
                    self._tracker.record_finished(marker)
                except Exception:
                    # The timeout remains authoritative; retained tracking state
                    # makes the next attempt re-observe convergence fail-closed.
                    pass
            raise OwnerUpgradeExecutionError("owner_command_timed_out") from error
        except OSError as error:
            raise OwnerUpgradeExecutionError("owner_command_unavailable") from error
        if marker is not None:
            try:
                self._tracker.record_finished(marker)
            except Exception as error:
                raise OwnerUpgradeExecutionError(
                    "owner_command_tracking_failed"
                ) from error
        return returncode

    def _spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        pass_fds: tuple[int, ...] = (),
    ) -> subprocess.Popen[bytes]:
        try:
            return self._popen(
                argv,
                shell=False,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                pass_fds=pass_fds,
            )
        except OSError as error:
            raise OwnerUpgradeExecutionError("owner_command_unavailable") from error


def _owner_command_control_payload(argv: Sequence[str]) -> bytes:
    payload = json.dumps(tuple(argv), separators=(",", ":")).encode("utf-8")
    if len(payload) > _MAX_OWNER_COMMAND_CONTROL_BYTES:
        raise ValueError("owner command release payload exceeds configured bound")
    return payload


def _release_owner_command(control_fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(control_fd, payload[offset:])
        if written <= 0:
            raise OSError("owner command release pipe closed")
        offset += written


def _terminate_process_group(process: subprocess.Popen[bytes]) -> bool:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        # The process group may have exited before termination.
        pass
    try:
        process.wait(timeout=5.0)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


class CodexViteOwnerLifecycle:
    """Preserve the discovered Vite+ owner and verify its installed byte closure."""

    def __init__(
        self,
        *,
        vp_home: Path,
        discovery: InstallationDiscovery,
        artifact_verifier: ArtifactClosureVerifier,
        runner: ExactCommandRunner | None = None,
        base_environment: Mapping[str, str] | None = None,
        timeout_seconds: float = 15 * 60,
    ) -> None:
        self._vp_home = Path(vp_home)
        if not self._vp_home.is_absolute():
            raise ValueError("Vite home must be absolute")
        self._discovery = discovery
        self._artifact_verifier = artifact_verifier
        self._runner = runner or SubprocessExactCommandRunner()
        source = os.environ if base_environment is None else base_environment
        self._base_environment = {
            key: value for key, value in source.items() if key in _ALLOWED_ENVIRONMENT
        }
        if "HOME" not in self._base_environment:
            raise ValueError("owner command environment requires HOME")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("owner command timeout must be positive")
        self._timeout = float(timeout_seconds)

    def preview_action(
        self,
        observation: InstallationObservation,
        artifact_closure: VerifiedArtifactClosure,
    ) -> OwnerUpgradeAction:
        if observation.application_identity != "external-application:codex":
            raise ValueError("Codex owner lifecycle requires the Codex application")
        if observation.platform != "darwin" or observation.architecture != "arm64":
            raise ValueError("Codex owner lifecycle supports darwin/arm64")
        if observation.active_invocation != str(self._vp_home / "bin" / "codex"):
            raise ValueError(
                "Codex owner lifecycle requires the Vite active invocation"
            )
        if artifact_closure.application_identity != observation.application_identity:
            raise ValueError("artifact closure does not belong to Codex")
        return self._action(
            observation.owner_identity,
            observation.owner_runtime_identity,
            artifact_closure,
        )

    def verify_authorization_material(
        self,
        selected: ExternalApplicationInstallation,
        preview: OwnerUpgradePreview,
        artifact_closure: VerifiedArtifactClosure,
    ) -> bool:
        """Verify exact Codex/Vite closure and action before transition locking."""

        try:
            if (
                selected.application_identity != "external-application:codex"
                or selected.installation_identity
                != "application-installation:codex:vite"
                or selected.application_identity != preview.application_identity
                or selected.installation_identity != preview.installation_identity
                or selected.owner_identity != preview.owner_identity
                or selected.release_intent.channel != preview.release_channel
                or selected.platform != "darwin"
                or selected.platform != preview.platform
                or selected.architecture != "arm64"
                or selected.architecture != preview.architecture
                or artifact_closure.application_identity
                != selected.application_identity
            ):
                return False
            expected = self._action(
                preview.owner_identity,
                preview.owner_runtime_identity,
                artifact_closure,
            )
            return expected.fingerprint == preview.action.fingerprint
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    def apply_exact(
        self, request: OwnerUpgradeRequest
    ) -> OwnerUpgradeExecutionEvidence:
        try:
            expected = self._action(
                request.action.owner_identity,
                request.expected_owner_runtime_identity,
                request.artifact_closure,
            )
        except (TypeError, ValueError) as error:
            raise OwnerUpgradeNotAttemptedError("owner_action_invalid") from error
        if expected.fingerprint != request.action.fingerprint:
            raise OwnerUpgradeNotAttemptedError("owner_action_invalid")
        try:
            self._artifact_verifier.prepare(
                request.artifact_closure,
                request.expected_owner_runtime_identity,
            )
        except OwnerUpgradeCommandError as error:
            reason = _not_attempted_failure(
                error,
                fallback=OwnerUpgradeNotAttemptedFailure.ARTIFACT_PREPARATION_FAILED,
            )
            raise OwnerUpgradeNotAttemptedError(reason) from None
        try:
            self._artifact_verifier.verify_staged(request.artifact_closure)
        except OwnerUpgradeCommandError as error:
            reason = _not_attempted_failure(
                error,
                fallback=OwnerUpgradeNotAttemptedFailure.STAGED_ARTIFACT_CHANGED,
            )
            raise OwnerUpgradeNotAttemptedError(reason) from None
        try:
            current = self._discovery.discover(
                selected_installation_identity=request.installation_identity,
                selected_release_channel=request.release_channel,
            )
        except InstallationDiscoveryError as error:
            raise OwnerUpgradeNotAttemptedError(
                "expected_current_unavailable"
            ) from error
        if (
            current.owner_installation_identity
            != request.expected_owner_installation_identity
            or current.owner_runtime_identity != request.expected_owner_runtime_identity
            or current.state_fingerprint != request.expected_state_fingerprint
        ):
            raise OwnerUpgradeNotAttemptedError("expected_current_changed")
        returncode = self._runner.run(
            request.action.argv,
            cwd=request.action.cwd,
            environment=dict(request.action.environment),
            timeout_seconds=self._timeout,
        )
        if returncode != 0:
            raise OwnerUpgradeExecutionError("owner_command_failed")
        try:
            self._artifact_verifier.verify_staged(request.artifact_closure)
        except OwnerUpgradeCommandError as error:
            raise OwnerUpgradeExecutionError("staged_artifact_changed") from error
        try:
            after = self._discovery.discover(
                selected_installation_identity=request.installation_identity,
                selected_release_channel=request.release_channel,
            )
            if not _verified_target_matches(request, current, after):
                raise OwnerUpgradeExecutionError("installed_target_unverified")
            self._artifact_verifier.verify_installed(request.artifact_closure, after)
        except OwnerUpgradeExecutionError:
            raise
        except InstallationDiscoveryError as error:
            raise OwnerUpgradeExecutionError("installed_artifact_unverified") from error
        return OwnerUpgradeExecutionEvidence(
            request_fingerprint=request.fingerprint,
            artifact_closure_fingerprint=request.artifact_closure.fingerprint,
            verified_post_state_fingerprint=after.state_fingerprint,
        )

    def _action(
        self,
        owner_identity: str,
        owner_runtime_identity: str,
        closure: VerifiedArtifactClosure,
    ) -> OwnerUpgradeAction:
        if _NPM_VERSION.fullmatch(closure.exact_release) is None:
            raise ValueError("Codex target release must be an exact npm version")
        _validate_codex_closure(closure)
        runtime = _node_runtime(owner_runtime_identity)
        primary = closure.artifact("primary")
        local_spec = f"./{primary.staged_path.name}"
        environment = {
            **self._base_environment,
            "PATH": ":".join(
                (
                    str(self._vp_home / "bin"),
                    "/usr/bin",
                    "/bin",
                    "/usr/sbin",
                    "/sbin",
                )
            ),
            "VP_HOME": str(self._vp_home),
            "NO_COLOR": "1",
            "NPM_CONFIG_CACHE": str(closure.cache_directory),
            "NPM_CONFIG_IGNORE_SCRIPTS": "true",
            "NPM_CONFIG_AUDIT": "false",
            "NPM_CONFIG_FUND": "false",
            "NPM_CONFIG_ALLOW_FILE": "root",
            "NPM_CONFIG_PREFER_OFFLINE": "true",
            "NPM_CONFIG_REGISTRY": "https://registry.npmjs.org/",
            "NPM_CONFIG_USERCONFIG": "/dev/null",
            "NPM_CONFIG_GLOBALCONFIG": "/dev/null",
            "NPM_CONFIG_ALWAYS_AUTH": "false",
            "NPM_CONFIG_STRICT_SSL": "true",
        }
        if owner_identity == "vite-plus/npm-global":
            argv = (
                str(self._vp_home / "bin" / "vp"),
                "env",
                "exec",
                "--node",
                runtime,
                "--",
                str(self._vp_home / "bin" / "npm"),
                "install",
                "--global",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--allow-file=root",
                "--prefer-offline",
                "--cache",
                str(closure.cache_directory),
                "--",
                local_spec,
            )
            action_kind = "npm-global-install-verified-closure"
        elif owner_identity == "vite-plus/global-package":
            argv = (
                str(self._vp_home / "bin" / "vp"),
                "install",
                "--global",
                "--node",
                runtime,
                "--prefer-offline",
                local_spec,
            )
            action_kind = "vite-plus-global-install-verified-closure"
        else:
            raise ValueError("unsupported Codex Vite+ owner")
        return OwnerUpgradeAction(
            owner_identity=owner_identity,
            action_kind=action_kind,
            argv=argv,
            cwd=closure.staging_directory,
            environment=tuple(environment.items()),
            target_release=closure.exact_release,
            artifact_closure_fingerprint=closure.fingerprint,
        )


def _node_runtime(value: str) -> str:
    match = _NODE_RUNTIME.fullmatch(value)
    if match is None:
        raise ValueError("Codex Vite+ owner requires an exact Node runtime")
    return match.group(1)


def _not_attempted_failure(
    error: OwnerUpgradeCommandError,
    *,
    fallback: OwnerUpgradeNotAttemptedFailure,
) -> OwnerUpgradeNotAttemptedFailure:
    try:
        return OwnerUpgradeNotAttemptedFailure(error.reason_code)
    except ValueError:
        return fallback


def _verified_target_matches(
    request: OwnerUpgradeRequest,
    before: InstallationObservation,
    after: InstallationObservation,
) -> bool:
    return (
        after.application_identity == before.application_identity
        and after.installation_identity == request.installation_identity
        and after.owner_identity == request.action.owner_identity
        and after.owner_runtime_identity == request.expected_owner_runtime_identity
        and after.release_channel == request.release_channel
        and after.platform == before.platform
        and after.architecture == before.architecture
        and after.installed_release == request.action.target_release
        and after.active_invocation == before.active_invocation
        and after.reachable_invocations == before.reachable_invocations
    )


def _validate_codex_closure(closure: VerifiedArtifactClosure) -> None:
    if {artifact.role for artifact in closure.artifacts} != {"primary", "platform"}:
        raise ValueError("Codex requires the exact wrapper and platform closure")
    primary = closure.artifact("primary")
    platform = closure.artifact("platform")
    if (
        closure.application_identity != "external-application:codex"
        or primary.package_identity != "@openai/codex"
        or primary.exact_release != closure.exact_release
        or platform.package_identity != "@openai/codex-darwin-arm64"
        or platform.exact_release != f"{closure.exact_release}-darwin-arm64"
    ):
        raise ValueError("Codex artifact closure identities do not match")


def _exact_argv(argv: Sequence[str]) -> tuple[str, ...]:
    exact = tuple(argv)
    if not exact or not Path(exact[0]).is_absolute():
        raise ValueError("owner command executable must be absolute")
    if any(
        not isinstance(argument, str) or not argument or "\x00" in argument
        for argument in exact
    ):
        raise ValueError("owner command arguments must be nonempty strings")
    return exact


def _exact_environment(environment: Mapping[str, str]) -> dict[str, str]:
    exact = dict(environment)
    if any(
        not isinstance(key, str)
        or not key
        or "=" in key
        or "\x00" in key
        or not isinstance(value, str)
        or "\x00" in value
        for key, value in exact.items()
    ):
        raise ValueError("owner command environment must contain valid strings")
    return exact
