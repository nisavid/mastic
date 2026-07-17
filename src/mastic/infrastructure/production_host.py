"""Concrete host, application-target, setup, and filesystem adapters for production composition."""

from __future__ import annotations

import os
import platform
import plistlib
import shutil
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from pathlib import Path
from types import MappingProxyType

import httpx
import psutil
import tomlkit

from mastic.application.application_targets import SamplingProfile
from mastic.application.config_schema import (
    ApplicationTargetSettings,
    MasticConfig,
    validate_config,
)
from mastic.application.dispatch import ApplicationError
from mastic.application.setup import RemovalInventory, SetupPreflight
from mastic.infrastructure.application_target_integrations import (
    ApplicationTargetConfiguration,
    ApplicationTargetOwnershipDiscovery,
    ApplicationTargetOwnershipRecoveryRequired,
    CodexModelMetadata,
    LocalApplicationTargetIntegrationFactory,
    OwnershipDiscoveryPolicy,
)
from mastic.infrastructure.config_store import ConfigStore, private_file_lock
from mastic.infrastructure.gateway_credential import GatewayCredential
from mastic.infrastructure.launchd import LaunchdAdapter
from mastic.infrastructure.model_supply import (
    ModelInstallation,
    ModelProvenance,
    ModelRevision,
    ModelSupply,
)
from mastic.infrastructure.model_profiles import ModelProfileCatalogue
from mastic.infrastructure.operation_ports import ApplicationTargetOperationPort
from mastic.infrastructure.paths_v1 import MasticPaths
from mastic.infrastructure.state_store import OperationalStateStore


class AbsoluteUvRunner:
    """Run RuntimeManager's uv commands through one verified absolute executable."""

    def __init__(self, executable: Path) -> None:
        resolved = executable.expanduser().resolve(strict=True)
        metadata = resolved.stat()
        if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
            raise ValueError("uv executable must be an executable regular file")
        self._executable = resolved

    def run(self, argv: tuple[str, ...]) -> None:
        if not argv or argv[0] != "uv":
            raise ValueError("runtime installation runner accepts only uv commands")
        subprocess.run((str(self._executable), *argv[1:]), check=True, shell=False)


class ProductionLaunchdAdapter(LaunchdAdapter):
    """Add private Supervisor output to the safe inactive LaunchAgent."""

    def __init__(self, *, supervisor_log: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        if not supervisor_log.is_absolute():
            raise ValueError("Supervisor log path must be absolute")
        self._supervisor_log = supervisor_log

    def preview(self) -> bytes:
        payload = plistlib.loads(super().preview())
        payload.update(
            {
                "StandardErrorPath": str(self._supervisor_log),
                "StandardOutPath": str(self._supervisor_log),
                "Umask": 0o077,
            }
        )
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)

    def install(self) -> Path:
        parent = self._supervisor_log.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = parent.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise RuntimeError(
                "Supervisor log directory must be private and user-owned"
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self._supervisor_log, flags, 0o600)
        try:
            log_metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(log_metadata.st_mode)
                or log_metadata.st_uid != os.getuid()
            ):
                raise RuntimeError("Supervisor log must be a user-owned regular file")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        return super().install()


class OwnedStateRemover:
    """Remove only the exact product paths supplied by composition."""

    def __init__(self, owned_paths: Sequence[Path]) -> None:
        self._owned = frozenset(path.expanduser().absolute() for path in owned_paths)

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        if operation != "state.remove" or parameters.get("confirmed") is not True:
            raise ApplicationError(
                "confirmation_required", "product state removal requires confirmation"
            )
        requested = tuple(
            Path(str(item)).expanduser().absolute()
            for item in parameters.get("paths", ())
        )
        if any(path not in self._owned for path in requested):
            raise ApplicationError(
                "unsafe_path", "refusing to remove a path outside mastic ownership"
            )
        removed = []
        for path in requested:
            if path.is_symlink():
                raise ApplicationError(
                    "unsafe_path", f"refusing to follow product-state symlink: {path}"
                )
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            removed.append(str(path))
        return {"removed_paths": removed}


