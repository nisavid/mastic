"""Production composition for the supported-v1 local inference manager."""

from __future__ import annotations

import os
import stat
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mastic.application.application_targets import SamplingProfile
from mastic.application.catalogue import Operation, OperationKind
from mastic.application.config_schema import (
    MasticConfig,
    validate_config,
)
from mastic.application.dispatch import ApplicationError, OperationRequest
from mastic.application.setup import (
    CapacityProfile,
    ExactSetupSelection,
    RecommendedProfile,
    SetupIntent,
    SetupResolver,
)
from mastic.infrastructure.composition import (
    ApplicationComposition,
    compose_application,
)
from mastic.infrastructure.application_supply import ApplicationSupply
from mastic.infrastructure.config_store import ConfigStore, private_file_lock
from mastic.infrastructure.control_client import UnixControlClient
from mastic.infrastructure.daemon_service import DaemonOperationRouter, DaemonService
from mastic.infrastructure.gateway_runtime import GatewayRuntime
from mastic.infrastructure.gateway import GatewayRequestProfile
from mastic.infrastructure.gateway_credential import GatewayCredential
from mastic.infrastructure.host_integration import (
    LaunchdSupervisorActivator,
    private_socket_ready,
)
from mastic.infrastructure.launchd import LaunchdAdapter
from mastic.infrastructure.model_intelligence import (
    HuggingFaceModelRepository,
    ModelIntelligence,
    PsutilMachineInventory,
    optiq_kv_bytes,
)
from mastic.infrastructure.model_profiles import ModelProfileCatalogue
from mastic.infrastructure.model_supply import (
    HuggingFaceHubClient,
    ModelInstallation,
    ModelSupply,
)
from mastic.infrastructure.operation_ports import (
    RemoteOperationPort,
    SupervisorOperationPort,
)
from mastic.infrastructure.paths_v1 import MasticPaths, resolve_paths
from mastic.infrastructure.production_host import (
    GatewayVerificationPort,
    LazyUvRunner,
    OwnedStateRemover,
    ProductionLaunchdAdapter,
    SystemSetupPreflight,
    application_target_port,
    configured_model_installations,
    default_sampling,
    plain,
    removal_inventory,
    resolve_uv,
    sampling_profile,
)
from mastic.infrastructure.runtime_supply import (
    RuntimeCatalogue,
    RuntimeInstallation,
    RuntimeLaunchBuilder,
    RuntimeManager,
    SubprocessRuntimeProbe,
)
from mastic.infrastructure.setup_port import (
    DurableSetupOutcomeProvider,
    OperationalSetupEvidenceStore,
    OperationalSetupPlanStore,
    SetupOperationPort,
)
from mastic.infrastructure.state_store import OperationalStateStore
from mastic.infrastructure.supply_ports import (
    ExactRevisionModelSecurity,
    ModelSupplyPort,
    RuntimeSupplyPort,
    inspect_adopted_snapshot,
    verify_adopted_snapshot,
)
from mastic.infrastructure.supervisor_v1 import Supervisor
from mastic.infrastructure.system_adapters import (
    ConfigDesiredState,
    ExactRuntimeLaunchSupply,
    MacOSMemoryPressure,
    MacOSProcessLauncher,
    MacOSProcessProbe,
    SystemClock,
)


