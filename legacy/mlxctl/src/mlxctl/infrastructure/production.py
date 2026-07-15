"""Production composition for the supported-v1 local inference manager."""

from __future__ import annotations

import sys
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mlxctl.application.config_schema import MlxctlConfig, validate_config
from mlxctl.application.dispatch import ApplicationError, OperationRequest
from mlxctl.application.setup import (
    ExactSetupSelection,
    RecommendedProfile,
    SetupPlanner,
)
from mlxctl.infrastructure.composition import (
    ApplicationComposition,
    compose_application,
)
from mlxctl.infrastructure.config_store import ConfigStore
from mlxctl.infrastructure.control_client import UnixControlClient
from mlxctl.infrastructure.daemon_service import DaemonOperationRouter, DaemonService
from mlxctl.infrastructure.gateway_runtime import GatewayRuntime
from mlxctl.infrastructure.gateway_credential import GatewayCredential
from mlxctl.infrastructure.host_integration import LaunchdSupervisorActivator
from mlxctl.infrastructure.launchd import LaunchdAdapter
from mlxctl.infrastructure.model_intelligence import (
    HuggingFaceModelRepository,
    ModelIntelligence,
    PsutilMachineInventory,
)
from mlxctl.infrastructure.model_supply import (
    HuggingFaceHubClient,
    ModelInstallation,
    ModelSupply,
)
from mlxctl.infrastructure.operation_ports import (
    RemoteOperationPort,
    SupervisorOperationPort,
)
from mlxctl.infrastructure.paths_v1 import MlxctlPaths, resolve_paths
from mlxctl.infrastructure.production_host import (
    AbsoluteUvRunner,
    GatewayVerificationPort,
    OwnedStateRemover,
    ProductionLaunchdAdapter,
    SystemSetupPreflight,
    client_port,
    configured_model_installations,
    plain,
    removal_inventory,
    resolve_uv,
)
from mlxctl.infrastructure.runtime_supply import (
    RuntimeCatalogue,
    RuntimeInstallation,
    RuntimeLaunchBuilder,
    RuntimeManager,
    SubprocessRuntimeProbe,
)
from mlxctl.infrastructure.setup_port import (
    OperationalSetupEvidenceStore,
    SetupOperationPort,
)
from mlxctl.infrastructure.state_store import OperationalStateStore
from mlxctl.infrastructure.supply_ports import (
    ExactRevisionModelSecurity,
    ModelSupplyPort,
    RuntimeSupplyPort,
    inspect_adopted_snapshot,
    verify_adopted_snapshot,
)
from mlxctl.infrastructure.supervisor_v1 import Supervisor
from mlxctl.infrastructure.system_adapters import (
    ConfigDesiredState,
    ExactRuntimeLaunchSupply,
    MacOSMemoryPressure,
    MacOSProcessLauncher,
    MacOSProcessProbe,
    SystemClock,
)


LAUNCHD_LABEL = "io.nisavid.mlxd"
_DEFAULT_MODEL = "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit"
_DEFAULT_MODEL_REVISION = "70a3aa32c7feef511182bf16aa332f37e8d82014"


class OperationOwner(Protocol):
    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]: ...


class _LocalModelSupply:
    """Keep search/inspection local while sending physical mutations to mlxd."""

    def __init__(
        self,
        supply: ModelSupply,
        remote: OperationOwner,
        security: ExactRevisionModelSecurity,
        *,
        adoption_forbidden_roots: tuple[Path, ...] = (),
    ) -> None:
        self._supply = supply
        self._remote = remote
        self._security = security
        self._adoption_forbidden_roots = adoption_forbidden_roots

    def search(self, query: str, *, mode: str = "curated", limit: int = 20):
        return self._supply.search(query, mode=mode, limit=limit)

    def inventory(self):
        return self._supply.inventory()

    def resolve(self, repo_id: str, revision: str, *, offline: bool = False):
        return self._supply.resolve(repo_id, revision, offline=offline)

    def inspect_adoption(self, path: str):
        return inspect_adopted_snapshot(
            path,
            forbidden_roots=self._adoption_forbidden_roots,
            cached_roots=tuple(
                revision.snapshot_path
                for revision in self._supply.inventory().revisions
            ),
        )

    def verify(self, installation: ModelInstallation):
        if installation.provenance.source == "external-adopted":
            assessment = self._security.require(
                installation.revision.repo_id, installation.revision.commit_sha
            )
            verification = verify_adopted_snapshot(
                installation.snapshot_path, assessment
            )
        else:
            assessment = self._security.inspect(
                installation.revision.repo_id, installation.revision.commit_sha
            )
            verification = self._supply.verify(installation)
        self._security.record_verification(assessment, verification)
        return verification

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        return self._remote.execute(operation, parameters)