class SystemSetupPreflight:
    """Observe the current Mac without installing or starting anything."""

    def __init__(self, paths: MasticPaths, *, online_probe=None) -> None:
        self._paths = paths
        self._online_probe = online_probe or hub_online

    def __call__(self, offline: bool) -> SetupPreflight:
        memory = psutil.virtual_memory()
        return SetupPreflight(
            platform=platform.system().lower(),
            machine=platform.machine().lower(),
            memory_bytes=int(memory.total),
            disk_free_bytes=shutil.disk_usage(self._paths.data_dir).free,
            online=False if offline else self._online_probe(),
        )


class GatewayVerificationPort:
    """Send the setup gate through the public Gateway route."""

    def __init__(self, credential: GatewayCredential | None = None) -> None:
        self._credential = credential

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        if operation != "verify.request":
            raise ApplicationError("operation_unavailable", operation)
        endpoint = str(parameters["endpoint"]).rstrip("/")
        response = httpx.post(
            endpoint + "/chat/completions",
            json={
                "model": str(parameters["model"]),
                "messages": [{"role": "user", "content": str(parameters["request"])}],
                "temperature": 0,
                "max_tokens": 16,
            },
            headers=(
                {"authorization": self._credential.authorization_header()}
                if self._credential is not None
                else None
            ),
            timeout=120.0,
            follow_redirects=False,
            trust_env=False,
        )
        response.raise_for_status()
        payload = response.json()
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise RuntimeError(
                "Gateway returned an invalid chat-completions response"
            ) from error
        return {"ok": response.is_success, "text": str(text).strip()}


