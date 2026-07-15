"""Concrete supported-v1 adapters for a local macOS Supervisor."""

from __future__ import annotations

import os
import re
import socket
import stat
import subprocess
import time
from ipaddress import ip_address
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import urlsplit

import httpx
import psutil

from mlxctl.application.config_schema import MlxctlConfig
from mlxctl.domain.admission import PressureLevel
from mlxctl.domain.resources import InferenceService
from mlxctl.infrastructure.model_supply import (
    ModelInstallation as SuppliedModelInstallation,
)
from mlxctl.infrastructure.runtime_supply import (
    RuntimeInstallation as SuppliedRuntimeInstallation,
)
from mlxctl.infrastructure.runtime_supply import RuntimeLaunchBuilder
from mlxctl.infrastructure.supervisor_v1 import (
    CapabilityValidationError,
    PreparedLaunch,
    ProcessIdentity,
)


_SAFE_LOG_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class SubprocessManagedProcess:
    """Adapt one directly spawned subprocess to the Supervisor process port."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()

    def wait(self, timeout: float) -> int:
        try:
            return self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError from error


class PsutilManagedProcess:
    """Adapt an already-running PID to the Supervisor process port."""

    def __init__(self, process: psutil.Process) -> None:
        self._process = process

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        try:
            return self._process.wait(timeout=0)
        except psutil.TimeoutExpired:
            return None
        except psutil.NoSuchProcess:
            return 0

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()

    def wait(self, timeout: float) -> int:
        try:
            return self._process.wait(timeout=timeout)
        except psutil.TimeoutExpired as error:
            raise TimeoutError from error


class MacOSProcessLauncher:
    """Launch exact argv arrays with private, service-scoped output logs."""

    def __init__(
        self,
        *,
        log_dir: Path,
        base_environment: Mapping[str, str] | None = None,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        process_factory: Callable[[int], psutil.Process] = psutil.Process,
    ) -> None:
        self._log_dir = log_dir.expanduser()
        self._base_environment = dict(
            os.environ if base_environment is None else base_environment
        )
        self._popen = popen
        self._process_factory = process_factory

    def allocate_loopback_port(self, host: str) -> int:
        try:
            address = ip_address(host)
        except ValueError as error:
            raise ValueError(
                "port allocation requires a literal loopback IP"
            ) from error
        if not address.is_loopback:
            raise ValueError("port allocation requires a literal loopback IP")
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as listener:
            listener.bind((address.compressed, 0))
            return int(listener.getsockname()[1])

    def launch(
        self, argv: Sequence[str], environment: Mapping[str, str]
    ) -> SubprocessManagedProcess:
        exact_argv = _validate_argv(argv)
        merged_environment = {**self._base_environment, **dict(environment)}
        _prepare_private_directory(self._log_dir)
        service = merged_environment.get("MLXCTL_SERVICE_NAME", "runtime")
        if _SAFE_LOG_NAME.fullmatch(service) is None:
            service = "runtime"
        log_path = self._log_dir / f"{service}.log"
        descriptor = _open_private_log(log_path)
        log = os.fdopen(descriptor, "ab", buffering=0)
        try:
            process = self._popen(
                exact_argv,
                shell=False,
                env=merged_environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            log.close()
        return SubprocessManagedProcess(process)

    def attach(self, pid: int) -> PsutilManagedProcess | None:
        if type(pid) is not int or pid <= 0:
            return None
        try:
            process = self._process_factory(pid)
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return None
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None
        return PsutilManagedProcess(process)


class MacOSProcessProbe:
    """Observe PID birth identity and bounded OpenAI-compatible readiness."""

    def __init__(
        self,
        *,
        process_factory: Callable[[int], psutil.Process] = psutil.Process,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._process_factory = process_factory
        self._transport = transport

    def identity(self, process: SubprocessManagedProcess) -> ProcessIdentity:
        observed = self._process_factory(process.pid)
        return ProcessIdentity(process.pid, _birth_token(observed.create_time()))

    def identity_matches(self, identity: ProcessIdentity) -> bool:
        try:
            process = self._process_factory(identity.pid)
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return False
            return _birth_token(process.create_time()) == identity.birth_token
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False

    def is_ready(self, endpoint: str, timeout: float) -> bool:
        if timeout <= 0:
            raise ValueError("readiness timeout must be positive")
        readiness_url = _readiness_url(endpoint)
        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                response = client.get(readiness_url)
        except httpx.HTTPError:
            return False
        return 200 <= response.status_code < 300


class MacOSMemoryPressure:
    """Classify unified-memory headroom before macOS reaches swap distress."""

    def __init__(
        self,
        *,
        warning_available_ratio: float = 0.25,
        critical_available_ratio: float = 0.15,
        sample: Callable[[], object] = psutil.virtual_memory,
    ) -> None:
        if not 0 < critical_available_ratio < warning_available_ratio < 1:
            raise ValueError(
                "memory pressure thresholds require 0 < critical < warning < 1"
            )
        self._warning = warning_available_ratio
        self._critical = critical_available_ratio
        self._sample = sample

    def current(self) -> PressureLevel:
        memory = self._sample()
        total = getattr(memory, "total", 0)
        available = getattr(memory, "available", 0)
        if not isinstance(total, (int, float)) or total <= 0:
            return PressureLevel.CRITICAL
        if not isinstance(available, (int, float)) or available < 0:
            return PressureLevel.CRITICAL
        ratio = available / total
        if ratio <= self._critical:
            return PressureLevel.CRITICAL
        if ratio <= self._warning:
            return PressureLevel.WARNING
        return PressureLevel.NORMAL


class SystemClock:
    """Expose wall and monotonic system time through the Supervisor Clock port."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        time_ns: Callable[[], int] = time.time_ns,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._monotonic = monotonic
        self._time_ns = time_ns
        self._sleep = sleep

    def monotonic(self) -> float:
        return self._monotonic()

    def time_ns(self) -> int:
        return self._time_ns()

    def sleep(self, seconds: float) -> None:
        self._sleep(seconds)