class _ReentrantPrivateFileLock:
    """Share one cross-process transition lock across nested operation owners."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._threads = threading.RLock()
        self._local = threading.local()

    @contextmanager
    def __call__(self) -> Iterator[None]:
        with self._threads:
            depth = getattr(self._local, "depth", 0)
            if depth:
                self._local.depth = depth + 1
                try:
                    yield
                finally:
                    self._local.depth -= 1
                return
            with private_file_lock(self.path):
                self._local.depth = 1
                try:
                    yield
                finally:
                    self._local.depth = 0


class _OrderedTransition:
    """Acquire lifecycle before removal/composition coordination."""

    def __init__(
        self,
        first: Callable[[], AbstractContextManager[None]],
        second: Callable[[], AbstractContextManager[None]],
    ) -> None:
        self._first = first
        self._second = second

    @contextmanager
    def __call__(self) -> Iterator[None]:
        with self._first():
            with self._second():
                yield


def _external_transition(
    paths: MasticPaths, filename: str
) -> _ReentrantPrivateFileLock:
    """Place a coordination lock outside every product-owned removable path."""

    owned = tuple(
        path.expanduser().absolute()
        for path in (paths.config_dir, paths.state_dir, paths.data_dir, paths.log_dir)
    )
    parent = paths.state_dir.expanduser().absolute().parent
    while True:
        candidate = parent / ".mastic-locks" / filename
        if not any(
            candidate == path or candidate.is_relative_to(path) for path in owned
        ):
            return _ReentrantPrivateFileLock(candidate)
        if parent == parent.parent:
            raise ValueError("transition lock must be outside removable paths")
        parent = parent.parent


def _setup_transition(paths: MasticPaths) -> _ReentrantPrivateFileLock:
    """Serialize setup, removal, and public mutation lifecycles."""

    return _external_transition(paths, "setup-removal.lock")


def _composition_transition(paths: MasticPaths) -> _ReentrantPrivateFileLock:
    """Keep product path creation outside an active removal."""

    return _external_transition(paths, "composition-removal.lock")


def _removal_transition(
    paths: MasticPaths,
    *,
    setup_transition: Callable[[], AbstractContextManager[None]] | None = None,
    composition_transition: Callable[[], AbstractContextManager[None]] | None = None,
) -> _OrderedTransition:
    """Exclude setup/mutations first, then path composition, during removal."""

    return _OrderedTransition(
        setup_transition or _setup_transition(paths),
        composition_transition or _composition_transition(paths),
    )


LAUNCHD_LABEL = "io.nisavid.masticd"
_DEFAULT_MODEL = "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit"
_DEFAULT_MODEL_REVISION = "70a3aa32c7feef511182bf16aa332f37e8d82014"


class OperationOwner(Protocol):
    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]: ...


class _LocalModelSupply:
    """Keep search/inspection local while sending physical mutations to masticd."""

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
    """Activate masticd exactly when a setup step crosses a remote mutation boundary."""

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

    def __init__(
        self,
        remote: OperationOwner,
        launchd: LaunchdAdapter,
        control_socket: Path,
        state_store: OperationalStateStore,
        config_store: ConfigStore[MasticConfig],
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._remote = remote
        self._launchd = launchd
        self._control_socket = control_socket
        self._state_store = state_store
        self._config_store = config_store
        self._clock = clock or SystemClock().time_ns

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        if operation == "supervisor.stop":
            if self._launchd.status().running or private_socket_ready(
                self._control_socket
            ):
                return self._remote.execute(operation, parameters)
            self._reconcile_stopped()
            return {"state": "stopped", "already_stopped": True}
        return self._remote.execute(operation, parameters)

    def _reconcile_stopped(self) -> None:
        config = (
            self._config_store.load().value
            if self._config_store.exists
            else validate_config({"schema_version": 1})
        )
        self._state_store.put_snapshot(
            {
                "kind": "supervisor",
                "id": "supervisor",
                "version": self._clock(),
                "state": "stopped",
                "pressure": "unknown",
            }
        )
        self._state_store.put_snapshot(
            {
                "kind": "gateway",
                "id": "gateway",
                "version": self._clock(),
                "state": "stopped",
                "host": config.gateway.host,
                "port": config.gateway.port,
            }
        )
        latest_runs: dict[str, Mapping[str, object]] = {}
        for run in self._state_store.snapshots("service_run"):
            service = str(run.get("service", run.get("service_name", "")))
            if service:
                latest_runs[service] = run
        for service, run in latest_runs.items():
            self._state_store.put_snapshot(
                {
                    "kind": "service_run",
                    "id": str(run["id"]),
                    "version": self._clock(),
                    "service": service,
                    "run_id": str(run.get("run_id", "unknown")),
                    "state": "stopped",
                }
            )
        for operation in self._state_store.operations():
            if operation.get("status") not in {"queued", "running", "resuming"}:
                continue
            interrupted = {
                **operation,
                "status": "failed",
                "outcome": "interrupted",
                "error": "Supervisor stopped before the operation completed.",
            }
            self._state_store.put_operation(interrupted)
            self._state_store.append_event(
                {
                    "kind": "interrupted",
                    "operation_id": str(operation["id"]),
                    "resource": str(operation.get("resource", "unknown")),
                    "reason": "supervisor_inactive",
                }
            )


class _DispatcherOwner:
    def __init__(
        self,
        application: ApplicationComposition,
        config_store: ConfigStore[MasticConfig],
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
        *,
        catalogue: Mapping[str, Operation] | None = None,
        transition: Callable[[], AbstractContextManager[None]] | None = None,
        paths: MasticPaths | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._launchd = launchd
        self._control_socket = control_socket
        self._catalogue = {} if catalogue is None else catalogue
        self._transition = transition if transition is not None else nullcontext
        self._paths = paths

    def preview(self, request: OperationRequest):
        self._check(request)
        return self._dispatcher.preview(request)

    def execute(self, request: OperationRequest):
        operation = self._catalogue.get(request.name)
        if operation is not None and operation.kind is OperationKind.MUTATION:
            with self._transition():
                self._require_product_roots()
                self._check(request)
                return self._dispatcher.execute(request)
        self._check(request)
        return self._dispatcher.execute(request)

    def _check(self, request: OperationRequest) -> None:
        socket_running = self._control_socket is not None and private_socket_ready(
            self._control_socket
        )
        if request.name == "gateway.configure" and (
            self._launchd.status().running or socket_running
        ):
            raise ApplicationError(
                "supervisor_running",
                "Stop the Supervisor before changing the Gateway endpoint.",
                next_actions=(
                    "mastic supervisor stop",
                    "retry the Gateway configuration",
                    "mastic supervisor start",
                ),
            )

    def _require_product_roots(self) -> None:
        if self._paths is None:
            return
        required = (
            self._paths.config_dir,
            self._paths.state_dir,
            self._paths.data_dir,
            self._paths.log_dir,
        )
        if any(not _private_product_root_ready(path) for path in required):
            raise ApplicationError(
                "product_state_removed",
                "MASTIC product state was removed after this command initialized.",
                next_actions=("retry the command to reinitialize local state",),
            )


def _private_product_root_ready(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700
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
        elif operation == "supervisor.start":
            # Setup can be the first command run after replacing the installed
            # mastic package.  A running launchd process still has the old
            # Python code loaded, so recycle it before asking the freshly
            # started daemon to reconcile its configured services.
            if self._launchd.status().running:
                self._launchd.bootout()
            self._activator.activate()
        elif operation == "service.start":
            self._activator.activate()
        return self._remote.execute(operation, parameters)


@dataclass(frozen=True, slots=True)
class ProductionApplication:
    application: ApplicationComposition
    launchd: LaunchdAdapter


def compose_local(
    *,
    paths: MasticPaths | None = None,
    home: Path | None = None,
    executable: Path | None = None,
) -> ProductionApplication:
    """Build the CLI/TUI graph without inspecting or activating launchd."""

    resolved_home = (home or Path.home()).expanduser().resolve()
    resolved_paths = paths or resolve_paths(home=resolved_home)
    setup_transition = _setup_transition(resolved_paths)
    composition_transition = _composition_transition(resolved_paths)
    with composition_transition():
        return _compose_local_locked(
            paths=resolved_paths,
            resolved_home=resolved_home,
            executable=executable,
            setup_transition=setup_transition,
            removal_transition=_removal_transition(
                resolved_paths,
                setup_transition=setup_transition,
                composition_transition=composition_transition,
            ),
        )


def _compose_local_locked(
    *,
    paths: MasticPaths,
    resolved_home: Path,
    executable: Path | None,
    setup_transition: _ReentrantPrivateFileLock,
    removal_transition: _OrderedTransition,
) -> ProductionApplication:
    """Build the local graph while state removal cannot cross composition."""

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
    application_targets = application_target_port(
        resolved_home,
        paths,
        config_store,
        credential=credential,
        transition=setup_transition,
    )
    config_owner = _DeferredOperationOwner()
    setup_supervisor = _SetupSupervisorOwner(remote, launchd, activator)
    applications = ApplicationSupply(
        resolved_home,
        paths.data_dir / "bootstrap-artifacts" / "application-targets-v1",
        paths.state_dir,
        uv_executable=paths.data_dir / "bootstrap-uv" / "uv",
        python_executable=(paths.data_dir / "bootstrap-python" / "bin" / "python3.11"),
        application_tool_dir=paths.data_dir / "application-tools",
        application_bin_dir=paths.data_dir / "application-bin",
        transition=setup_transition,
    )
    setup_evidence = OperationalSetupEvidenceStore(state_store)
    setup_plans = OperationalSetupPlanStore(state_store)
    setup_outcomes = DurableSetupOutcomeProvider(
        setup_plans,
        setup_evidence,
        application_targets=application_targets,
    )
    setup = SetupOperationPort(
        _setup_resolver(),
        preflight=SystemSetupPreflight(paths),
        runtime=activating_remote,
        model=activating_remote,
        config=config_owner,
        applications=applications,
        application_targets=application_targets,
        supervisor=setup_supervisor,
        verifier=GatewayVerificationPort(credential),
        evidence=setup_evidence,
        removal_inventory=lambda: removal_inventory(
            paths,
            launchd,
            config_store,
            hub_supply,
            resolved_home,
            application_inventory=applications.inventory(),
        ),
        transition=setup_transition,
        removal_transition=removal_transition,
        plan_store=setup_plans,
    )
    application = compose_application(
        paths=paths,
        activator=activator,
        runtime_supply=remote,
        model_supply=model,
        supervisor=_LocalSupervisorOwner(
            remote, launchd, paths.control_socket, state_store, config_store
        ),
        setup=setup,
        application_targets=application_targets,
        config_store=config_store,
        state_store=state_store,
        model_intelligence=intelligence,
        setup_outcomes=setup_outcomes,
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
        application.dispatcher,
        launchd,
        paths.control_socket,
        catalogue=application.catalogue,
        transition=setup_transition,
        paths=paths,
    )
    public_application = ApplicationComposition(
        dispatcher=guarded,
        catalogue=application.catalogue,
        config_store=application.config_store,
        state_store=application.state_store,
        snapshots=application.snapshots,
        paths=application.paths,
    )
    return ProductionApplication(public_application, launchd)


def _sampling_matches_service_model(
    config: MasticConfig,
    service,
    sampling: SamplingProfile,
    catalogue: ModelProfileCatalogue | None = None,
) -> bool:
    """Fail closed unless stored provenance matches the service's exact model."""

    alias = config.aliases.get(service.model_alias)
    if alias is None:
        return False
    installation = config.models.get(alias.installation_name)
    if installation is None:
        return False
    if sampling.upstream_profile is None:
        return True
    profiles = catalogue or ModelProfileCatalogue.load_builtin()
    try:
        expected = profiles.profile(
            installation.revision.repository,
            installation.revision.revision,
            sampling.upstream_profile,
        )
    except KeyError:
        return False
    actual = dict(sampling_profile(sampling).values())
    actual.pop("preserve_thinking", None)
    return (
        actual == dict(expected.parameters)
        and sampling.source_url == expected.source_url
        and sampling.source_revision == expected.source_revision
    )


