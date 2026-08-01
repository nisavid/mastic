"""Composition root for the shared supported-v1 application catalogue."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Protocol

from mastic.application.catalogue import Operation, build_operation_catalogue
from mastic.application.config_schema import MasticConfig, validate_config
from mastic.application.dispatch import (
    OperationDispatch,
    OperationDispatcher,
    SupervisorActivator,
)
from mastic.application.status import SnapshotProvider
from mastic.infrastructure.config_store import ConfigStore
from mastic.infrastructure.host_integration import (
    LocalSnapshotProvider,
    PrivateLogReader,
    StateMetricsSource,
)
from mastic.infrastructure.local_backend import (
    ApplicationDiagnosticPort,
    LocalConfigurationMutations,
    LocalOperationBackend,
)
from mastic.infrastructure.paths_v1 import MasticPaths
from mastic.infrastructure.runtime_supply import RuntimeCatalogue
from mastic.infrastructure.state_store import OperationalStateStore


class OperationPort(Protocol):
    def execute(self, operation, parameters): ...


class ApplicationPort(OperationPort, ApplicationDiagnosticPort, Protocol):
    """Execute application operations and diagnose the live owner."""


@dataclass(frozen=True, slots=True)
class ApplicationComposition:
    """The one dispatcher and local state shared by CLI and TUI surfaces."""

    dispatcher: OperationDispatch
    catalogue: Mapping[str, Operation]
    config_store: ConfigStore[MasticConfig]
    state_store: OperationalStateStore
    snapshots: SnapshotProvider
    paths: MasticPaths


def compose_application(
    *,
    paths: MasticPaths,
    activator: SupervisorActivator,
    runtime_supply: OperationPort,
    model_supply,
    supervisor: OperationPort,
    setup: OperationPort,
    applications: ApplicationPort,
    application_targets: OperationPort,
    configuration_mutations: LocalConfigurationMutations | None = None,
    config_store: ConfigStore[MasticConfig] | None = None,
    state_store: OperationalStateStore | None = None,
    runtime_catalogue: RuntimeCatalogue | None = None,
    logs=None,
    metrics=None,
    model_intelligence=None,
    setup_outcomes=None,
) -> ApplicationComposition:
    """Bind concrete owners without activating any managed process."""

    paths.prepare()
    config = (
        ConfigStore(paths.config_file, validate_config)
        if config_store is None
        else config_store
    )
    state = (
        OperationalStateStore(paths.state_db) if state_store is None else state_store
    )
    if configuration_mutations is not None:
        configuration_mutations.validate_stores(config, state)
    runtimes = (
        RuntimeCatalogue.load_builtin()
        if runtime_catalogue is None
        else runtime_catalogue
    )
    catalogue = dict(build_operation_catalogue())
    backend = LocalOperationBackend(
        catalogue=catalogue,
        config_store=config,
        state_store=state,
        runtime_catalogue=runtimes,
        runtime_supply=runtime_supply,
        model_supply=model_supply,
        supervisor=supervisor,
        logs=PrivateLogReader(paths.log_dir) if logs is None else logs,
        metrics=StateMetricsSource(state) if metrics is None else metrics,
        setup=setup,
        applications=applications,
        application_diagnostics=applications,
        application_targets=application_targets,
        configuration_mutations=configuration_mutations,
        config_path=paths.config_file,
        gateway_credential_path=paths.gateway_credential,
        model_intelligence=model_intelligence,
        setup_outcomes=setup_outcomes,
    )
    dispatcher = OperationDispatcher(catalogue, activator, backend)
    return ApplicationComposition(
        dispatcher=dispatcher,
        catalogue=MappingProxyType(catalogue),
        config_store=config,
        state_store=state,
        snapshots=LocalSnapshotProvider(dispatcher),
        paths=paths,
    )