class ConfigDesiredState:
    """Present the latest validated desired config through the Supervisor port."""

    def __init__(self, load_config: Callable[[], MlxctlConfig]) -> None:
        self._load_config = load_config

    def service(self, name: str) -> InferenceService | None:
        return self._load_config().services.get(name)

    def services(self) -> tuple[InferenceService, ...]:
        return tuple(
            sorted(
                self._load_config().services.values(),
                key=lambda service: str(service.name),
            )
        )


class ExactRuntimeLaunchSupply:
    """Build a launch from exact configured runtime and cached model identities."""

    def __init__(
        self,
        *,
        load_config: Callable[[], MlxctlConfig],
        runtime_installations: Mapping[str, SuppliedRuntimeInstallation],
        model_installations: Mapping[str, SuppliedModelInstallation],
        launch_builder: RuntimeLaunchBuilder,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._load_config = load_config
        self._runtime_installations = dict(runtime_installations)
        self._model_installations = dict(model_installations)
        self._launch_builder = launch_builder
        self._environment = dict(environment or {})

    def prepare_launch(
        self, service: InferenceService, host: str, port: int
    ) -> PreparedLaunch:
        _require_literal_loopback(host, "runtime launch")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("runtime launch port must be in 1..65535")
        config = self._load_config()
        configured_service = config.services.get(str(service.name))
        if configured_service != service:
            raise CapabilityValidationError(
                f"service '{service.name}' differs from current desired state"
            )
        configured_runtime = config.runtimes.get(service.runtime_installation)
        runtime = self._runtime_installations.get(service.runtime_installation)
        if configured_runtime is None or runtime is None:
            raise CapabilityValidationError(
                f"runtime installation '{service.runtime_installation}' is unavailable"
            )
        self._validate_runtime(configured_runtime, runtime)

        alias = config.aliases.get(str(service.model_alias))
        if alias is None:
            raise CapabilityValidationError(
                f"model alias '{service.model_alias}' is unavailable"
            )
        configured_model = config.models.get(alias.installation_name)
        model = self._model_installations.get(alias.installation_name)
        if configured_model is None or model is None:
            raise CapabilityValidationError(
                f"model installation '{alias.installation_name}' is not cached"
            )
        snapshot = self._validate_model(configured_model, model)
        options = self._resolve_model_artifacts(dict(service.options), snapshot)
        required_capabilities = frozenset({"model", "host", "port", *options.keys()})
        argv = self._launch_builder.build(
            runtime,
            model=str(snapshot),
            host=host,
            port=port,
            options=options,
        )
        return PreparedLaunch(
            argv=argv,
            environment={
                **self._environment,
                "MLXCTL_SERVICE_NAME": str(service.name),
                "HF_HUB_OFFLINE": "1",
            },
            required_capabilities=required_capabilities,
            observed_capabilities=runtime.capabilities,
        )

    @staticmethod
    def _validate_runtime(configured, runtime: SuppliedRuntimeInstallation) -> None:
        if runtime.installation_id != configured.installation_id:
            raise CapabilityValidationError("runtime installation identity mismatch")
        if runtime.runtime.replace("_", "-") != configured.definition.replace("_", "-"):
            raise CapabilityValidationError("runtime definition identity mismatch")
        if runtime.version != configured.version:
            raise CapabilityValidationError("runtime version identity mismatch")
        if runtime.provenance != configured.provenance:
            raise CapabilityValidationError("runtime provenance identity mismatch")
        if runtime.capabilities != configured.capabilities:
            raise CapabilityValidationError("runtime capability evidence mismatch")
        if runtime.bundle_id != configured.bundle_id:
            raise CapabilityValidationError("runtime bundle identity mismatch")
        try:
            root = runtime.root.expanduser().resolve(strict=True)
            launcher = Path(runtime.launcher[0]).expanduser().resolve(strict=True)
            configured_root = Path(configured.root).expanduser().resolve(strict=True)
            configured_launcher = (
                str(Path(configured.launcher[0]).expanduser().resolve(strict=True)),
                *configured.launcher[1:],
            )
            launcher.relative_to(root)
        except (FileNotFoundError, IndexError, ValueError) as error:
            raise CapabilityValidationError(
                "runtime launcher is not inside the exact installation"
            ) from error
        if configured_root != root:
            raise CapabilityValidationError("runtime root evidence mismatch")
        if configured_launcher != (str(launcher), *runtime.launcher[1:]):
            raise CapabilityValidationError("runtime launcher evidence mismatch")
        if not launcher.is_file() or not os.access(launcher, os.X_OK):
            raise CapabilityValidationError("runtime launcher is not executable")

    @staticmethod
    def _validate_model(configured, model: SuppliedModelInstallation) -> Path:
        revision = model.revision
        if model.installation_id != revision.revision_id:
            raise CapabilityValidationError("model installation identity mismatch")
        if revision.repo_id != configured.revision.repository:
            raise CapabilityValidationError("model repository identity mismatch")
        if revision.commit_sha.casefold() != configured.revision.revision.casefold():
            raise CapabilityValidationError("model revision identity mismatch")
        if model.provenance.resolved_sha.casefold() != revision.commit_sha.casefold():
            raise CapabilityValidationError("model provenance identity mismatch")
        if model.cached_revision_id != revision.revision_id:
            raise CapabilityValidationError("cached model revision identity mismatch")
        try:
            snapshot = model.snapshot_path.expanduser().resolve(strict=True)
        except FileNotFoundError as error:
            raise CapabilityValidationError(
                "exact cached model snapshot is unavailable"
            ) from error
        if not snapshot.is_dir():
            raise CapabilityValidationError(
                "exact cached model snapshot is not a directory"
            )
        return snapshot

    @staticmethod
    def _resolve_model_artifacts(
        options: dict[str, object], snapshot: Path
    ) -> dict[str, object]:
        kv_config = options.get("kv_config")
        if kv_config is None:
            return options
        if not isinstance(kv_config, str) or not kv_config:
            raise CapabilityValidationError("kv_config must name a cached model file")
        candidate = Path(kv_config).expanduser()
        if not candidate.is_absolute():
            candidate = snapshot / candidate
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(snapshot)
        except (FileNotFoundError, ValueError) as error:
            raise CapabilityValidationError(
                "kv_config must resolve inside the exact cached model snapshot"
            ) from error
        if not resolved.is_file():
            raise CapabilityValidationError("kv_config is not a file")
        options["kv_config"] = str(resolved)
        return options


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    exact = tuple(argv)
    if not exact or any(not isinstance(item, str) or not item for item in exact):
        raise ValueError("process launch requires a non-empty exact argv")
    return exact


def _birth_token(created_at: float) -> str:
    return f"psutil-create-time:{float(created_at).hex()}"


def _require_literal_loopback(host: str, purpose: str) -> None:
    try:
        address = ip_address(host)
    except ValueError as error:
        raise ValueError(f"{purpose} requires a literal loopback IP") from error
    if not address.is_loopback:
        raise ValueError(f"{purpose} requires a literal loopback IP")


def _readiness_url(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("readiness endpoint must be HTTP on a literal loopback IP")
    try:
        address = ip_address(parsed.hostname)
        port = parsed.port
    except ValueError as error:
        raise ValueError(
            "readiness endpoint must be HTTP on a literal loopback IP"
        ) from error
    if not address.is_loopback or port is None:
        raise ValueError("readiness endpoint must be HTTP on a literal loopback IP")
    return f"{endpoint.rstrip('/')}/v1/models"


def _prepare_private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"private log path is not a directory: {path}")
    if metadata.st_uid != os.getuid():
        raise PermissionError(f"private log path is not user-owned: {path}")
    os.chmod(path, 0o700, follow_symlinks=False)


def _open_private_log(path: Path) -> int:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"private service log is not a regular file: {path}")
        if metadata.st_uid != os.getuid():
            raise PermissionError(f"private service log is not user-owned: {path}")
        os.fchmod(descriptor, 0o600)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor
