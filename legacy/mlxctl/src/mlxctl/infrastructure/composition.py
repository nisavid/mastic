"""Composition root for the shared supported-v1 application catalogue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mlxctl.application.catalogue import Operation, build_operation_catalogue
from mlxctl.application.config_schema import MlxctlConfig, validate_config
from mlxctl.application.dispatch import OperationDispatcher, SupervisorActivator
from mlxctl.application.manager import ApplicationManager
from mlxctl.infrastructure.config_store import ConfigStore
from mlxctl.infrastructure.host_integration import (
    LocalSnapshotProvider,
    PrivateLogReader,
    StateMetricsSource,
)
from mlxctl.infrastructure.local_backend import LocalOperationBackend
from mlxctl.infrastructure.paths_v1 import MlxctlPaths
from mlxctl.infrastructure.runtime_supply import RuntimeCatalogue
from mlxctl.infrastructure.state_store import OperationalStateStore


class OperationPort(Protocol):
    def execute(self, operation, parameters): ...


@dataclass(frozen=True, slots=True)
class ApplicationComposition:
    """The one dispatcher and local state shared by CLI and TUI surfaces."""

    dispatcher: OperationDispatcher
    catalogue: dict[str, Operation]
    config_store: ConfigStore[MlxctlConfig]
    state_store: OperationalStateStore
    snapshots: LocalSnapshotProvider
    paths: MlxctlPaths


def compose_application(
    *,
    paths: MlxctlPaths,
    activator: SupervisorActivator,
    runtime_supply: OperationPort,
    model_supply,
    supervisor: OperationPort,
    setup: OperationPort,
    clients: OperationPort,
    config_store: ConfigStore[MlxctlConfig] | None = None,
    state_store: OperationalStateStore | None = None,
    runtime_catalogue: RuntimeCatalogue | None = None,
    logs=None,
    metrics=None,
) -> ApplicationComposition:
    """Bind concrete owners without activating any managed process."""

    paths.prepare()
    config = config_store or ConfigStore(paths.config_file, validate_config)
    state = state_store or OperationalStateStore(paths.state_db)
    runtimes = runtime_catalogue or RuntimeCatalogue.load_builtin()
    catalogue = dict(build_operation_catalogue())
    dispatcher = OperationDispatcher(catalogue, activator)
    backend = LocalOperationBackend(
        catalogue=catalogue,
        config_store=config,
        state_store=state,
        runtime_catalogue=runtimes,
        runtime_supply=runtime_supply,
        model_supply=model_supply,
        supervisor=supervisor,
        logs=logs or PrivateLogReader(paths.log_dir),
        metrics=metrics or StateMetricsSource(state),
        setup=setup,
        clients=clients,
        config_path=paths.config_file,
    )
    ApplicationManager(catalogue, backend).register(dispatcher)
    return ApplicationComposition(
        dispatcher=dispatcher,
        catalogue=catalogue,
        config_store=config,
        state_store=state,
        snapshots=LocalSnapshotProvider(dispatcher),
        paths=paths,
    )
