"""Local supported-v1 implementation of the shared operation catalogue."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

import tomlkit

from mlxctl.application.catalogue import Operation, OperationKind
from mlxctl.application.config_schema import MlxctlConfig, validate_config
from mlxctl.application.dispatch import ApplicationError, OperationRequest
from mlxctl.application.manager import PreparedOperation
from mlxctl.infrastructure.config_store import ConfigStore
from mlxctl.infrastructure.model_supply import (
    ModelInstallation as SuppliedModelInstallation,
)
from mlxctl.infrastructure.model_intelligence import RuntimeObservation
from mlxctl.infrastructure.model_supply import (
    ModelProvenance,
    ModelRevision,
    ModelSupply,
)
from mlxctl.infrastructure.runtime_supply import RuntimeCatalogue
from mlxctl.infrastructure.state_store import OperationalStateStore


class OperationPort(Protocol):
    """Own one family of supported-v1 mutations."""

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]: ...


class LogReader(Protocol):
    def read(
        self, scope: str, resource: str | None = None
    ) -> Sequence[Mapping[str, object]]: ...


class MetricsSource(Protocol):
    def query(
        self, scope: str, resource: str | None = None
    ) -> Sequence[Mapping[str, object]]: ...


_SUPERVISOR_MUTATIONS = frozenset(
    {
        "supervisor.start",
        "supervisor.stop",
        "supervisor.restart",
        "gateway.restart",
        "service.start",
        "service.stop",
        "service.restart",
        "operation.cancel",
        "operation.resume",
    }
)
_RUNTIME_MUTATIONS = frozenset(
    {
        "runtime.install",
        "runtime.adopt",
        "runtime.update",
        "runtime.rollback",
        "runtime.remove",
        "runtime.prune",
    }
)
_MODEL_LONG_MUTATIONS = frozenset(
    {
        "model.install",
        "model.repair",
        "model.update",
        "model.rollback",
        "model.cache.move",
        "model.cache.evict",
        "model.cache.prune",
    }
)
_LOCAL_MUTATIONS = frozenset(
    {
        "gateway.configure",
        "model.uninstall",
        "model.trust",
        "service.create",
        "service.edit",
        "service.remove",
        "client.configure",
        "client.remove",
        "config.import",
        "config.restore",
    }
)


class LocalOperationBackend:
    """Prepare observations and owner-scoped mutations without hidden activation."""

    def __init__(
        self,
        *,
        catalogue: Mapping[str, Operation],
        config_store: ConfigStore[MlxctlConfig],
        state_store: OperationalStateStore,
        runtime_catalogue: RuntimeCatalogue,
        runtime_supply: OperationPort,
        model_supply: ModelSupply,
        supervisor: OperationPort,
        logs: LogReader,
        metrics: MetricsSource,
        setup: OperationPort,
        clients: OperationPort,
        config_path: str | Path,
        model_intelligence=None,
    ) -> None:
        self._catalogue = catalogue
        self._config_store = config_store
        self._state_store = state_store
        self._runtime_catalogue = runtime_catalogue
        self._runtime_supply = runtime_supply
        self._model_supply = model_supply
        self._supervisor = supervisor
        self._logs = logs
        self._metrics = metrics
        self._setup = setup
        self._clients = clients
        self._config_path = Path(config_path)
        self._model_intelligence = model_intelligence

    def prepare(self, request: OperationRequest) -> PreparedOperation:
        operation = self._catalogue.get(request.name)
        if operation is None:
            raise ApplicationError(
                "unknown_operation", f"unknown operation: {request.name}"
            )
        self._validate_request(request)
        if operation.kind is OperationKind.QUERY:
            return PreparedOperation(False, lambda: self._query(request))
        preview_method = (
            getattr(self._setup, "preview", None) if request.name == "setup" else None
        )
        if callable(preview_method):
            preview = {
                "schema_version": 1,
                "operation": request.name,
                "confirmation_required": operation.confirmation,
                **_plain(preview_method(request.parameters)),
            }
        else:
            preview = {
                "schema_version": 1,
                "operation": request.name,
                "confirmation_required": operation.confirmation,
                "parameters": _plain(request.parameters),
            }
        requires_supervisor = request.name == "setup" or request.name in (
            _SUPERVISOR_MUTATIONS | _RUNTIME_MUTATIONS | _MODEL_LONG_MUTATIONS
        )
        return PreparedOperation(
            requires_supervisor=requires_supervisor,
            execute=lambda: self._mutate(request, preview),
            events=({"phase": "preview", **preview},),
        )

    def _validate_request(self, request: OperationRequest) -> None:
        """Resolve named resources before returning an executable plan."""
        name = request.name
        config = self._config()
        if name == "runtime.inspect":
            _resource(
                request,
                set(config.runtimes)
                | {item.key for item in self._runtime_catalogue.definitions},
                "Runtime",
            )
        elif name in _RUNTIME_MUTATIONS:
            self._validate_runtime_mutation(request, config)
        elif name in {
            "model.verify",
            "model.repair",
            "model.update",
            "model.rollback",
            "model.uninstall",
            "model.trust",
        }:
            _resource(request, set(config.models) | set(config.aliases), "Model")
        elif name in {
            "model.cache.inspect",
            "model.cache.move",
            "model.cache.evict",
        }:
            revisions = self._model_supply.inventory().revisions
            _resource(
                request,
                {item.revision_id for item in revisions}
                | {item.commit_sha for item in revisions},
                "Cached Revision",
            )
        elif name.startswith("service.") and name not in {
            "service.list",
            "service.create",
        }:
            _resource(request, config.services, "Inference Service")
        elif name.startswith("operation.") and name != "operation.list":
            _resource(
                request,
                {str(item["id"]) for item in self._state_store.operations()},
                "Operation",
            )
        elif name in {"client.inspect", "client.test", "client.remove"}:
            _resource(request, config.clients, "Client")

    def _query(self, request: OperationRequest) -> Mapping[str, object]:
        name = request.name
        config = self._config()
        if name in {"status", "check", "doctor", "tui"}:
            return self._overview(name, config)
        if name in {"logs", "metrics"}:
            return self._telemetry(name, "all", None)
        if name.startswith("supervisor."):
            return self._supervisor_query(name)
        if name.startswith("gateway."):
            return self._gateway_query(name, config)
        if name.startswith("runtime."):
            return self._runtime_query(request, config)
        if name.startswith("model.cache."):
            return self._cache_query(request)
        if name.startswith("model."):
            return self._model_query(request, config)
        if name.startswith("service."):
            return self._service_query(request, config)
        if name.startswith("operation."):
            return self._operation_query(request)
        if name.startswith("client."):
            return self._client_query(request, config)
        if name.startswith("config."):
            return self._config_query(request)
        raise ApplicationError("operation_unavailable", f"{name} has no local backend")

    def _overview(self, name: str, config: MlxctlConfig) -> Mapping[str, object]:
        supervisor = self._latest("supervisor", "supervisor") or {"state": "stopped"}
        gateway = self._latest("gateway", "gateway") or {
            "state": "stopped",
            "host": config.gateway.host,
            "port": config.gateway.port,
        }
        services = self._service_items(config)
        failed = [
            item["name"]
            for item in services
            if (item["run"] or {}).get("state") in {"failed", "unhealthy"}
        ]
        state = (
            "failed"
            if failed
            else ("stopped" if supervisor.get("state") == "stopped" else "ok")
        )
        next_actions = []
        if supervisor.get("state") == "stopped":
            next_actions.append("mlxctl supervisor start")
        if failed:
            next_actions.append("mlxctl doctor")
        return _result(
            name,
            state=state,
            supervisor=supervisor,
            gateway=gateway,
            services=services,
            failed_services=failed,
            evidence=["desired-state", "operational-state"],
            next_actions=next_actions,
        )

    def _supervisor_query(self, name: str) -> Mapping[str, object]:
        if name == "supervisor.logs":
            return self._telemetry("logs", "supervisor", None, operation=name)
        snapshot = self._latest("supervisor", "supervisor") or {"state": "stopped"}
        return _result(
            name,
            state=str(snapshot.get("state", "unknown")),
            resource=snapshot,
            evidence=["operational-state"],
            next_actions=["mlxctl supervisor start"]
            if snapshot.get("state") == "stopped"
            else [],
        )

    def _gateway_query(self, name: str, config: MlxctlConfig) -> Mapping[str, object]:
        if name == "gateway.logs":
            return self._telemetry("logs", "gateway", None, operation=name)
        if name == "gateway.metrics":
            return self._telemetry("metrics", "gateway", None, operation=name)
        snapshot = self._latest("gateway", "gateway") or {"state": "stopped"}
        routes = [
            {"route": str(service.route), "service": service_name}
            for service_name, service in sorted(config.services.items())
        ]
        return _result(
            name,
            state=str(snapshot.get("state", "stopped")),
            endpoint={"host": config.gateway.host, "port": config.gateway.port},
            routes=routes,
            resource=snapshot,
            evidence=["desired-state", "operational-state"],
            next_actions=[],
        )

    def _runtime_query(
        self, request: OperationRequest, config: MlxctlConfig
    ) -> Mapping[str, object]:
        name = request.name
        definitions = {item.key: item for item in self._runtime_catalogue.definitions}
        if name == "runtime.available":
            items = [
                _plain(item)
                for item in sorted(definitions.values(), key=lambda item: item.key)
            ]
            return _result(
                name, items=items, evidence=["built-in-catalogue"], next_actions=[]
            )
        if name in {"runtime.list", "runtime.doctor"}:
            items = [_plain(item) for _, item in sorted(config.runtimes.items())]
            return _result(
                name, items=items, evidence=["desired-state"], next_actions=[]
            )
        resource = _resource(
            request, set(config.runtimes) | set(definitions), "Runtime"
        )
        if resource in config.runtimes:
            item = _plain(config.runtimes[resource])
        else:
            item = _plain(definitions[resource])
        return _result(
            name,
            resource=item,
            evidence=["desired-state", "built-in-catalogue"],
            next_actions=[],
        )

    def _model_query(
        self, request: OperationRequest, config: MlxctlConfig
    ) -> Mapping[str, object]:
        name = request.name
        if name == "model.inspect":
            if self._model_intelligence is None:
                raise ApplicationError(
                    "operation_unavailable",
                    "model intelligence is unavailable in this installation",
                )
            selected = str(
                request.parameters.get(
                    "repository", request.parameters.get("resource", "")
                )
            )
            revision = str(request.parameters.get("revision", "main"))
            if selected in config.aliases:
                selected = config.aliases[selected].installation_name
            if selected in config.models:
                installed = config.models[selected]
                repository = installed.revision.repository
                revision = installed.revision.revision
            else:
                repository = selected
            report = self._model_intelligence.inspect(
                repository,
                revision,
                runtimes=tuple(
                    RuntimeObservation(
                        installation_id=item.installation_id,
                        runtime=item.definition,
                        version=item.version,
                        recognized_model_types=frozenset(),
                        capabilities=item.capabilities,
                        source="exact-runtime-probe",
                    )
                    for item in config.runtimes.values()
                ),
                context_tokens=int(request.parameters.get("context_tokens", 32768)),
                concurrency=int(request.parameters.get("concurrency", 1)),
            )
            return _result(
                name,
                resource=_plain(report),
                evidence=["exact-hub-metadata", "local-machine-inventory"],
                next_actions=[],
            )
        if name == "model.search":
            query = str(request.parameters.get("query", ""))
            mode = str(request.parameters.get("source", "curated"))
            limit = int(request.parameters.get("limit", 20))
            return _result(
                name,
                items=[
                    _plain(item)
                    for item in self._model_supply.search(query, mode=mode, limit=limit)
                ],
                evidence=["model-catalogue"],
                next_actions=[],
            )
        if name == "model.list":
            aliases = {
                key: value.installation_name for key, value in config.aliases.items()
            }
            items = [
                {
                    **_plain(item),
                    "aliases": sorted(
                        alias for alias, target in aliases.items() if target == key
                    ),
                }
                for key, item in sorted(config.models.items())
            ]
            return _result(
                name, items=items, evidence=["desired-state"], next_actions=[]
            )
        resource = _resource(request, set(config.models) | set(config.aliases), "Model")
        installation_name = (
            config.aliases[resource].installation_name
            if resource in config.aliases
            else resource
        )
        item = config.models[installation_name]
        if name == "model.verify":
            verification = self._model_supply.verify(
                self._supplied_model_installation(installation_name, config)
            )
            return _result(
                name,
                resource=_plain(verification),
                evidence=[verification.evidence],
                next_actions=[f"mlxctl model repair {installation_name}"]
                if verification.issues
                else [],
            )
        verification = self._latest("model_verification", installation_name)
        return _result(
            name,
            resource={
                **_plain(item),
                "selected_by": resource,
                "verification": verification,
            },
            evidence=["desired-state"]
            + (["verification-state"] if verification else []),
            next_actions=[f"mlxctl model verify {installation_name}"]
            if verification is None
            else [],
        )

    def _cache_query(self, request: OperationRequest) -> Mapping[str, object]:
        inventory = self._model_supply.inventory()
        if request.name == "model.cache.list":
            return _result(
                request.name,
                items=[_plain(item) for item in inventory.revisions],
                warnings=list(inventory.warnings),
                evidence=[inventory.evidence],
                next_actions=[],
            )
        choices = {item.revision_id: item for item in inventory.revisions}
        choices.update({item.commit_sha: item for item in inventory.revisions})
        resource = _resource(request, set(choices), "Cached Revision")
        return _result(
            request.name,
            resource=_plain(choices[resource]),
            evidence=[inventory.evidence],
            next_actions=[],
        )

    def _service_query(
        self, request: OperationRequest, config: MlxctlConfig
    ) -> Mapping[str, object]:
        if request.name == "service.list":
            return _result(
                request.name,
                items=self._service_items(config),
                evidence=["desired-state", "operational-state"],
                next_actions=[],
            )
        resource = _resource(request, config.services, "Inference Service")
        if request.name == "service.logs":
            return self._telemetry("logs", "service", resource, operation=request.name)
        if request.name == "service.metrics":
            return self._telemetry(
                "metrics", "service", resource, operation=request.name
            )
        item = next(
            item for item in self._service_items(config) if item["name"] == resource
        )
        state = str((item["run"] or {}).get("state", "stopped"))
        return _result(
            request.name,
            state=state,
            resource=item,
            evidence=["desired-state", "operational-state"],
            next_actions=[f"mlxctl service start {resource}"]
            if state == "stopped"
            else [],
        )

    def _operation_query(self, request: OperationRequest) -> Mapping[str, object]:
        if request.name == "operation.list":
            return _result(
                request.name,
                items=list(self._state_store.operations()),
                evidence=["operational-state"],
                next_actions=[],
            )
        resource = _resource(
            request,
            {str(item["id"]) for item in self._state_store.operations()},
            "Operation",
        )
        operation = self._state_store.operation(resource)
        events = list(self._state_store.events(resource))
        return _result(
            request.name,
            resource=operation,
            events=events,
            evidence=["operational-state"],
            next_actions=[],
        )

    def _client_query(
        self, request: OperationRequest, config: MlxctlConfig
    ) -> Mapping[str, object]:
        if request.name == "client.list":
            return _result(
                request.name,
                items=[_plain(item) for _, item in sorted(config.clients.items())],
                evidence=["desired-state"],
                next_actions=[],
            )
        resource = _resource(request, config.clients, "Client")
        if request.name == "client.test":
            value = self._clients.execute(
                request.name, {**request.parameters, "resource": resource}
            )
            return _result(
                request.name,
                resource=_plain(value),
                evidence=["client-probe"],
                next_actions=[],
            )
        return _result(
            request.name,
            resource=_plain(config.clients[resource]),
            evidence=["desired-state"],
            next_actions=[],
        )

    def _config_query(self, request: OperationRequest) -> Mapping[str, object]:
        name = request.name
        if name == "config.path":
            return _result(
                name,
                path=str(self._config_path),
                evidence=["local-path"],
                next_actions=[],
            )
        if name == "config.history":
            return _result(
                name,
                items=[_plain(item) for item in self._config_store.history()],
                evidence=["config-journal"],
                next_actions=[],
            )
        if not self._config_store.exists:
            return _result(
                name,
                state="uninitialized",
                path=str(self._config_path),
                text="" if name == "config.export" else None,
                items=[] if name == "config.diff" else None,
                evidence=["desired-state-absent"],
                next_actions=["mlxctl setup"],
            )
        snapshot = self._config_store.load()
        if name == "config.show":
            return _result(
                name,
                resource=_plain(snapshot.value),
                revision=snapshot.revision,
                evidence=["desired-state"],
                next_actions=[],
            )
        if name == "config.validate":
            return _result(
                name,
                state="valid",
                revision=snapshot.revision,
                evidence=["schema-validation"],
                next_actions=[],
            )
        if name == "config.diff":
            text = request.parameters.get("text")
            if text is None and request.parameters.get("source") is not None:
                text = _read_config_source(str(request.parameters["source"]))
            candidate = (
                tomlkit.parse(str(text)) if text is not None else snapshot.document
            )
            return _result(
                name,
                items=[_plain(item) for item in self._config_store.diff(candidate)],
                evidence=["semantic-diff"],
                next_actions=[],
            )
        return _result(
            name,
            text=self._config_store.export_text(),
            revision=snapshot.revision,
            evidence=["desired-state"],
            next_actions=[],
        )

    def _telemetry(
        self,
        kind: str,
        scope: str,
        resource: str | None,
        *,
        operation: str | None = None,
    ) -> Mapping[str, object]:
        source = self._logs if kind == "logs" else self._metrics
        method = source.read if kind == "logs" else source.query
        items = [_plain(item) for item in method(scope, resource)]
        return _result(
            operation or kind,
            items=items,
            evidence=[f"{kind}-source"] if items else [f"no-{kind}-observed"],
            next_actions=[],
        )

    def _mutate(
        self, request: OperationRequest, preview: Mapping[str, object]
    ) -> Mapping[str, object]:
        name = request.name
        parameters = dict(request.parameters)
        if name == "setup":
            value = self._setup.execute(name, parameters)
        elif name in _SUPERVISOR_MUTATIONS:
            if name.startswith("service."):
                config = self._config()
                _resource(request, config.services, "Inference Service")
            value = self._supervisor.execute(name, parameters)
        elif name in _RUNTIME_MUTATIONS:
            self._validate_runtime_mutation(request, self._config())
            value = self._runtime_supply.execute(name, parameters)
        elif name in _MODEL_LONG_MUTATIONS:
            value = self._execute_model_mutation(name, parameters)
        elif name in {"client.configure", "client.remove"}:
            value = self._clients.execute(name, parameters)
        elif name in _LOCAL_MUTATIONS:
            value = self._local_mutation(request)
        else:
            raise ApplicationError(
                "operation_unavailable", f"{name} has no mutation owner"
            )
        return _result(
            name,
            state="accepted",
            preview=preview,
            resource=_plain(value),
            evidence=["owner-result"],
            next_actions=[],
        )

    def _validate_runtime_mutation(
        self, request: OperationRequest, config: MlxctlConfig
    ) -> None:
        if request.name in {"runtime.install", "runtime.adopt"}:
            key = str(
                request.parameters.get(
                    "runtime", request.parameters.get("resource", "")
                )
            )
            try:
                self._runtime_catalogue.definition(key)
            except KeyError as error:
                raise _not_found("Runtime Definition", key) from error
        elif request.name != "runtime.prune":
            _resource(request, config.runtimes, "Runtime Installation")

    def _execute_model_mutation(
        self, name: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        execute = getattr(self._model_supply, "execute", None)
        if callable(execute):
            return execute(name, parameters)
        if name == "model.install":
            repository = str(parameters["repository"])
            return _plain(
                self._model_supply.install(
                    alias=str(parameters.get("alias") or repository.rsplit("/", 1)[-1]),
                    repo_id=repository,
                    revision=str(parameters.get("revision", "main")),
                    offline=bool(parameters.get("offline", False)),
                )
            )
        config = self._config()
        resource = str(parameters.get("resource", ""))
        if name == "model.repair":
            installation_name = self._model_installation_name(resource, config)
            return _plain(
                self._model_supply.repair(
                    self._supplied_model_installation(installation_name, config)
                )
            )
        if name in {"model.update", "model.rollback"}:
            installation_name = self._model_installation_name(resource, config)
            installation = config.models[installation_name]
            alias = str(parameters.get("alias", resource))
            return _plain(
                self._model_supply.install(
                    alias=alias,
                    repo_id=installation.revision.repository,
                    revision=str(parameters["revision"]),
                    offline=bool(parameters.get("offline", False)),
                )
            )
        raise ApplicationError(
            "operation_unavailable",
            f"{name} requires an extended model-supply port",
        )

    @staticmethod
    def _model_installation_name(resource: str, config: MlxctlConfig) -> str:
        if resource in config.aliases:
            return config.aliases[resource].installation_name
        return resource

    def _supplied_model_installation(
        self, installation_name: str, config: MlxctlConfig
    ) -> SuppliedModelInstallation:
        desired = config.models[installation_name]
        cached = next(
            (
                item
                for item in self._model_supply.inventory().revisions
                if item.repo_id == desired.revision.repository
                and item.commit_sha == desired.revision.revision
            ),
            None,
        )
        if cached is None:
            raise ApplicationError(
                "resource_not_found",
                f"Cached Revision for Model Installation {installation_name!r} is absent",
                next_actions=(f"mlxctl model repair {installation_name}",),
            )
        revision = ModelRevision(
            repo_id=desired.revision.repository,
            commit_sha=desired.revision.revision,
            requested_revision=desired.revision.revision,
            evidence="desired-state",
        )
        return SuppliedModelInstallation(
            installation_id=installation_name,
            revision=revision,
            cached_revision_id=cached.revision_id,
            snapshot_path=cached.snapshot_path,
            provenance=ModelProvenance(
                requested_revision=desired.revision.revision,
                resolved_sha=desired.revision.revision,
                source="desired-state",
            ),
        )

    def _local_mutation(self, request: OperationRequest) -> Mapping[str, object]:
        name = request.name
        parameters = request.parameters
        if name == "config.import":
            text = parameters.get("text")
            if text is None:
                text = _read_config_source(str(parameters["source"]))
            return _plain(self._config_store.import_text(str(text)))
        if name == "config.restore":
            return _plain(self._config_store.restore(str(parameters["revision"])))
        if name == "model.trust":
            resource = str(parameters.get("resource", parameters.get("model", "")))
            config = self._config()
            _resource(
                OperationRequest(name, {"resource": resource}),
                config.models,
                "Model Installation",
            )
            return self._state_store.put_snapshot(
                {
                    "kind": "trust",
                    "id": resource,
                    "version": str(parameters.get("revision", "1")),
                    "accepted_risks": list(parameters.get("accepted_risks", ())),
                }
            )

        def edit(document) -> None:
            if name == "gateway.configure":
                gateway = document.setdefault("gateway", tomlkit.table())
                for key in ("host", "port"):
                    if key in parameters:
                        gateway[key] = parameters[key]
                return
            if name.startswith("service."):
                services = document.setdefault("services", tomlkit.table())
                resource = str(
                    parameters.get(
                        "resource",
                        parameters.get("service", parameters.get("name", "")),
                    )
                )
                if (
                    name in {"service.edit", "service.remove"}
                    and resource not in services
                ):
                    raise _not_found("Inference Service", resource)
                if name == "service.remove":
                    del services[resource]
                    return
                table = services.get(resource, tomlkit.table())
                for key in (
                    "model_alias",
                    "runtime",
                    "route",
                    "activation",
                    "pinned",
                    "options",
                ):
                    if key in parameters:
                        table[key] = parameters[key]
                services[resource] = table
                return
            if name == "model.uninstall":
                models = document.setdefault("models", tomlkit.table())
                resource = str(parameters.get("resource", ""))
                if resource not in models:
                    raise _not_found("Model Installation", resource)
                aliases = document.get("aliases", {})
                referenced = sorted(
                    key
                    for key, item in aliases.items()
                    if item.get("installation") == resource
                )
                if referenced:
                    raise ApplicationError(
                        "resource_in_use",
                        f"Model Installation {resource!r} is selected by aliases: {', '.join(referenced)}",
                    )
                del models[resource]

        return _plain(self._config_store.edit(edit))

    def _service_items(self, config: MlxctlConfig) -> list[dict[str, object]]:
        runs: dict[str, Mapping[str, object]] = {}
        for run in self._state_store.snapshots("service_run"):
            service = str(run.get("service", run.get("service_name", "")))
            runs[service] = run
        return [
            {"name": name, "desired": _plain(service), "run": runs.get(name)}
            for name, service in sorted(config.services.items())
        ]

    def _latest(self, kind: str, resource: str) -> dict[str, object] | None:
        return self._state_store.snapshot(kind, resource)

    def _config(self) -> MlxctlConfig:
        if self._config_store.exists:
            return self._config_store.load().value
        return validate_config({"schema_version": 1})


def _resource(
    request: OperationRequest,
    available: Mapping[str, object] | set[str],
    noun: str,
) -> str:
    names = set(available)
    raw = request.parameters.get(
        "resource", request.parameters.get("name", request.parameters.get("id"))
    )
    if raw is None:
        if len(names) == 1:
            return next(iter(names))
        raise ApplicationError("resource_required", f"{noun} resource is required")
    resource = str(raw)
    if resource not in names:
        raise _not_found(noun, resource)
    return resource


def _not_found(noun: str, resource: str) -> ApplicationError:
    return ApplicationError(
        "resource_not_found",
        f"{noun} {resource!r} is not configured",
        next_actions=(f"list {noun.lower()} resources",),
    )


def _read_config_source(source: str, *, max_bytes: int = 1024 * 1024) -> str:
    path = Path(source).expanduser()
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise ApplicationError(
            "config_source_missing", f"config source is absent: {source}"
        ) from error
    if path.is_symlink() or not path.is_file():
        raise ApplicationError(
            "config_source_unsafe", "config source must be a regular non-symlink file"
        )
    if metadata.st_size > max_bytes:
        raise ApplicationError(
            "config_source_too_large",
            f"config source exceeds the {max_bytes}-byte limit",
        )
    return path.read_text(encoding="utf-8")


def _result(operation: str, **value: object) -> Mapping[str, object]:
    return {"schema_version": 1, "operation": operation, **value}


def _plain(value: object) -> object:
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _plain(getattr(value, name))
            for name in value.__dataclass_fields__  # type: ignore[attr-defined]
        }
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, frozenset, set)):
        return [_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value
