"""Composition root for the shared supported-v1 application catalogue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mastic.application.catalogue import Operation, build_operation_catalogue
from mastic.application.config_schema import MasticConfig, validate_config
from mastic.application.dispatch import OperationDispatcher, SupervisorActivator
from mastic.application.manager import ApplicationManager
from mastic.infrastructure.config_store import ConfigStore
from mastic.infrastructure.host_integration import (
    LocalSnapshotProvider,
    PrivateLogReader,
    StateMetricsSource,
)
from mastic.infrastructure.local_backend import LocalOperationBackend
from mastic.infrastructure.paths_v1 import MasticPaths
from mastic.infrastructure.runtime_supply import RuntimeCatalogue
from mastic.infrastructure.state_store import OperationalStateStore


class OperationPort(Protocol):
    def execute(self, operation, parameters): ...


@dataclass(frozen=True, slots=True)
class ApplicationComposition:
    """The one dispatcher and local state shared by CLI and TUI surfaces."""

    dispatcher: OperationDispatcher
    catalogue: dict[str, Operation]
    config_store: ConfigStore[MasticConfig]
    state_store: OperationalStateStore
    snapshots: LocalSnapshotProvider
    paths: MasticPaths


def compose_application(
    *,
    paths: MasticPaths,
    activator: SupervisorActivator,
    runtime_supply: OperationPort,
    model_supply,
    supervisor: OperationPort,
    setup: OperationPort,
    application_targets: OperationPort,
    config_store: ConfigStore[MasticConfig] | None = None,
    state_store: OperationalStateStore | None = None,
    runtime_catalogue: RuntimeCatalogue | None = None,
    logs=None,
    metrics=None,
    model_intelligence=None,
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
        application_targets=application_targets,
        config_path=paths.config_file,
        gateway_credential_path=paths.gateway_credential,
        model_intelligence=model_intelligence,
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