class _DeferredOperationOwner:
    """Break the setup/application construction cycle without hiding execution."""

    def __init__(self) -> None:
        self._owner: OperationOwner | None = None

    def bind(self, owner: OperationOwner) -> None:
        if self._owner is not None:
            raise RuntimeError("operation owner is already bound")
        self._owner = owner

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        if self._owner is None:
            raise RuntimeError("operation owner is not bound")
        return self._owner.execute(operation, parameters)


class _ActivatingOperationOwner:
    """Activate mlxd exactly when a setup step crosses a remote mutation boundary."""

    def __init__(
        self, activator: LaunchdSupervisorActivator, remote: OperationOwner
    ) -> None:
        self._activator = activator
        self._remote = remote

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        self._activator.activate()
        return self._remote.execute(operation, parameters)


class _LocalSupervisorOwner:
    """Forward lifecycle work without starting a Supervisor just to stop it."""

    def __init__(self, remote: OperationOwner, launchd: LaunchdAdapter) -> None:
        self._remote = remote
        self._launchd = launchd

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        if operation == "supervisor.stop" and not self._launchd.status().running:
            return {"state": "stopped", "already_stopped": True}
        return self._remote.execute(operation, parameters)


class _DispatcherOwner:
    def __init__(
        self,
        application: ApplicationComposition,
        config_store: ConfigStore[MlxctlConfig],
        state_remover: OperationOwner,
    ) -> None:
        self._application = application
        self._config_store = config_store
        self._state_remover = state_remover

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        if operation == "state.remove":
            return self._state_remover.execute(operation, parameters)
        if not self._config_store.exists:
            self._config_store.import_text("schema_version = 1\n")
        result = self._application.dispatcher.execute(
            OperationRequest(operation, parameters)
        )
        resource = result.value.get("resource", result.value)
        return dict(resource) if isinstance(resource, Mapping) else {"value": resource}