def application_target_port(
    home: Path,
    paths: MasticPaths,
    config_store: ConfigStore[MasticConfig],
    *,
    credential: GatewayCredential | None = None,
) -> ApplicationTargetOperationPort:
    gateway_credential = credential or GatewayCredential(paths.gateway_credential)
    factory = LocalApplicationTargetIntegrationFactory(
        codex_config_path=home / ".codex/config.toml",
        hindsight_profiles_dir=home / ".hindsight/profiles",
        ownership_dir=paths.state_dir / "application-targets",
    )

    def settings(name: str) -> ApplicationTargetSettings | None:
        if not config_store.exists:
            return None
        return config_store.load().value.application_targets.get(name)

    def configuration(
        name: str,
        parameters: Mapping[str, object],
        stored: ApplicationTargetSettings | None,
    ) -> ApplicationTargetConfiguration:
        config = (
            config_store.load().value
            if config_store.exists
            else validate_config({"schema_version": 1})
        )
        service_key = str(
            parameters.get("service") or (stored.service if stored else "")
        )
        service = config.services.get(service_key)
        if service is None:
            service = next(
                (
                    item
                    for item in config.services.values()
                    if str(item.route) == service_key
                ),
                None,
            )
        if service is None:
            raise ValueError(
                f"unknown Inference Service or Gateway route: {service_key!r}"
            )
        endpoint = str(
            parameters.get("endpoint")
            or f"http://{config.gateway.host}:{config.gateway.port}/v1"
        )
        alias = config.aliases[str(service.model_alias)]
        model = config.models[alias.installation_name]
        repository = model.revision.repository
        revision = model.revision.revision
        raw_sampling = parameters.get("sampling_profiles")
        if not isinstance(raw_sampling, Mapping):
            raw_sampling = stored.sampling if stored is not None else {}
        if not raw_sampling:
            raw_sampling = default_sampling(repository, revision, name)
        sampling = {
            str(profile): sampling_profile(value)
            for profile, value in raw_sampling.items()
        }
        gateway_credential.load_or_create()
        display_name = (
            "Qwen3.6 35B A3B OptiQ 4-bit"
            if repository == "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit"
            else repository.rsplit("/", 1)[-1]
        )
        context_window = coherent_application_target_context(
            service.options.get("max_context"),
            parameters.get("context_window", stored.context_window if stored else None),
        )
        stored_codex_provider = stored.provider if stored and name == "codex" else None
        if stored_codex_provider in {None, "mastic-local"}:
            stored_codex_provider = "mlx-local"
        return ApplicationTargetConfiguration(
            gateway_endpoint=endpoint,
            service_name=str(service.route),
            context_window=context_window,
            sampling_profiles=sampling,
            codex_provider_id=str(
                parameters.get(
                    "provider",
                    stored_codex_provider,
                )
            ),
            hindsight_provider=str(
                parameters.get(
                    "provider",
                    stored.provider if stored and name == "hindsight" else "openai",
                )
            ),
            max_concurrent=int(
                parameters.get(
                    "max_concurrent",
                    stored.max_concurrent if stored and stored.max_concurrent else 1,
                )
            ),
            credential_path=paths.gateway_credential,
            service_identity=str(service.name),
            codex_model=(
                CodexModelMetadata(
                    slug=str(service.route),
                    display_name=display_name,
                    description=f"Local mastic-managed route for {repository}.",
                )
                if name == "codex"
                else None
            ),
        )

    def record(name: str, value: ApplicationTargetSettings | None) -> None:
        if not config_store.exists:
            config_store.import_text("schema_version = 1\n")

        def edit(document) -> None:
            application_targets = document.setdefault(
                "application_targets", tomlkit.table()
            )
            if value is None:
                application_targets.pop(name, None)
                return
            table = tomlkit.table()
            table["kind"] = value.kind
            table["service"] = value.service
            if value.profile is not None:
                table["profile"] = value.profile
            if value.context_window is not None:
                table["context_window"] = value.context_window
            table["provider"] = value.provider
            if value.max_concurrent is not None:
                table["max_concurrent"] = value.max_concurrent
            sampling = tomlkit.table()
            for profile, profile_value in value.sampling.items():
                settings_table = tomlkit.table()
                for field in fields(profile_value):
                    item = getattr(profile_value, field.name)
                    if item is not None:
                        settings_table[field.name] = item
                sampling[profile] = settings_table
            table["sampling"] = sampling
            application_targets[name] = table

        config_store.edit(edit)

    return ApplicationTargetOperationPort(
        factory,
        configuration,
        request=lambda target, endpoint, route, payload: application_target_request(
            target,
            endpoint,
            route,
            payload,
            credential=gateway_credential,
        ),
        settings=settings,
        record=record,
        transition=lambda name: private_file_lock(
            paths.state_dir / "application-targets" / f"{name}.transition.lock"
        ),
    )


def coherent_application_target_context(
    service_context: object, requested_context: object
) -> int | None:
    service_value = optional_int(service_context)
    requested_value = optional_int(requested_context)
    if (
        requested_value is not None
        and service_value is not None
        and requested_value != service_value
    ):
        raise ValueError(
            "Application Configuration Target context_window must match the selected service max_context"
        )
    return requested_value or service_value


