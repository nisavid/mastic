"""Application-native canaries for owned Codex and Hindsight configurations."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from mastic.application.config_schema import ApplicationTargetSettings
from mastic.application.dispatch import ApplicationError
from mastic.infrastructure.application_target_integrations import (
    ApplicationTargetConfiguration,
)


_EXPECTED_TEXT = "mastic gateway contract ok"
_MAX_CAPTURE_BYTES = 64 * 1024
_CAPTURE_LIMIT_BLOCKS = _MAX_CAPTURE_BYTES // 512
_SAFE_AMBIENT_ENVIRONMENT_KEYS = ("LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")


@dataclass(frozen=True, slots=True)
class ApplicationCanaryResult:
    """Sanitized evidence that an application completed its native contract."""

    target: str
    phases: tuple[str, ...]
    exact_contract: bool
    duration_seconds: float
    evidence_sha256: str


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
ProcessSpawner = Callable[..., subprocess.Popen[bytes]]
ExecutableResolver = Callable[[str], Path]


class NativeApplicationTargetCanary:
    """Dispatch one bounded canary without reading application credentials."""

    def __init__(
        self,
        home: Path,
        *,
        uv_executable: Path | None = None,
        resolve_executable: ExecutableResolver | None = None,
        run_command: CommandRunner = subprocess.run,
        spawn_process: ProcessSpawner = subprocess.Popen,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._home = home
        self._uv = uv_executable
        self._resolve = resolve_executable or (
            lambda name: _resolve_executable(name, home=self._home)
        )
        self._run = run_command
        self._spawn = spawn_process
        self._monotonic = monotonic
        self._sleep = sleep

    def run(
        self,
        target: str,
        configuration: ApplicationTargetConfiguration,
        settings: ApplicationTargetSettings,
        *,
        profile: str,
    ) -> Mapping[str, object]:
        started = self._monotonic()
        if target == "codex":
            if profile != "coding":
                raise _canary_error(
                    "application_target_canary_unsupported",
                    f"Codex has no native {profile!r} canary",
                )
            phases = self._run_codex()
        elif target == "hindsight":
            if profile != "retain":
                raise _canary_error(
                    "application_target_canary_unsupported",
                    f"Hindsight has no bounded native {profile!r} canary",
                )
            phases = self._run_hindsight(settings)
        else:
            raise ApplicationError(
                "invalid_parameter", f"unsupported application canary: {target}"
            )
        duration = max(0.0, self._monotonic() - started)
        evidence = application_canary_evidence_sha256(
            target=target,
            profile=profile,
            service=configuration.service_name,
            phases=phases,
            exact_contract=True,
        )
        return _plain_result(
            ApplicationCanaryResult(target, phases, True, duration, evidence)
        )

    def _run_codex(self) -> tuple[str, ...]:
        codex = self._available("codex")
        with tempfile.TemporaryDirectory(prefix="mastic-codex-canary-") as raw:
            root = Path(raw)
            work = root / "work"
            work.mkdir(mode=0o700)
            output = root / "result.txt"
            command = [
                str(codex),
                "exec",
                "--ephemeral",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "-c",
                'approval_policy="never"',
                "-c",
                'shell_environment_policy.inherit="none"',
                "--color",
                "never",
                "--output-last-message",
                str(output),
                "-C",
                str(work),
                "This is a health check. Do not call tools or inspect files. "
                "Respond with exactly: mastic gateway contract ok",
            ]
            try:
                completed = self._run(
                    _bounded_file_command(command),
                    cwd=work,
                    env=_isolated_environment(
                        root,
                        {"CODEX_HOME": str(self._home / ".codex")},
                    ),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=180.0,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise _canary_error(
                    "application_target_canary_timeout",
                    "Codex did not complete its application-native canary in time",
                ) from error
            if completed.returncode != 0:
                raise _canary_error(
                    "application_target_canary_failed",
                    "Codex did not complete its application-native canary",
                )
            try:
                if (
                    not output.is_file()
                    or output.is_symlink()
                    or output.stat().st_size > _MAX_CAPTURE_BYTES
                ):
                    raise OSError("missing regular result file")
                result = output.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError) as error:
                raise _canary_error(
                    "application_target_canary_failed",
                    "Codex did not produce a valid bounded canary result",
                ) from error
            if result != _EXPECTED_TEXT:
                raise _canary_error(
                    "application_target_canary_failed",
                    "Codex did not return the exact application canary response",
                )
        return ("codex.exec", "responses.exact")

    def _run_hindsight(self, settings: ApplicationTargetSettings) -> tuple[str, ...]:
        if not settings.profile:
            raise _canary_error(
                "application_target_canary_failed",
                "Hindsight has no managed environment profile",
            )
        uv = self._uv or self._available("uv")
        api = self._available("hindsight-api")
        hindsight = self._available("hindsight")
        profile_path = (
            self._home / ".hindsight" / "profiles" / f"{settings.profile}.env"
        )
        if not profile_path.is_file() or profile_path.is_symlink():
            raise _canary_error(
                "application_unavailable",
                "The managed Hindsight environment profile is unavailable",
            )
        with tempfile.TemporaryDirectory(prefix="mastic-hindsight-canary-") as raw:
            root = Path(raw)
            for attempt in range(3):
                port = _loopback_port()
                endpoint = f"http://127.0.0.1:{port}"
                environment = _isolated_environment(
                    root,
                    {
                        "HINDSIGHT_API_DATABASE_URL": "pg0://mastic-canary",
                        "HINDSIGHT_API_HOST": "127.0.0.1",
                        "HINDSIGHT_API_PORT": str(port),
                        "HINDSIGHT_API_URL": endpoint,
                    },
                )
                command = [
                    str(uv),
                    "run",
                    "--no-project",
                    "--no-config",
                    "--env-file",
                    str(profile_path),
                    "--",
                    str(api),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--log-level",
                    "warning",
                    "--no-access-log",
                ]
                process = self._spawn(
                    command,
                    cwd=root,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                try:
                    self._await_hindsight(hindsight, environment, process, 90.0)
                except ApplicationError:
                    if process.poll() is None:
                        self._stop_process(process)
                        raise
                    process.wait(timeout=5.0)
                    if attempt == 2:
                        raise
                    continue
                break
            try:
                nonce = uuid.uuid4().hex
                bank = f"mastic-canary-{nonce}"
                document = f"mastic-canary-{nonce}"
                created = self._hindsight_command(
                    hindsight,
                    environment,
                    ("bank", "create", bank, "--name", "MASTIC canary", "-o", "json"),
                    60.0,
                )
                if str(created.get("bank_id", created.get("id", ""))) != bank:
                    raise ValueError("bank identity mismatch")
                retained = self._hindsight_command(
                    hindsight,
                    environment,
                    (
                        "memory",
                        "retain",
                        bank,
                        f"The MASTIC canary value is {nonce}.",
                        "--doc-id",
                        document,
                        "--timestamp",
                        "unset",
                        "-o",
                        "json",
                    ),
                    180.0,
                )
                if retained.get("success") is not True:
                    raise ValueError("retain did not report success")
                reflected = self._hindsight_command(
                    hindsight,
                    environment,
                    (
                        "memory",
                        "reflect",
                        bank,
                        "Respond with exactly: mastic gateway contract ok",
                        "--budget",
                        "low",
                        "--max-tokens",
                        "64",
                        "-o",
                        "json",
                    ),
                    180.0,
                )
                text = reflected.get("text", reflected.get("response"))
                if text != _EXPECTED_TEXT:
                    raise ValueError("reflect response mismatch")
            except ApplicationError:
                raise
            except (
                KeyError,
                OSError,
                UnicodeError,
                ValueError,
                json.JSONDecodeError,
            ) as error:
                raise _canary_error(
                    "application_target_canary_failed",
                    "Hindsight did not complete its disposable native canary",
                ) from error
            finally:
                self._stop_process(process)
        return ("hindsight.start", "bank.create", "memory.retain", "memory.reflect")

    def _await_hindsight(
        self,
        hindsight: Path,
        environment: Mapping[str, str],
        process: subprocess.Popen[bytes],
        timeout: float,
    ) -> None:
        deadline = self._monotonic() + timeout
        while self._monotonic() < deadline:
            if process.poll() is not None:
                raise _canary_error(
                    "application_target_canary_failed",
                    "The disposable Hindsight server exited before becoming ready",
                )
            try:
                completed = self._run(
                    [str(hindsight), "health", "-o", "json"],
                    env=dict(environment),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=min(5.0, max(0.1, deadline - self._monotonic())),
                    check=False,
                )
                if completed.returncode == 0:
                    return
            except subprocess.TimeoutExpired:
                pass
            self._sleep(0.1)
        raise _canary_error(
            "application_target_canary_timeout",
            "The disposable Hindsight server did not become ready in time",
        )

    def _hindsight_command(
        self,
        executable: Path,
        environment: Mapping[str, str],
        arguments: Sequence[str],
        timeout: float,
    ) -> Mapping[str, object]:
        with tempfile.TemporaryFile() as captured:
            try:
                completed = self._run(
                    _bounded_file_command([str(executable), *arguments]),
                    env=dict(environment),
                    stdout=captured,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise _canary_error(
                    "application_target_canary_timeout",
                    "Hindsight did not complete a disposable canary phase in time",
                ) from error
            size = captured.tell()
            if completed.returncode != 0 or size > _MAX_CAPTURE_BYTES:
                raise _canary_error(
                    "application_target_canary_failed",
                    "Hindsight did not complete a disposable canary phase",
                )
            captured.seek(0)
            value = json.loads(captured.read().decode("utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("Hindsight returned a non-object result")
        return value

    def _stop_process(self, process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            process.wait(timeout=5.0)
            return
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                process.wait(timeout=5.0)
                return
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired as error:
                raise _canary_error(
                    "application_target_cleanup_failed",
                    "The disposable Hindsight process group could not be removed",
                ) from error

    def _available(self, name: str) -> Path:
        try:
            return self._resolve(name)
        except (OSError, ValueError) as error:
            raise _canary_error(
                "application_unavailable",
                f"Required application executable {name!r} is unavailable",
            ) from error


def _resolve_executable(name: str, *, home: Path | None = None) -> Path:
    conventional = home / ".local/bin" / name if home is not None else None
    raw = (
        str(conventional)
        if conventional is not None
        and conventional.is_file()
        and not conventional.is_symlink()
        else shutil.which(name)
    )
    if raw is None:
        raise FileNotFoundError(name)
    path = Path(raw).resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"invalid executable: {name}")
    return path


def _isolated_environment(home: Path, additions: Mapping[str, str]) -> dict[str, str]:
    environment = {"HOME": str(home), "PATH": os.defpath}
    for key in _SAFE_AMBIENT_ENVIRONMENT_KEYS:
        if value := os.environ.get(key):
            environment[key] = value
    environment.update(additions)
    return environment


def _bounded_file_command(command: Sequence[str]) -> list[str]:
    """Run a macOS canary with an inherited hard regular-file size limit."""
    return [
        "/bin/zsh",
        "-f",
        "-c",
        f'ulimit -f {_CAPTURE_LIMIT_BLOCKS}; exec "$@"',
        "mastic-canary",
        *command,
    ]


def _loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _canary_error(code: str, message: str) -> ApplicationError:
    return ApplicationError(
        code,
        message,
        next_actions=("mastic application-target inspect --help", "mastic doctor"),
    )


def _plain_result(result: ApplicationCanaryResult) -> Mapping[str, object]:
    return {
        "target": result.target,
        "phases": list(result.phases),
        "exact_contract": result.exact_contract,
        "ok": result.exact_contract,
        "duration_seconds": result.duration_seconds,
        "evidence_sha256": result.evidence_sha256,
    }


def application_canary_evidence_sha256(
    *,
    target: str,
    profile: str,
    service: str,
    phases: Sequence[str],
    exact_contract: bool,
) -> str:
    """Digest the content-free native canary contract."""

    return hashlib.sha256(
        json.dumps(
            {
                "target": target,
                "profile": profile,
                "service": service,
                "phases": list(phases),
                "exact_contract": exact_contract,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