class _GatewayMutationGuard:
    """Reject desired endpoint edits that cannot rebind the running Gateway."""

    def __init__(
        self,
        dispatcher,
        launchd: LaunchdAdapter,
        control_socket: Path | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._launchd = launchd
        self._control_socket = control_socket

    def preview(self, request: OperationRequest):
        self._check(request)
        return self._dispatcher.preview(request)

    def execute(self, request: OperationRequest):
        self._check(request)
        return self._dispatcher.execute(request)

    def _check(self, request: OperationRequest) -> None:
        socket_running = False
        if self._control_socket is not None:
            try:
                socket_running = stat.S_ISSOCK(self._control_socket.lstat().st_mode)
            except FileNotFoundError:
                pass
        if request.name == "gateway.configure" and (
            self._launchd.status().running or socket_running
        ):
            raise ApplicationError(
                "supervisor_running",
                "Stop the Supervisor before changing the Gateway endpoint.",
                next_actions=(
                    "mlxctl supervisor stop",
                    "retry the Gateway configuration",
                    "mlxctl supervisor start",
                ),
            )


class _SetupSupervisorOwner:
    def __init__(
        self,
        remote: OperationOwner,
        launchd: LaunchdAdapter,
        activator: LaunchdSupervisorActivator,
    ) -> None:
        self._remote = remote
        self._launchd = launchd
        self._activator = activator

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        if operation == "supervisor.unregister":
            status = self._launchd.status()
            if status.registered:
                status = self._launchd.bootout()
            return plain(status)
        if operation in {"service.drain", "service.stop"}:
            if not self._launchd.status().running:
                return {"state": "stopped", "already_stopped": True}
        elif operation in {"supervisor.start", "service.start"}:
            self._activator.activate()
        return self._remote.execute(operation, parameters)


@dataclass(frozen=True, slots=True)
class ProductionApplication:
    application: ApplicationComposition
    launchd: LaunchdAdapter


def compose_local(
    *,
    paths: MlxctlPaths | None = None,
    home: Path | None = None,
    executable: Path | None = None,
) -> ProductionApplication:
    """Build the CLI/TUI graph without inspecting or activating launchd."""

    resolved_home = (home or Path.home()).expanduser().resolve()
    paths = paths or resolve_paths(home=resolved_home)
    paths.prepare()
    credential = GatewayCredential(paths.gateway_credential)
    executable = (executable or Path(sys.executable)).expanduser().absolute()
    launchd = make_launchd(
        executable=executable,
        home=resolved_home,
        supervisor_log=paths.log_dir / "supervisor.log",
    )
    activator = LaunchdSupervisorActivator(
        launchd, paths.control_socket, timeout_seconds=30.0
    )
    remote = RemoteOperationPort(
        UnixControlClient(paths.control_socket, timeout_seconds=6 * 60 * 60)
    )
    activating_remote = _ActivatingOperationOwner(activator, remote)
    hub_supply = ModelSupply(HuggingFaceHubClient())
    config_store = ConfigStore(paths.config_file, validate_config)
    state_store = OperationalStateStore(paths.state_db)
    intelligence = ModelIntelligence(
        HuggingFaceModelRepository(), PsutilMachineInventory()
    )
    security = ExactRevisionModelSecurity(intelligence, state_store)
    model = _LocalModelSupply(
        hub_supply,
        remote,
        security,
        adoption_forbidden_roots=(
            paths.config_dir,
            paths.state_dir,
            paths.data_dir,
            paths.log_dir,
        ),
    )
    client = client_port(resolved_home, paths, config_store, credential=credential)
    config_owner = _DeferredOperationOwner()
    setup_supervisor = _SetupSupervisorOwner(remote, launchd, activator)
    setup = SetupOperationPort(
        _setup_planner(),
        preflight=SystemSetupPreflight(paths),
        runtime=activating_remote,
        model=activating_remote,
        config=config_owner,
        clients=client,
        supervisor=setup_supervisor,
        verifier=GatewayVerificationPort(credential),
        evidence=OperationalSetupEvidenceStore(state_store),
        removal_inventory=lambda: removal_inventory(
            paths, launchd, config_store, hub_supply, resolved_home
        ),
    )
    application = compose_application(
        paths=paths,
        activator=activator,
        runtime_supply=remote,
        model_supply=model,
        supervisor=_LocalSupervisorOwner(remote, launchd),
        setup=setup,
        clients=client,
        config_store=config_store,
        state_store=state_store,
        model_intelligence=intelligence,
    )
    config_owner.bind(
        _DispatcherOwner(
            application,
            config_store,
            OwnedStateRemover(
                (paths.config_dir, paths.state_dir, paths.data_dir, paths.log_dir)
            ),
        )
    )
    guarded = _GatewayMutationGuard(
        application.dispatcher, launchd, paths.control_socket
    )
    public_application = ApplicationComposition(
        dispatcher=guarded,  # type: ignore[arg-type]
        catalogue=application.catalogue,
        config_store=application.config_store,
        state_store=application.state_store,
        snapshots=application.snapshots,
        paths=application.paths,
    )
    return ProductionApplication(public_application, launchd)


def compose_daemon(
    *, paths: MlxctlPaths | None = None, home: Path | None = None
) -> DaemonService:
    """Build real Runtime, Model, Gateway, and Supervisor owners for mlxd."""

    resolved_home = (home or Path.home()).expanduser().resolve()
    paths = paths or resolve_paths(home=resolved_home)
    paths.prepare()
    credential = GatewayCredential(paths.gateway_credential)
    credential.load_or_create()
    config_store = ConfigStore(paths.config_file, validate_config)
    state_store = OperationalStateStore(paths.state_db)
    catalogue = RuntimeCatalogue.load_builtin()
    runtime_manager = RuntimeManager(
        catalogue,
        runner=AbsoluteUvRunner(resolve_uv(resolved_home)),
        probe=SubprocessRuntimeProbe(),
    )
    runtime = RuntimeSupplyPort(
        runtime_manager,
        config_store,
        paths.runtime_dir,
        catalogue=catalogue,
    )
    hub_supply = ModelSupply(HuggingFaceHubClient())
    security = ExactRevisionModelSecurity(
        ModelIntelligence(HuggingFaceModelRepository(), PsutilMachineInventory()),
        state_store,
    )
    model = ModelSupplyPort(
        hub_supply,
        config_store,
        security,
        adoption_forbidden_roots=(
            paths.config_dir,
            paths.state_dir,
            paths.data_dir,
            paths.log_dir,
        ),
    )

    def load_config() -> MlxctlConfig:
        if not config_store.exists:
            return validate_config({"schema_version": 1})
        return config_store.load().value

    def runtime_installations() -> Mapping[str, RuntimeInstallation]:
        return {
            key: RuntimeInstallation(
                installation_id=item.installation_id,
                runtime=item.definition,
                version=item.version,
                provenance=item.provenance,
                root=Path(item.root),
                launcher=item.launcher,
                capabilities=item.capabilities,
                bundle_id=item.bundle_id,
            )
            for key, item in load_config().runtimes.items()
        }

    def model_installations() -> Mapping[str, ModelInstallation]:
        return configured_model_installations(load_config(), hub_supply.inventory())

    configured = load_config()
    gateway = GatewayRuntime(
        host=configured.gateway.host,
        port=configured.gateway.port,
        metric_sink=state_store.record_metric,
        authenticate=credential.authenticate,
    )
    pressure = MacOSMemoryPressure()
    supervisor = Supervisor(
        desired_state=ConfigDesiredState(load_config),
        runtime_supply=ExactRuntimeLaunchSupply(
            load_config=load_config,
            runtime_installations=runtime_installations,
            model_installations=model_installations,
            launch_builder=RuntimeLaunchBuilder(catalogue),
            trust_grants=lambda: state_store.snapshots("trust"),
            model_security=security,
            model_verifier=model.verify_installation,
        ),
        state_store=state_store,
        gateway=gateway,
        processes=MacOSProcessLauncher(log_dir=paths.log_dir),
        probe=MacOSProcessProbe(),
        memory_pressure=pressure,
        clock=SystemClock(),
    )
    supervisor_port = SupervisorOperationPort(supervisor)

    def router_factory(request_stop: Callable[[], None]) -> DaemonOperationRouter:
        return DaemonOperationRouter(
            runtime=runtime,
            model=model,
            supervisor=supervisor_port,
            state=state_store,
            request_stop=request_stop,
            gateway_host=gateway.host,
            gateway_port=gateway.port,
            pressure=pressure.current,
        )

    return DaemonService(paths.control_socket, router_factory)


def make_launchd(
    *, executable: Path, home: Path, supervisor_log: Path | None = None
) -> LaunchdAdapter:
    """Describe mlxd as a registered-but-inactive per-user LaunchAgent."""

    return ProductionLaunchdAdapter(
        label=LAUNCHD_LABEL,
        program_arguments=(str(executable), "-m", "mlxctl.entrypoints", "daemon"),
        plist_path=home / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist",
        supervisor_log=supervisor_log or home / "Library/Logs/mlxctl/supervisor.log",
    )


def _setup_planner() -> SetupPlanner:
    catalogue = RuntimeCatalogue.load_builtin()
    bundle = next(item for item in catalogue.tested_bundles if item.runtime == "optiq")
    selection = ExactSetupSelection(
        runtime_name="optiq",
        runtime_version=bundle.version,
        runtime_lock_digest=f"sha256:{bundle.lock_sha256}",
        model_repository=_DEFAULT_MODEL,
        model_revision=_DEFAULT_MODEL_REVISION,
        trust_grants=(),
        service_name="qwen36-optiq",
        model_alias="qwen36-optiq",
        service_route="qwen36-optiq",
        activation="manual",
        service_options={
            "kv_config": "kv_config.json",
            "max_context": 32768,
            "mtp": True,
            "temp": 0.0,
        },
        gateway_endpoint="http://127.0.0.1:8766/v1",
        clients=(),
        context_window=32768,
    )
    return SetupPlanner(
        (
            RecommendedProfile(
                "qwen36-optiq",
                48 * 1024**3,
                selection,
                minimum_disk_bytes=24 * 1024**3,
            ),
        )
    )