def application_target_request(
    target: str,
    endpoint: str,
    route: str,
    payload: Mapping[str, object],
    *,
    credential: GatewayCredential | None = None,
) -> Mapping[str, object]:
    if target == "codex":
        path = "/responses"
        body = {
            **dict(payload),
            "model": route,
            "input": "Respond with exactly: mastic gateway contract ok",
        }
    elif target == "hindsight":
        path = "/chat/completions"
        body = {
            **dict(payload),
            "model": route,
            "messages": [
                {
                    "role": "user",
                    "content": "Respond with exactly: mastic gateway contract ok",
                }
            ],
        }
    else:
        raise ValueError(f"unsupported Application Configuration Target: {target}")
    response = httpx.post(
        endpoint.rstrip("/") + path,
        json=body,
        headers=(
            {"authorization": credential.authorization_header()}
            if credential is not None
            else None
        ),
        timeout=120.0,
        follow_redirects=False,
        trust_env=False,
    )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, Mapping):
        raise RuntimeError("Gateway returned a non-object response")
    try:
        if target == "codex":
            output = value["output"]
            if not isinstance(output, list):
                raise TypeError("Responses output must be a list")
            message = next(
                item
                for item in output
                if isinstance(item, Mapping) and item.get("type") == "message"
            )
            content = message["content"]
            if not isinstance(content, list):
                raise TypeError("Responses message content must be a list")
            text = next(
                item["text"]
                for item in content
                if isinstance(item, Mapping) and item.get("type") == "output_text"
            )
        else:
            choices = value["choices"]
            if not isinstance(choices, list):
                raise TypeError("Chat Completions choices must be a list")
            first = choices[0]
            if not isinstance(first, Mapping):
                raise TypeError("Chat Completions choice must be an object")
            message = first["message"]
            if not isinstance(message, Mapping):
                raise TypeError("Chat Completions message must be an object")
            text = message["content"]
    except (KeyError, IndexError, StopIteration, TypeError) as error:
        contract = "Responses" if target == "codex" else "Chat Completions"
        raise RuntimeError(
            f"Gateway returned an invalid {contract} response"
        ) from error
    return {
        "ok": response.is_success,
        "text": str(text).strip(),
        "response": dict(value),
    }


def removal_inventory(
    paths: MasticPaths,
    launchd: LaunchdAdapter,
    config_store: ConfigStore[MasticConfig],
    supply: ModelSupply,
    home: Path,
) -> RemovalInventory:
    config = (
        config_store.load().value
        if config_store.exists
        else validate_config({"schema_version": 1})
    )
    running = tuple(
        sorted(
            {
                str(item.get("service", ""))
                for item in OperationalStateStore(paths.state_db).snapshots(
                    "service_run"
                )
                if item.get("state") in {"starting", "ready", "unhealthy"}
                and item.get("service")
            }
        )
    )
    owned = tuple(
        str(path)
        for path in (paths.config_dir, paths.state_dir, paths.data_dir, paths.log_dir)
        if path.exists()
    )
    cache = supply.inventory()
    return RemovalInventory(
        running_services=running,
        registered=launchd.status().registered,
        application_target_integrations=_removal_application_targets(
            paths, config, home
        ),
        product_owned_paths=owned,
        product_owned_bytes=sum(tree_size(Path(path)) for path in owned),
        shared_cache_paths=tuple(str(item.snapshot_path) for item in cache.revisions),
        shared_cache_bytes=sum(item.size_on_disk for item in cache.revisions),
        unrelated_settings=(str(home / ".codex"), str(home / ".hindsight")),
    )


def _removal_application_targets(
    paths: MasticPaths, config: MasticConfig, home: Path
) -> tuple[str, ...]:
    targets = set(config.application_targets)
    discovery = ApplicationTargetOwnershipDiscovery(
        codex_config_path=home / ".codex/config.toml",
        hindsight_profiles_dir=home / ".hindsight/profiles",
        ownership_dir=paths.state_dir / "application-targets",
    )
    try:
        ownership = discovery.discover(OwnershipDiscoveryPolicy.STRICT)
        desired_hindsight = config.application_targets.get("hindsight")
        discovery.reconcile_hindsight(
            desired_hindsight.profile if desired_hindsight is not None else None,
            ownership,
        )
    except (ApplicationTargetOwnershipRecoveryRequired, OSError, ValueError) as error:
        raise _removal_recovery_required("application targets") from error
    targets.update(item.integration for item in ownership)
    return tuple(sorted(targets))


def _removal_recovery_required(target: str) -> ApplicationError:
    return ApplicationError(
        "application_target_recovery_required",
        f"Application Configuration Target {target!r} requires manual recovery",
        next_actions=(
            "inspect the application-target ownership manifests",
            "resolve invalid or ambiguous recovery evidence before removing mastic",
        ),
    )


def sampling_profile(value: object) -> SamplingProfile:
    if isinstance(value, SamplingProfile):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("sampling profile must be an object")
    return SamplingProfile.from_mapping(value)


_APPLICATION_TARGET_PROFILE_BINDINGS = {
    "codex": {"coding": "precise-coding-thinking"},
    "hindsight": {
        "verification": "non-thinking",
        "retain": "non-thinking",
        "reflect": "general-thinking",
        "consolidation": "non-thinking",
    },
}


def default_sampling(
    repository: str, revision: str, name: str
) -> Mapping[str, SamplingProfile]:
    bindings = _APPLICATION_TARGET_PROFILE_BINDINGS.get(name)
    if bindings is None:
        raise ValueError(f"unsupported Application Configuration Target: {name}")
    catalogue = ModelProfileCatalogue.load_builtin()
    result = {}
    for workload, profile_name in bindings.items():
        try:
            profile = catalogue.profile(repository, revision, profile_name)
        except KeyError:
            return {}
        result[workload] = SamplingProfile(
            **dict(profile.parameters),
            upstream_profile=profile.name,
            source_url=profile.source_url,
            source_revision=profile.source_revision,
        )
    return MappingProxyType(result)


def configured_model_installations(
    config: MasticConfig, inventory
) -> Mapping[str, ModelInstallation]:
    cached_revisions = {
        (item.repo_id, item.commit_sha): item for item in inventory.revisions
    }
    result = {}
    for name, item in config.models.items():
        revision = ModelRevision(
            item.revision.repository,
            item.revision.revision,
            item.revision.revision,
            "desired-state",
        )
        if item.provenance == "adopted":
            assert item.path is not None
            result[name] = ModelInstallation(
                revision.revision_id,
                revision,
                revision.revision_id,
                Path(item.path),
                ModelProvenance(
                    item.revision.revision,
                    item.revision.revision,
                    "external-adopted",
                ),
            )
        else:
            cached = cached_revisions.get(
                (item.revision.repository, item.revision.revision)
            )
            if cached is None:
                continue
            result[name] = ModelInstallation(
                revision.revision_id,
                revision,
                cached.revision_id,
                cached.snapshot_path,
                ModelProvenance(
                    item.revision.revision,
                    item.revision.revision,
                    "desired-state",
                ),
            )
    return result


def hub_online() -> bool:
    try:
        response = httpx.head(
            "https://huggingface.co/",
            timeout=3.0,
            follow_redirects=False,
            trust_env=False,
        )
    except httpx.HTTPError:
        return False
    return response.status_code < 500


def resolve_uv(home: Path) -> Path:
    candidates = []
    configured = os.environ.get("MASTIC_UV_EXECUTABLE")
    if configured:
        candidates.append(Path(configured))
    discovered = shutil.which("uv")
    if discovered:
        candidates.append(Path(discovered))
    candidates.extend(
        (
            home / ".local/bin/uv",
            Path("/opt/homebrew/bin/uv"),
            Path("/usr/local/bin/uv"),
        )
    )
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if stat.S_ISREG(metadata.st_mode) and os.access(resolved, os.X_OK):
            return resolved
    raise FileNotFoundError(
        "uv is required for Runtime Installation; set MASTIC_UV_EXECUTABLE "
        "to its absolute path"
    )


def optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError("value must be an integer")
    return value


def tree_size(root: Path) -> int:
    if not root.exists() or root.is_symlink():
        return 0
    if root.is_file():
        return root.stat().st_size
    return sum(
        item.stat().st_size
        for item in root.rglob("*")
        if item.is_file() and not item.is_symlink()
    )


def plain(value: object) -> dict[str, object]:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    if isinstance(value, Mapping):
        return dict(value)
    return {"value": value}
