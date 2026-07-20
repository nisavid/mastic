"""Concrete supported-v1 adapters for a local macOS Supervisor."""

from __future__ import annotations

import json
import os
import re
import selectors
import socket
import stat
import subprocess
import sys
import threading
import time
from ipaddress import ip_address
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

import httpx
import psutil

from mastic.application.config_schema import MasticConfig
from mastic.domain.admission import PressureLevel
from mastic.domain.resources import InferenceService
from mastic.infrastructure.model_supply import (
    ModelInstallation as SuppliedModelInstallation,
    VerificationResult,
)
from mastic.infrastructure.runtime_supply import (
    RuntimeInstallation as SuppliedRuntimeInstallation,
)
from mastic.infrastructure.runtime_supply import RuntimeLaunchBuilder
from mastic.infrastructure.supervisor_v1 import (
    CapabilityValidationError,
    PreparedLaunch,
    ProcessIdentity,
)


_SAFE_LOG_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
# Keep these framing limits paired with the dependency-free _launch_gate.py
# script, which must remain directly executable before package imports work.
_LAUNCH_FRAME_HEADER_BYTES = 8
_MAX_LAUNCH_PAYLOAD_BYTES = 1024 * 1024
_LAUNCH_COMMIT_TIMEOUT_SECONDS = 5.0
_LAUNCH_COMMIT_POLL_SECONDS = 0.05
_LAUNCH_WRITE_CHUNK_BYTES = 64 * 1024
_CONTENT_ADDRESSED_BLOB_NAME = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")
_ALLOWED_PROCESS_ENVIRONMENT = frozenset(
    {
        "HF_HOME",
        "HF_HUB_CACHE",
        "HF_HUB_OFFLINE",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "METAL_DEVICE_WRAPPER_TYPE",
        "MASTIC_SERVICE_NAME",
        "MLX_METAL_CACHE_DIR",
        "PATH",
        "TMPDIR",
        "TOKENIZERS_PARALLELISM",
        "TRANSFORMERS_CACHE",
        "XDG_CACHE_HOME",
    }
)


class SubprocessManagedProcess:
    """Adapt one directly spawned subprocess to the Supervisor process port."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        output_thread: threading.Thread | None = None,
    ) -> None:
        self._process = process
        self._output_thread = output_thread

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
            started = time.monotonic()
            result = self._process.wait(timeout=timeout)
            if self._output_thread is not None:
                remaining = max(0.0, timeout - (time.monotonic() - started))
                self._output_thread.join(remaining)
                if self._output_thread.is_alive():
                    raise TimeoutError
            return result
        except subprocess.TimeoutExpired as error:
            raise TimeoutError from error


class PendingSubprocessManagedProcess(SubprocessManagedProcess):
    """Hold a spawned exec gate closed until the launch identity is durable."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        output_thread: threading.Thread,
        control_descriptor: int,
        launch_frame: bytes,
        *,
        commit_timeout_seconds: float = _LAUNCH_COMMIT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(process, output_thread)
        if (
            isinstance(commit_timeout_seconds, bool)
            or not isinstance(commit_timeout_seconds, (int, float))
            or commit_timeout_seconds <= 0
        ):
            raise ValueError("launch commit timeout must be positive")
        self._control_descriptor: int | None = control_descriptor
        self._launch_frame = launch_frame
        self._commit_timeout_seconds = float(commit_timeout_seconds)
        self._control_lock = threading.Lock()
        self._committed = False
        self._commit_in_progress = False
        self._abort_requested = False

    def commit(self) -> None:
        with self._control_lock:
            if self._committed:
                return
            descriptor = self._control_descriptor
            if descriptor is None:
                raise RuntimeError("process launch was already aborted")
            if self._commit_in_progress:
                raise RuntimeError("process launch commit is already in progress")
            self._commit_in_progress = True
            frame = self._launch_frame

        deadline = time.monotonic() + self._commit_timeout_seconds
        selector = selectors.DefaultSelector()
        offset = 0
        try:
            selector.register(descriptor, selectors.EVENT_WRITE)
            while offset < len(frame):
                with self._control_lock:
                    if self._abort_requested:
                        raise RuntimeError("process launch was aborted")
                remaining_seconds = deadline - time.monotonic()
                if remaining_seconds <= 0:
                    raise TimeoutError("process launch gate commit timed out")
                try:
                    ready = selector.select(
                        min(remaining_seconds, _LAUNCH_COMMIT_POLL_SECONDS)
                    )
                except InterruptedError:
                    continue
                if not ready:
                    continue
                with self._control_lock:
                    if self._abort_requested:
                        raise RuntimeError("process launch was aborted")
                    if self._control_descriptor != descriptor:
                        raise RuntimeError("process launch was already aborted")
                    try:
                        written = os.write(
                            descriptor,
                            frame[offset : offset + _LAUNCH_WRITE_CHUNK_BYTES],
                        )
                    except (BlockingIOError, InterruptedError):
                        continue
                    if written <= 0:
                        raise OSError("process launch gate accepted no data")
                    offset += written
                    if offset == len(frame):
                        self._committed = True
                        self._close_control_locked()
        except Exception:
            with self._control_lock:
                if self._control_descriptor == descriptor:
                    self._close_control_locked()
            raise
        finally:
            selector.close()
            with self._control_lock:
                self._commit_in_progress = False
                self._launch_frame = b""

    def abort(self) -> None:
        with self._control_lock:
            if self._committed:
                return
            self._abort_requested = True
            if not self._commit_in_progress:
                self._close_control_locked()

    def terminate(self) -> None:
        try:
            self.abort()
        finally:
            super().terminate()

    def kill(self) -> None:
        try:
            self.abort()
        finally:
            super().kill()

    def _close_control_locked(self) -> None:
        descriptor = self._control_descriptor
        self._control_descriptor = None
        self._launch_frame = b""
        if descriptor is not None:
            os.close(descriptor)

    def __del__(self) -> None:
        try:
            self.abort()
        except Exception:
            # Destructors cannot report best-effort cleanup failures safely.
            return


class PsutilManagedProcess:
    """Adapt an already-running PID to the Supervisor process port."""

    def __init__(self, process: psutil.Process) -> None:
        self._process = process

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        try:
            if not self._process.is_running():
                return 0
            if self._process.status() in {psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE}:
                return 0
            return None
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return 0

    def terminate(self) -> None:
        try:
            self._process.terminate()
        except psutil.NoSuchProcess:
            # Termination is already complete when the process has exited.
            return

    def kill(self) -> None:
        try:
            self._process.kill()
        except psutil.NoSuchProcess:
            # Killing is already complete when the process has exited.
            return

    def wait(self, timeout: float) -> int:
        try:
            return self._process.wait(timeout=timeout)
        except psutil.TimeoutExpired as error:
            raise TimeoutError from error
        except psutil.NoSuchProcess:
            return 0


class MacOSProcessLauncher:
    """Launch exact argv arrays with private, service-scoped output logs."""

    def __init__(
        self,
        *,
        log_dir: Path,
        base_environment: Mapping[str, str] | None = None,
        max_log_bytes: int = 10 * 1024 * 1024,
        retained_log_files: int = 3,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        process_factory: Callable[[int], psutil.Process] = psutil.Process,
    ) -> None:
        self._log_dir = log_dir.expanduser()
        source_environment = (
            os.environ if base_environment is None else base_environment
        )
        self._base_environment = {
            key: value
            for key, value in source_environment.items()
            if key in _ALLOWED_PROCESS_ENVIRONMENT
        }
        if type(max_log_bytes) is not int or max_log_bytes <= 0:
            raise ValueError("maximum service log size must be a positive integer")
        if type(retained_log_files) is not int or retained_log_files < 0:
            raise ValueError("retained service log count must be nonnegative")
        self._max_log_bytes = max_log_bytes
        self._retained_log_files = retained_log_files
        self._log_lock = threading.Lock()
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
    ) -> PendingSubprocessManagedProcess:
        exact_argv = _validate_argv(argv)
        _validate_environment_strings(environment)
        unsupported = sorted(set(environment) - _ALLOWED_PROCESS_ENVIRONMENT)
        if unsupported:
            raise ValueError(
                "process environment variable is not allowlisted: "
                + ", ".join(unsupported)
            )
        merged_environment = {**self._base_environment, **dict(environment)}
        launch_frame = _encode_launch_frame(exact_argv, merged_environment)
        _prepare_private_directory(self._log_dir)
        service = merged_environment.get("MASTIC_SERVICE_NAME", "runtime")
        if _SAFE_LOG_NAME.fullmatch(service) is None:
            service = "runtime"
        log_path = self._log_dir / f"{service}.log"
        writer = _RotatingLogWriter(
            log_path,
            max_bytes=self._max_log_bytes,
            retained_files=self._retained_log_files,
            lock=self._log_lock,
        )
        read_descriptor, write_descriptor = os.pipe()
        pump = threading.Thread(
            target=_pump_process_log,
            args=(read_descriptor, writer),
            name=f"mastic-log-{service}",
            daemon=True,
        )
        pump.start()
        control_read_descriptor: int | None = None
        control_write_descriptor: int | None = None
        process: subprocess.Popen[bytes] | None = None
        try:
            control_read_descriptor, control_write_descriptor = os.pipe()
            gate = Path(__file__).with_name("_launch_gate.py")
            process = self._popen(
                (sys.executable, str(gate), str(control_read_descriptor)),
                shell=False,
                env=merged_environment,
                stdin=subprocess.DEVNULL,
                stdout=write_descriptor,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                pass_fds=(control_read_descriptor,),
            )
        finally:
            os.close(write_descriptor)
            if control_read_descriptor is not None:
                os.close(control_read_descriptor)
            if process is None and control_write_descriptor is not None:
                os.close(control_write_descriptor)
        if process is None or control_write_descriptor is None:
            raise RuntimeError("process launch gate did not start")
        try:
            os.set_blocking(control_write_descriptor, False)
        except Exception:
            os.close(control_write_descriptor)
            process.terminate()
            raise
        return PendingSubprocessManagedProcess(
            process, pump, control_write_descriptor, launch_frame
        )

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

    def __init__(self, load_config: Callable[[], MasticConfig]) -> None:
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


class ModelSecurityPort(Protocol):
    def require(self, repository: str, revision: str) -> Mapping[str, object]: ...

    def record_cached_verification(
        self,
        repository: str,
        revision: str,
        verification: VerificationResult,
    ) -> Mapping[str, object]: ...


class ExactRuntimeLaunchSupply:
    """Build a launch from exact configured runtime and cached model identities."""

    def __init__(
        self,
        *,
        load_config: Callable[[], MasticConfig],
        runtime_installations: Mapping[str, SuppliedRuntimeInstallation]
        | Callable[[], Mapping[str, SuppliedRuntimeInstallation]],
        model_installations: Mapping[str, SuppliedModelInstallation]
        | Callable[[], Mapping[str, SuppliedModelInstallation]],
        launch_builder: RuntimeLaunchBuilder,
        model_security: ModelSecurityPort,
        model_verifier: Callable[[SuppliedModelInstallation], VerificationResult],
        trust_grants: Callable[[], Sequence[Mapping[str, object]]] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._load_config = load_config
        self._runtime_installations = runtime_installations
        self._model_installations = model_installations
        self._launch_builder = launch_builder
        self._model_security = model_security
        self._model_verifier = model_verifier
        self._trust_grants = trust_grants or (lambda: ())
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
        runtime = self._current(self._runtime_installations).get(
            service.runtime_installation
        )
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
        model = self._current(self._model_installations).get(alias.installation_name)
        if configured_model is None or model is None:
            raise CapabilityValidationError(
                f"model installation '{alias.installation_name}' is unavailable"
            )
        snapshot = self._validate_model(configured_model, model)
        try:
            self._model_security.require(
                configured_model.revision.repository,
                configured_model.revision.revision,
            )
            assessment = self._model_security.record_cached_verification(
                configured_model.revision.repository,
                configured_model.revision.revision,
                self._model_verifier(model),
            )
        except Exception as error:
            raise CapabilityValidationError(
                "model launch security gate rejected the exact Model Revision"
            ) from error
        options = self._resolve_model_artifacts(dict(service.options), snapshot)
        self._require_remote_code_grant(
            options,
            model_installation=alias.installation_name,
            repository=configured_model.revision.repository,
            revision=configured_model.revision.revision,
            runtime_installation=service.runtime_installation,
            assessed_risks=assessment.get("overridable_risks", ()),
        )
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
                "MASTIC_SERVICE_NAME": str(service.name),
                "HF_HUB_OFFLINE": "1",
            },
            required_capabilities=required_capabilities,
            observed_capabilities=runtime.capabilities,
        )

    @staticmethod
    def _current(supply):
        return supply() if callable(supply) else supply

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
            metadata = model.snapshot_path.expanduser().lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise CapabilityValidationError(
                    "exact model snapshot must be a non-symlink directory"
                )
            if metadata.st_uid != os.getuid():
                raise CapabilityValidationError(
                    "exact model snapshot must be owned by the current user"
                )
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

    def _require_remote_code_grant(
        self,
        options: Mapping[str, object],
        *,
        model_installation: str,
        repository: str,
        revision: str,
        runtime_installation: str,
        assessed_risks: object,
    ) -> None:
        if options.get("trust_remote_code") is not True:
            return
        if not isinstance(assessed_risks, (tuple, list)) or not all(
            isinstance(item, str) for item in assessed_risks
        ):
            raise CapabilityValidationError("model security risk evidence is invalid")
        requested_risks = frozenset({"remote_code", *assessed_risks})
        for grant in self._trust_grants():
            accepted = grant.get("accepted_risks", ())
            if (
                grant.get("model_installation") == model_installation
                and grant.get("repository") == repository
                and grant.get("revision") == revision
                and grant.get("runtime_installation") == runtime_installation
                and isinstance(accepted, (tuple, list))
                and requested_risks.issubset(accepted)
            ):
                return
        raise CapabilityValidationError(
            "remote model code is not trusted for this exact Model Revision and "
            "Runtime Installation; grant "
            + ", ".join(sorted(requested_risks))
            + " with mastic model trust"
        )

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
            selected = Path(os.path.abspath(candidate))
            selected.relative_to(snapshot)
            resolved = selected.resolve(strict=True)
        except (FileNotFoundError, ValueError) as error:
            raise CapabilityValidationError(
                "kv_config must select an artifact from the exact cached model snapshot"
            ) from error
        if not _is_snapshot_artifact(snapshot, resolved):
            raise CapabilityValidationError(
                "kv_config must select an artifact from the exact cached model snapshot"
            )
        if not resolved.is_file():
            raise CapabilityValidationError("kv_config is not a file")
        options["kv_config"] = str(resolved)
        return options


def _is_snapshot_artifact(snapshot: Path, resolved: Path) -> bool:
    if resolved.is_relative_to(snapshot):
        return True
    repository_cache = snapshot.parent.parent
    return (
        snapshot.parent.name == "snapshots"
        and resolved.parent == repository_cache / "blobs"
        and _CONTENT_ADDRESSED_BLOB_NAME.fullmatch(resolved.name) is not None
    )


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    exact = tuple(argv)
    if not exact or any(not isinstance(item, str) or not item for item in exact):
        raise ValueError("process launch requires a non-empty exact argv")
    if any("\0" in item for item in exact):
        raise ValueError("process launch argv contains an embedded NUL")
    if not Path(exact[0]).is_absolute():
        raise ValueError("process launch executable must be an absolute path")
    return exact


def _encode_launch_frame(
    argv: tuple[str, ...], environment: Mapping[str, str]
) -> bytes:
    _validate_environment_strings(environment)
    payload = json.dumps(
        {"argv": argv, "environment": dict(environment)},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if not payload or len(payload) > _MAX_LAUNCH_PAYLOAD_BYTES:
        raise ValueError("process launch payload is too large")
    return len(payload).to_bytes(_LAUNCH_FRAME_HEADER_BYTES, "big") + payload


def _validate_environment_strings(environment: Mapping[str, str]) -> None:
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environment.items()
    ):
        raise ValueError("process environment requires string keys and values")
    if any("\0" in key or "\0" in value for key, value in environment.items()):
        raise ValueError("process environment contains an embedded NUL")


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


class _RotatingLogWriter:
    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int,
        retained_files: int,
        lock: threading.Lock,
    ) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._retained_files = retained_files
        self._lock = lock
        descriptor = _open_private_log(path)
        os.close(descriptor)

    def write(self, payload: bytes) -> None:
        remaining = memoryview(payload)
        with self._lock:
            while remaining:
                descriptor = _open_private_log(self._path)
                try:
                    size = os.fstat(descriptor).st_size
                    available = self._max_bytes - size
                    if available > 0:
                        written = os.write(descriptor, remaining[:available])
                        remaining = remaining[written:]
                finally:
                    os.close(descriptor)
                if remaining or available <= 0:
                    _rotate_private_logs(self._path, self._retained_files)


def _pump_process_log(descriptor: int, writer: _RotatingLogWriter) -> None:
    logging_enabled = True
    try:
        while payload := os.read(descriptor, 64 * 1024):
            if logging_enabled:
                try:
                    writer.write(payload)
                except OSError:
                    logging_enabled = False
    finally:
        os.close(descriptor)


def _rotate_private_logs(path: Path, retained_files: int) -> None:
    candidates = [
        path,
        *(_log_archive(path, index) for index in range(1, retained_files + 1)),
    ]
    for candidate in candidates:
        _validate_optional_private_log(candidate)
    if retained_files == 0:
        path.unlink(missing_ok=True)
        return
    oldest = _log_archive(path, retained_files)
    oldest.unlink(missing_ok=True)
    for index in range(retained_files - 1, 0, -1):
        source = _log_archive(path, index)
        if source.exists():
            os.replace(source, _log_archive(path, index + 1))
    if path.exists():
        os.replace(path, _log_archive(path, 1))


def _log_archive(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


def _validate_optional_private_log(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"private service log is not a regular file: {path}")
    if metadata.st_uid != os.getuid():
        raise PermissionError(f"private service log is not user-owned: {path}")