def compose_daemon(
    *, paths: MasticPaths | None = None, home: Path | None = None
) -> DaemonService:
    """Build real Runtime, Model, Gateway, and Supervisor owners for masticd."""

    resolved_home = (home or Path.home()).expanduser().resolve()
    resolved_paths = paths or resolve_paths(home=resolved_home)
    with _composition_transition(resolved_paths)():
        return _compose_daemon_locked(
            paths=resolved_paths,
            resolved_home=resolved_home,
        )


def _compose_daemon_locked(*, paths: MasticPaths, resolved_home: Path) -> DaemonService:
    """Build the daemon graph while state removal cannot cross composition."""

    paths.prepare()
    credential = GatewayCredential(paths.gateway_credential)
    credential.load_or_create()
    config_store = ConfigStore(paths.config_file, validate_config)
    state_store = OperationalStateStore(paths.state_db)
    catalogue = RuntimeCatalogue.load_builtin()
    runtime_manager = RuntimeManager(
        catalogue,
        runner=LazyUvRunner(lambda: resolve_uv(resolved_home)),
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

    def load_config() -> MasticConfig:
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
    model_profiles = ModelProfileCatalogue.load_builtin()

    def request_profile(application_target_name: str, profile_name: str):
        config = load_config()
        application_target = config.application_targets.get(application_target_name)
        if application_target is None:
            return None
        sampling = application_target.sampling.get(profile_name)
        if sampling is None:
            return None
        service = config.services.get(application_target.service)
        if service is None:
            service = next(
                (
                    item
                    for item in config.services.values()
                    if str(item.route) == application_target.service
                ),
                None,
            )
        if service is None:
            return None
        if not _sampling_matches_service_model(
            config, service, sampling, model_profiles
        ):
            return None
        parameters = dict(sampling_profile(sampling).values())
        return GatewayRequestProfile(str(service.route), parameters)

    gateway = GatewayRuntime(
        host=configured.gateway.host,
        port=configured.gateway.port,
        metric_sink=state_store.record_metric,
        authenticate=credential.authenticate,
        profile_resolver=request_profile,
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
    """Describe masticd as a registered-but-inactive per-user LaunchAgent."""

    return ProductionLaunchdAdapter(
        label=LAUNCHD_LABEL,
        program_arguments=(str(executable), "-m", "mastic.entrypoints", "daemon"),
        plist_path=home / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist",
        supervisor_log=supervisor_log or home / "Library/Logs/mastic/supervisor.log",
    )


def _setup_resolver() -> SetupResolver:
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
            "mtp": True,
        },
        gateway_endpoint="http://127.0.0.1:8766/v1",
        application_targets=("codex", "hindsight"),
        application_target_options={
            "codex": {
                "sampling_profiles": {
                    name: dict(sampling_profile(settings).definition())
                    for name, settings in default_sampling(
                        _DEFAULT_MODEL, _DEFAULT_MODEL_REVISION, "codex"
                    ).items()
                },
            },
            "hindsight": {
                "profile": "default",
                "max_concurrent": 1,
                "sampling_profiles": {
                    name: dict(sampling_profile(settings).definition())
                    for name, settings in default_sampling(
                        _DEFAULT_MODEL, _DEFAULT_MODEL_REVISION, "hindsight"
                    ).items()
                },
            },
        },
    )
    pinned_config = {"head_dim": 256, "num_key_value_heads": 2}
    pinned_kv_config = [
        {"layer_idx": index, "bits": bits, "group_size": 64}
        for index, bits in zip(
            (3, 7, 11, 15, 19, 23, 27, 31, 35, 39),
            (4, 8, 4, 8, 8, 4, 4, 4, 4, 4),
            strict=True,
        )
    ]
    projected_kv_bytes = optiq_kv_bytes(
        pinned_config,
        pinned_kv_config,
        context_tokens=131_072,
        concurrency=6,
    )
    if projected_kv_bytes is None:
        raise RuntimeError("pinned OptiQ KV geometry is invalid")
    prompt_cache_bytes = 2 * 1024**3
    capacities = (
        CapacityProfile(
            "balanced",
            "Balanced",
            131_072,
            6,
            projected_kv_bytes,
            prompt_cache_bytes,
            "Best default for several active tools while retaining a 128K context window.",
        ),
        CapacityProfile(
            "long-context",
            "Long context",
            196_608,
            4,
            projected_kv_bytes,
            prompt_cache_bytes,
            "More context per request with four simultaneous inference requests.",
        ),
        CapacityProfile(
            "native-context",
            "Native context",
            262_144,
            3,
            projected_kv_bytes,
            prompt_cache_bytes,
            "The model's native context with three simultaneous inference requests.",
        ),
    )
    return SetupResolver(
        (
            RecommendedProfile(
                "qwen36-optiq",
                48 * 1024**3,
                selection,
                minimum_disk_bytes=24 * 1024**3,
            ),
        ),
        capacity_profiles=capacities,
        default_capacity_profile="balanced",
        # Phase 1 has validation evidence for one interactive profile and one
        # longer-context profile. Responsive therefore shares balanced until a
        # separately validated low-latency capacity profile exists.
        intent_capacity_profiles={
            SetupIntent.BALANCED: "balanced",
            SetupIntent.DEEP: "long-context",
            SetupIntent.RESPONSIVE: "balanced",
        },
    )
