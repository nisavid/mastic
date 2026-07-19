"""Local supported-v1 implementation of the shared operation catalogue."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

import tomlkit

from mastic.application.catalogue import Operation, OperationKind
from mastic.application.config_schema import MasticConfig, validate_config
from mastic.application.dispatch import (
    ApplicationError,
    OperationRequest,
    PreparedOperation,
)
from mastic.application.serialization import to_plain_data as _plain
from mastic.infrastructure.config_store import ConfigRevisionConflict, ConfigStore
from mastic.infrastructure.model_supply import (
    ModelInstallation as SuppliedModelInstallation,
)
from mastic.infrastructure.model_intelligence import RuntimeObservation
from mastic.infrastructure.model_supply import (
    ModelProvenance,
    ModelRevision,
    ModelSupply,
)
from mastic.infrastructure.runtime_supply import RuntimeCatalogue
from mastic.infrastructure.state_store import OperationalStateStore


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
        "model.adopt",
        "model.repair",
        "model.update",
        "model.rollback",
        "model.cache.evict",
        "model.cache.prune",
    }
)
_LOCAL_MUTATIONS = frozenset(
    {
        "remove",
        "gateway.configure",
        "model.uninstall",
        "model.trust",
        "service.create",
        "service.edit",
        "application-target.configure",
        "application-target.remove",
        "config.import",
        "config.restore",
    }
)
_APPLICATION_TARGET_NAMES = frozenset({"codex", "hindsight"})


class LocalOperationBackend:
    """Prepare observations and owner-scoped mutations without hidden activation."""

    def __init__(
        self,
        *,
        catalogue: Mapping[str, Operation],
        config_store: ConfigStore[MasticConfig],
        state_store: OperationalStateStore,
        runtime_catalogue: RuntimeCatalogue,
        runtime_supply: OperationPort,
        model_supply: ModelSupply,
        supervisor: OperationPort,
        logs: LogReader,
        metrics: MetricsSource,
        setup: OperationPort,
        application_targets: OperationPort,
        config_path: str | Path,
        gateway_credential_path: str | Path | None = None,
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
        self._application_targets = application_targets
        self._config_path = Path(config_path)
        self._gateway_credential_path = (
            Path(gateway_credential_path)
            if gateway_credential_path is not None
            else None
        )
        self._model_intelligence = model_intelligence

    def prepare(self, request: OperationRequest) -> PreparedOperation:
        operation = self._catalogue.get(request.name)
        if operation is None:
            raise ApplicationError(
                "unknown_operation", f"unknown operation: {request.name}"
            )
        _validate_parameter_types(operation, request.parameters)
        _validate_required_model_parameters(request)
        self._validate_request(request)
        if operation.kind is OperationKind.QUERY:
            return PreparedOperation(False, lambda: self._query(request))
        preview_method = None
        if request.name == "setup":
            preview_method = getattr(self._setup, "preview", None)
        elif request.name == "remove":
            preview_method = getattr(self._setup, "preview_removal", None)
        if callable(preview_method):
            resolved = (
                preview_method()
                if request.name == "remove"
                else preview_method(request.parameters)
            )
            preview = {
                "schema_version": 1,
                "operation": request.name,
                "confirmation_required": operation.confirmation,
                **_plain(resolved),
            }
        else:
            preview = self._resolved_mutation_preview(request, operation)
        owner_fingerprint = preview.get("preview_fingerprint")
        if not isinstance(owner_fingerprint, str):
            owner_fingerprint = _preview_fingerprint(preview)
            preview["preview_fingerprint"] = owner_fingerprint
        if request.parameters.get("confirmed") is True:
            supplied = request.parameters.get("preview_fingerprint")
            if supplied != owner_fingerprint:
                raise _stale_preview(request.name)
        requires_supervisor = (
            request.name
            in (_SUPERVISOR_MUTATIONS | _RUNTIME_MUTATIONS | _MODEL_LONG_MUTATIONS)
            or request.name == "service.remove"
        )
        return PreparedOperation(
            requires_supervisor=requires_supervisor,
            execute=lambda: self._mutate(request, preview),
            events=({"phase": "preview", **preview},),
        )

    def _resolved_mutation_preview(
        self, request: OperationRequest, operation: Operation
    ) -> dict[str, object]:
        """Resolve a confirmation-bound preview without crossing a mutation boundary."""

        parameters = {
            key: _plain(value)
            for key, value in request.parameters.items()
            if key not in {"confirmed", "preview_fingerprint"}
        }
        config = self._config()
        config_revision = (
            self._config_store.load().revision if self._config_store.exists else None
        )
        resolved: dict[str, object] = {
            "schema_version": 1,
            "operation": request.name,
            "confirmation_required": operation.confirmation,
            "config_revision": config_revision,
            "parameters": parameters,
        }
        if request.name == "config.import":
            resolved["candidate_sha256"] = _config_import_digest(request.parameters)
        elif request.name == "model.install":
            resolve = getattr(self._model_supply, "resolve", None)
            if callable(resolve):
                revision = resolve(
                    str(parameters["repository"]),
                    str(parameters.get("revision", "main")),
                    offline=bool(parameters.get("offline", False)),
                )
                resolved["target"] = _plain(revision)
        elif request.name == "model.adopt":
            inspect_adoption = getattr(self._model_supply, "inspect_adoption", None)
            if not callable(inspect_adoption):
                raise ApplicationError(
                    "operation_unavailable",
                    "model adoption inspection is unavailable",
                )
            resolved["target"] = _plain(inspect_adoption(str(parameters["path"])))
        elif request.name == "model.update":
            resource = str(parameters["resource"])
            installation_name = self._model_installation_name(resource, config)
            current = config.models[installation_name]
            resolve = getattr(self._model_supply, "resolve", None)
            if callable(resolve):
                revision = resolve(
                    current.revision.repository,
                    str(parameters["revision"]),
                    offline=bool(parameters.get("offline", False)),
                )
                resolved["target"] = _plain(revision)
            resolved["current_installation"] = installation_name
        elif request.name == "runtime.install":
            runtime = str(parameters.get("runtime", ""))
            channel = str(
                parameters.get(
                    "channel", "custom" if parameters.get("version") else "tested"
                )
            )
            if channel == "tested":
                bundles = sorted(
                    (
                        bundle
                        for bundle in self._runtime_catalogue.tested_bundles
                        if bundle.runtime == runtime
                    ),
                    key=lambda item: (item.version, item.bundle_id),
                )
                if bundles:
                    bundle = bundles[-1]
                    resolved["target"] = {
                        "installation": bundle.bundle_id,
                        "runtime": bundle.runtime,
                        "version": bundle.version,
                        "lock_sha256": bundle.lock_sha256,
                    }
        elif request.name.startswith("runtime.") and request.name not in {
            "runtime.prune",
            "runtime.install",
            "runtime.adopt",
        }:
            resource = str(parameters.get("resource", ""))
            current = config.runtimes[resource]
            resolved["current"] = _plain(current)
            resolved["affected_services"] = sorted(
                name
                for name, service in config.services.items()
                if service.runtime_installation == resource
            )
        elif request.name.startswith("service."):
            resource = str(parameters.get("resource", parameters.get("service", "")))
            if resource in config.services:
                resolved["current"] = _plain(config.services[resource])
        elif request.name == "model.uninstall":
            resource = str(parameters.get("resource", ""))
            installation = self._model_installation_name(resource, config)
            resolved["installation"] = installation
            resolved["aliases"] = sorted(
                alias
                for alias, target in config.aliases.items()
                if target.installation_name == installation
            )
        return resolved

    def _validate_request(self, request: OperationRequest) -> None:
        """Resolve named resources before returning a mutation preview."""
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
        elif name == "application-target.test":
            _resource(
                request, config.application_targets, "Application Configuration Target"
            )
        elif name in {
            "application-target.inspect",
            "application-target.remove",
        }:
            _resource(
                request,
                set(config.application_targets) | _APPLICATION_TARGET_NAMES,
                "Application Configuration Target",
            )

    def _query(self, request: OperationRequest) -> Mapping[str, object]:
        name = request.name
        config = self._config()
        if name in {"status", "check", "doctor", "tui"}:
            return self._overview(name, config)
        if name in {"logs", "metrics"}:
            resource = request.parameters.get("resource")
            return self._telemetry(
                name,
                "all",
                str(resource) if resource is not None else None,
            )
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
        if name.startswith("application-target."):
            return self._application_target_query(request, config)
        if name.startswith("config."):
            return self._config_query(request)
        raise ApplicationError("operation_unavailable", f"{name} has no local backend")

    def _overview(self, name: str, config: MasticConfig) -> Mapping[str, object]:
        supervisor = self._latest("supervisor", "supervisor") or {"state": "stopped"}
        gateway = self._latest("gateway", "gateway") or {
            "state": "stopped",
            "host": config.gateway.host,
            "port": config.gateway.port,
        }
        services = self._service_items(config)
        operations = list(self._state_store.operations())
        active_operations = sum(
            1
            for operation in operations
            if operation.get("status") in {"queued", "running", "resuming"}
        )
        pressure = str(supervisor.get("pressure", "unknown"))
        failed = [
            item["name"]
            for item in services
            if (item["run"] or {}).get("state") in {"failed", "unhealthy"}
        ]
        component_failed = any(
            component.get("state") in {"failed", "unhealthy"}
            for component in (supervisor, gateway)
        )
        gateway_stopped = (
            supervisor.get("state") != "stopped" and gateway.get("state") == "stopped"
        )
        state = (
            "failed"
            if failed or component_failed or gateway_stopped
            else ("stopped" if supervisor.get("state") == "stopped" else "ok")
        )
        next_actions = []
        if supervisor.get("state") == "stopped":
            next_actions.append("mastic supervisor start")
        elif gateway_stopped:
            next_actions.append("mastic gateway restart")
        if failed or component_failed:
            next_actions.append("mastic doctor")
        details: dict[str, object] = {}
        if name == "check":
            details["checks"] = [
                {
                    "name": "supervisor",
                    "state": str(supervisor.get("state", "stopped")),
                },
                {
                    "name": "gateway",
                    "state": str(gateway.get("state", "stopped")),
                },
                *(
                    {
                        "name": f"service:{item['name']}",
                        "state": str((item["run"] or {}).get("state", "stopped")),
                    }
                    for item in services
                ),
            ]
        elif name == "doctor":
            issues = self._diagnostic_issues(config, supervisor, gateway, services)
            if "codex" in config.application_targets:
                try:
                    codex = config.application_targets["codex"]
                    service = config.services.get(codex.service)
                    if service is None:
                        service = next(
                            (
                                candidate
                                for candidate in config.services.values()
                                if candidate.route == codex.service
                            ),
                            None,
                        )
                    if service is None:
                        raise KeyError(codex.service)
                    service_context = service.options.get("max_context")
                    if (
                        service_context is not None
                        and codex.context_window != service_context
                    ):
                        issues.append(
                            {
                                "code": "codex_context_drift",
                                "message": "Codex context does not match the selected Inference Service cap.",
                                "next_actions": [
                                    "mastic application-target configure codex"
                                ],
                            }
                        )
                    integration = self._application_targets.execute(
                        "application-target.inspect", {"application_target": "codex"}
                    )
                    if integration.get("state") in {
                        "missing",
                        "drifted",
                        "incompatible",
                        "malformed",
                    }:
                        issues.append(
                            {
                                "code": "codex_catalog_unhealthy",
                                "message": str(
                                    integration.get(
                                        "detail",
                                        "Codex Application Configuration Target is unhealthy.",
                                    )
                                ),
                                "next_actions": list(
                                    integration.get(
                                        "next_actions",
                                        ["mastic application-target configure codex"],
                                    )
                                ),
                            }
                        )
                except Exception as error:
                    issues.append(
                        {
                            "code": "codex_catalog_unknown",
                            "message": f"Codex integration inspection failed: {error}",
                            "next_actions": ["mastic application-target inspect codex"],
                        }
                    )
            details["issues"] = issues
            details["healthy"] = not issues
            next_actions.extend(
                action
                for issue in issues
                for action in issue.get("next_actions", [])
                if action not in next_actions
            )
        return _result(
            name,
            state=state,
            supervisor=supervisor,
            gateway=gateway,
            services=services,
            operations=operations,
            active_operations=active_operations,
            pressure=pressure,
            failed_services=failed,
            evidence=["desired-state", "operational-state"],
            next_actions=next_actions,
            **details,
        )

    def _supervisor_query(self, name: str) -> Mapping[str, object]:
        if name == "supervisor.logs":
            return self._telemetry("logs", "supervisor", None, operation=name)
        snapshot = self._latest("supervisor", "supervisor") or {"state": "stopped"}
        details = (
            {"operations": list(self._state_store.operations())}
            if name == "supervisor.inspect"
            else {}
        )
        return _result(
            name,
            state=str(snapshot.get("state", "unknown")),
            resource=snapshot,
            evidence=["operational-state"],
            next_actions=["mastic supervisor start"]
            if snapshot.get("state") == "stopped"
            else [],
            **details,
        )

    def _gateway_query(self, name: str, config: MasticConfig) -> Mapping[str, object]:
        if name == "gateway.logs":
            return self._telemetry("logs", "gateway", None, operation=name)
        if name == "gateway.metrics":
            return self._telemetry("metrics", "gateway", None, operation=name)
        snapshot = self._latest("gateway", "gateway") or {"state": "stopped"}
        routes = [
            {"route": str(service.route), "service": service_name}
            for service_name, service in sorted(config.services.items())
        ]
        if name == "gateway.routes":
            return _result(
                name,
                items=routes,
                evidence=["desired-state", "operational-state"],
                next_actions=[],
            )
        if name == "gateway.status":
            return _result(
                name,
                state=str(snapshot.get("state", "stopped")),
                endpoint={"host": config.gateway.host, "port": config.gateway.port},
                route_count=len(routes),
                evidence=["desired-state", "operational-state"],
                next_actions=[],
            )
        return _result(
            name,
            state=str(snapshot.get("state", "stopped")),
            endpoint={"host": config.gateway.host, "port": config.gateway.port},
            routes=routes,
            credential=(
                {
                    "scheme": "Bearer",
                    "path": str(self._gateway_credential_path),
                    "instructions": "Configure Application Configuration Targets with mastic application-target configure; do not copy the token into desired state.",
                }
                if self._gateway_credential_path is not None
                else None
            ),
            resource=snapshot,
            evidence=["desired-state", "operational-state"],
            next_actions=[],
        )

    def _runtime_query(
        self, request: OperationRequest, config: MasticConfig
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
        if name == "runtime.list":
            items = [_plain(item) for _, item in sorted(config.runtimes.items())]
            return _result(
                name, items=items, evidence=["desired-state"], next_actions=[]
            )
        if name == "runtime.doctor":
            items = [
                {
                    "installation_id": key,
                    "root": item.root,
                    "root_exists": Path(item.root).is_dir(),
                    "launcher": item.launcher[0],
                    "launcher_exists": Path(item.launcher[0]).is_file(),
                    "state": (
                        "ready"
                        if Path(item.root).is_dir() and Path(item.launcher[0]).is_file()
                        else "missing"
                    ),
                }
                for key, item in sorted(config.runtimes.items())
            ]
            return _result(
                name,
                items=items,
                evidence=["desired-state", "local-filesystem"],
                next_actions=["mastic runtime install --help"]
                if any(item["state"] == "missing" for item in items)
                else [],
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
        self, request: OperationRequest, config: MasticConfig
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
            configured_installation = None
            if selected in config.models:
                configured_installation = config.models[selected]
                installed = configured_installation
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
            resource = _plain_model_intelligence_report(report)
            evidence = ["exact-hub-metadata", "local-machine-inventory"]
            if (
                configured_installation is not None
                and configured_installation.provenance == "adopted"
            ):
                if configured_installation.path is None:
                    raise ApplicationError(
                        "invalid_state",
                        "adopted model installation is missing its snapshot path",
                    )
                inspect_adoption = getattr(self._model_supply, "inspect_adoption", None)
                snapshot = (
                    _plain(inspect_adoption(configured_installation.path))
                    if callable(inspect_adoption)
                    else {"path": configured_installation.path}
                )
                resource["installation"] = {
                    "provenance": "external-adopted",
                    "snapshot": snapshot,
                }
                evidence.append("external-adopted-snapshot")
            return _result(
                name,
                resource=resource,
                evidence=evidence,
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
                next_actions=[f"mastic model repair {installation_name}"]
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
            next_actions=[f"mastic model verify {installation_name}"]
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
        self, request: OperationRequest, config: MasticConfig
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
        details = {}
        if request.name == "service.check":
            gateway = self._latest("gateway", "gateway") or {"state": "stopped"}
            details["checks"] = [
                {"name": "desired-state", "state": "valid"},
                {"name": "service-run", "state": state},
                {
                    "name": "gateway-route",
                    "state": "ready"
                    if state == "ready" and gateway.get("state") in {"ready", "running"}
                    else "unavailable",
                },
            ]
        return _result(
            request.name,
            state=state,
            resource=item,
            evidence=["desired-state", "operational-state"],
            next_actions=[f"mastic service start {resource}"]
            if state == "stopped"
            else [],
            **details,
        )

    def _diagnostic_issues(
        self,
        config: MasticConfig,
        supervisor: Mapping[str, object],
        gateway: Mapping[str, object],
        services: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        issues: list[dict[str, object]] = []
        if supervisor.get("state") == "failed":
            issues.append(
                {
                    "code": "supervisor_failed",
                    "message": "The Supervisor reported a failed state.",
                    "next_actions": ["mastic supervisor logs"],
                }
            )
        gateway_host = gateway.get("host")
        gateway_port = gateway.get("port")
        if gateway.get("state") == "running" and (
            gateway_host != config.gateway.host or gateway_port != config.gateway.port
        ):
            issues.append(
                {
                    "code": "gateway_drift",
                    "message": "The running Gateway endpoint differs from desired state.",
                    "next_actions": ["mastic gateway restart"],
                }
            )
        for item in services:
            run = item.get("run")
            if isinstance(run, Mapping) and run.get("state") in {"failed", "unhealthy"}:
                name = str(item.get("name", "unknown"))
                issues.append(
                    {
                        "code": "service_unhealthy",
                        "message": f"Inference Service {name!r} is unhealthy.",
                        "next_actions": [f"mastic service logs {name}"],
                    }
                )
        return issues

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

    def _application_target_query(
        self, request: OperationRequest, config: MasticConfig
    ) -> Mapping[str, object]:
        if request.name == "application-target.list":
            return _result(
                request.name,
                items=[
                    _plain(item)
                    for _, item in sorted(config.application_targets.items())
                ],
                evidence=["desired-state"],
                next_actions=[],
            )
        if request.name == "application-target.test":
            resource = _resource(
                request,
                config.application_targets,
                "Application Configuration Target",
            )
            value = self._application_targets.execute(
                request.name, {**request.parameters, "application_target": resource}
            )
            return _result(
                request.name,
                resource=_plain(value),
                evidence=["application-target-probe"],
                next_actions=[],
            )
        if request.name == "application-target.inspect":
            resource = _resource(
                request,
                set(config.application_targets) | _APPLICATION_TARGET_NAMES,
                "Application Configuration Target",
            )
            value = self._application_targets.execute(
                request.name, {**request.parameters, "application_target": resource}
            )
            desired = config.application_targets.get(resource)
            evidence = ["managed-application-target-files"]
            if desired is not None:
                evidence.insert(0, "desired-state")
            return _result(
                request.name,
                resource={
                    "desired": _plain(desired) if desired is not None else None,
                    "integration": _plain(value),
                },
                evidence=evidence,
                next_actions=list(value.get("next_actions", [])),
            )
        resource = _resource(
            request, config.application_targets, "Application Configuration Target"
        )
        return _result(
            request.name,
            resource=_plain(config.application_targets[resource]),
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
                next_actions=["mastic setup"],
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
        if "config_revision" in preview:
            current_revision = (
                self._config_store.load().revision
                if self._config_store.exists
                else None
            )
            if current_revision != preview.get("config_revision"):
                raise _stale_preview(name)
        parameters = dict(request.parameters)
        target = preview.get("target")
        if isinstance(target, Mapping):
            if name in {"model.install", "model.update"} and isinstance(
                target.get("commit_sha"), str
            ):
                parameters["revision"] = target["commit_sha"]
            elif name == "model.adopt" and isinstance(target.get("fingerprint"), str):
                parameters["snapshot_fingerprint"] = target["fingerprint"]
            elif name == "runtime.install":
                if isinstance(target.get("installation"), str):
                    parameters["bundle_id"] = target["installation"]
                if isinstance(target.get("version"), str):
                    parameters["expected_version"] = target["version"]
                if isinstance(target.get("lock_sha256"), str):
                    parameters["expected_lock_digest"] = target["lock_sha256"]
        if name == "setup":
            value = self._setup.execute(name, parameters)
        elif name == "remove":
            remove = getattr(self._setup, "remove", None)
            if not callable(remove):
                raise ApplicationError(
                    "operation_unavailable", "product removal is not configured"
                )
            value = remove(parameters)
        elif name == "service.remove":
            resource = str(parameters.get("resource", ""))
            service = self._config().services.get(resource)
            if service is None:
                raise _not_found("Inference Service", resource)
            configured = self._local_mutation(request, preview)
            removed = self._supervisor.execute(
                "service.remove",
                {
                    "resource": resource,
                    "previous_route": str(service.route),
                    "confirmed": True,
                },
            )
            value = {
                "service": resource,
                "lifecycle": removed,
                "configuration": configured,
            }
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
        elif name in {"application-target.configure", "application-target.remove"}:
            value = self._application_targets.execute(name, parameters)
        elif name in _LOCAL_MUTATIONS:
            value = self._local_mutation(request, preview)
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
        self, request: OperationRequest, config: MasticConfig
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
            if name == "model.rollback":
                target_name = str(parameters["target"])
                target = config.models.get(target_name)
                if target is None:
                    raise _not_found("Model Installation", target_name)
                if target.revision.repository != installation.revision.repository:
                    raise ApplicationError(
                        "invalid_parameter",
                        "model rollback target must have the same repository",
                    )
                revision = target.revision.revision
            else:
                revision = str(parameters["revision"])
            return _plain(
                self._model_supply.install(
                    alias=alias,
                    repo_id=installation.revision.repository,
                    revision=revision,
                    offline=bool(parameters.get("offline", False)),
                )
            )
        raise ApplicationError(
            "operation_unavailable",
            f"{name} requires an extended model-supply port",
        )

    @staticmethod
    def _model_installation_name(resource: str, config: MasticConfig) -> str:
        if resource in config.aliases:
            return config.aliases[resource].installation_name
        return resource

    def _supplied_model_installation(
        self, installation_name: str, config: MasticConfig
    ) -> SuppliedModelInstallation:
        desired = config.models[installation_name]
        if desired.provenance == "adopted":
            if desired.path is None:
                raise ApplicationError(
                    "invalid_state",
                    "adopted model installation is missing its snapshot path",
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
                cached_revision_id=revision.revision_id,
                snapshot_path=Path(desired.path),
                provenance=ModelProvenance(
                    requested_revision=desired.revision.revision,
                    resolved_sha=desired.revision.revision,
                    source="external-adopted",
                ),
            )
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
                next_actions=(f"mastic model repair {installation_name}",),
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

    def _local_mutation(
        self,
        request: OperationRequest,
        preview: Mapping[str, object],
    ) -> Mapping[str, object]:
        name = request.name
        parameters = request.parameters
        if name == "config.import":
            text = _config_import_text(parameters)
            if preview.get("candidate_sha256") != _text_digest(text):
                raise _stale_preview(name)
            try:
                return _plain(
                    self._config_store.import_text(
                        text,
                        expected_revision=preview.get("config_revision"),
                    )
                )
            except ConfigRevisionConflict as error:
                raise _stale_preview(name) from error
        if name == "config.restore":
            try:
                return _plain(
                    self._config_store.restore(
                        str(parameters["revision"]),
                        expected_revision=preview.get("config_revision"),
                    )
                )
            except ConfigRevisionConflict as error:
                raise _stale_preview(name) from error
        if name == "model.trust":
            resource = str(parameters.get("resource", parameters.get("model", "")))
            config = self._config()
            installation_name = self._model_installation_name(resource, config)
            _resource(
                OperationRequest(name, {"resource": installation_name}),
                config.models,
                "Model Installation",
            )
            runtime = str(parameters.get("runtime", ""))
            _resource(
                OperationRequest(name, {"resource": runtime}),
                config.runtimes,
                "Runtime Installation",
            )
            model = config.models[installation_name]
            revision = str(parameters.get("revision", model.revision.revision))
            if revision != model.revision.revision:
                raise ApplicationError(
                    "revision_mismatch",
                    "trust must target the configured exact Model Revision",
                )
            accepted = parameters.get("accepted_risks")
            if not isinstance(accepted, (tuple, list)) or not all(
                isinstance(item, str) and item for item in accepted
            ):
                raise ApplicationError(
                    "invalid_parameter",
                    "accepted_risks must be a JSON array of nonempty strings",
                )
            return self._state_store.put_snapshot(
                {
                    "kind": "trust",
                    "id": f"{installation_name}@{runtime}",
                    "version": revision,
                    "model_installation": installation_name,
                    "repository": model.revision.repository,
                    "revision": revision,
                    "runtime_installation": runtime,
                    "accepted_risks": list(accepted),
                }
            )

        model_uninstall: tuple[str, tuple[str, ...]] | None = None
        if name == "model.uninstall":
            config = self._config()
            resource = str(parameters.get("resource", ""))
            installation_name = self._model_installation_name(resource, config)
            aliases = tuple(
                sorted(
                    alias
                    for alias, target in config.aliases.items()
                    if target.installation_name == installation_name
                )
            )
            referenced = tuple(
                sorted(
                    service_name
                    for service_name, service in config.services.items()
                    if str(service.model_alias) in aliases
                )
            )
            if referenced:
                raise ApplicationError(
                    "resource_in_use",
                    f"Model Installation {installation_name!r} is selected by services: "
                    + ", ".join(referenced),
                    next_actions=tuple(
                        f"mastic service remove {service}" for service in referenced
                    ),
                )
            model_uninstall = installation_name, aliases

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
                assert model_uninstall is not None
                installation_name, aliases_to_remove = model_uninstall
                alias_table = document.setdefault("aliases", tomlkit.table())
                for alias in aliases_to_remove:
                    alias_table.pop(alias, None)
                del models[installation_name]

        try:
            if not self._config_store.exists:
                document = tomlkit.parse("schema_version = 1\n")
                edit(document)
                return _plain(
                    self._config_store.save(
                        document,
                        action="edit",
                        expected_revision=preview.get("config_revision"),
                    )
                )
            return _plain(
                self._config_store.edit(
                    edit,
                    expected_revision=preview.get("config_revision"),
                )
            )
        except ConfigRevisionConflict as error:
            raise _stale_preview(name) from error

    def _service_items(self, config: MasticConfig) -> list[dict[str, object]]:
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

    def _config(self) -> MasticConfig:
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
        "resource",
        request.parameters.get(
            "application_target",
            request.parameters.get("name", request.parameters.get("id")),
        ),
    )
    if raw is None:
        if len(names) == 1:
            return next(iter(names))
        raise ApplicationError("resource_required", f"{noun} resource is required")
    resource = str(raw)
    if resource not in names:
        raise _not_found(noun, resource)
    return resource


def _validate_parameter_types(
    operation: Operation, parameters: Mapping[str, object]
) -> None:
    specifications = {item.name: item for item in operation.parameters}
    expected_internal = {
        "confirmed": "boolean",
        "noninteractive": "boolean",
        "preview_fingerprint": "string",
    }
    for name, value in parameters.items():
        specification = specifications.get(name)
        value_type = (
            specification.value_type
            if specification is not None
            else expected_internal.get(name)
        )
        if value_type is None or value_type == "json":
            continue
        valid = (
            type(value) is bool
            if value_type in {"boolean", "tristate_boolean"}
            else type(value) is int
            if value_type == "integer"
            else type(value) is str
            if value_type == "string"
            else False
        )
        if not valid:
            raise ApplicationError(
                "invalid_parameter",
                f"{name} must be a {value_type.replace('_', ' ')}",
            )
        if specification is not None and specification.accepted:
            if value not in specification.accepted:
                raise ApplicationError(
                    "invalid_parameter",
                    f"{name} must be one of: {', '.join(specification.accepted)}",
                )


def _validate_required_model_parameters(request: OperationRequest) -> None:
    required = {
        "model.install": ("repository",),
        "model.adopt": ("repository", "revision", "path"),
        "model.update": ("resource", "revision"),
    }.get(request.name, ())
    for name in required:
        if name not in request.parameters or request.parameters[name] is None:
            raise ApplicationError(
                "invalid_parameter",
                f"{name} is required for {request.name}",
            )


def _not_found(noun: str, resource: str) -> ApplicationError:
    return ApplicationError(
        "resource_not_found",
        f"{noun} {resource!r} is not configured",
        next_actions=(f"list {noun.lower()} resources",),
    )


def _read_config_source(source: str, *, max_bytes: int = 1024 * 1024) -> str:
    path = Path(source).expanduser()
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except FileNotFoundError as error:
        raise ApplicationError(
            "config_source_missing", f"config source is absent: {source}"
        ) from error
    except OSError as error:
        raise ApplicationError(
            "config_source_unsafe", "config source must be a regular non-symlink file"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ApplicationError(
                "config_source_unsafe",
                "config source must be a regular non-symlink file",
            )
        if metadata.st_size > max_bytes:
            raise ApplicationError(
                "config_source_too_large",
                f"config source exceeds the {max_bytes}-byte limit",
            )
        payload = os.read(descriptor, max_bytes + 1)
        if len(payload) > max_bytes:
            raise ApplicationError(
                "config_source_too_large",
                f"config source exceeds the {max_bytes}-byte limit",
            )
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ApplicationError(
                "config_source_unsafe", "config source must be valid UTF-8"
            ) from error
    finally:
        os.close(descriptor)


def _config_import_text(parameters: Mapping[str, object]) -> str:
    text = parameters.get("text")
    if text is None:
        return _read_config_source(str(parameters["source"]))
    return str(text)


def _text_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _config_import_digest(parameters: Mapping[str, object]) -> str:
    return _text_digest(_config_import_text(parameters))


def _stale_preview(operation: str) -> ApplicationError:
    return ApplicationError(
        "stale_preview",
        "The mutation preview changed or was not reviewed; resolve it again.",
        next_actions=(
            f"mastic {operation.replace('.', ' ')} --help",
            "review the newly resolved preview before confirming",
        ),
    )


def _result(operation: str, **value: object) -> Mapping[str, object]:
    return {"schema_version": 1, "operation": operation, **value}


def _preview_fingerprint(preview: Mapping[str, object]) -> str:
    canonical = json.dumps(
        _plain(preview), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _plain_model_intelligence_report(report: object) -> dict[str, object]:
    resource = _plain(report)
    if not isinstance(resource, Mapping):
        raise ApplicationError(
            "invalid_model_report", "model intelligence returned an invalid report"
        )
    result = dict(resource)
    repository_files = result.pop("repository_files", ())
    if not isinstance(repository_files, (tuple, list)):
        raise ApplicationError(
            "invalid_model_report",
            "model intelligence returned an invalid repository manifest",
        )
    canonical = json.dumps(
        repository_files, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    result["repository_file_count"] = len(repository_files)
    result["repository_manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
    return result
