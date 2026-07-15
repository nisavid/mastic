"""Concrete supported-v1 runtime and model operation ports."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Mapping, Protocol

import tomlkit

from mlxctl.application.config_schema import ConfiguredRuntime, MlxctlConfig
from mlxctl.infrastructure.config_store import ConfigStore
from mlxctl.infrastructure.model_intelligence import (
    ModelIntelligence,
    ModelIntelligenceReport,
    RuntimeObservation,
)
from mlxctl.infrastructure.model_supply import (
    CachedRevision,
    ModelInstallResult,
    ModelInstallation as SuppliedModelInstallation,
    ModelProvenance,
    ModelRevision as SuppliedModelRevision,
    ModelSupply,
    VerificationResult,
)
from mlxctl.infrastructure.runtime_supply import (
    RuntimeCatalogue,
    RuntimeChangePlan,
    RuntimeChangePlanner,
    RuntimeInstallation,
    RuntimeManager,
)


class SupplyPortError(ValueError):
    """A requested supply transition is invalid or unsafe."""


class ModelSecurityPolicyError(SupplyPortError):
    """Exact-revision model security evidence is absent or disqualifying."""


class SecurityEvidenceStore(Protocol):
    def put_snapshot(self, snapshot: Mapping[str, object]) -> Mapping[str, object]: ...

    def snapshots(
        self, kind: str | None = None
    ) -> tuple[Mapping[str, object], ...]: ...


class ExactRevisionModelSecurity:
    """Persist and enforce immutable-identity Hub and cache security evidence."""

    def __init__(
        self, intelligence: ModelIntelligence, state: SecurityEvidenceStore
    ) -> None:
        self._intelligence = intelligence
        self._state = state

    def inspect(
        self,
        repository: str,
        revision: str,
        *,
        runtimes: tuple[RuntimeObservation, ...] = (),
    ) -> Mapping[str, object]:
        try:
            report = self._intelligence.inspect(repository, revision, runtimes=runtimes)
        except Exception as error:
            raise ModelSecurityPolicyError(
                "required exact-revision model security evidence is unavailable"
            ) from error
        if (
            report.identity.repo_id != repository
            or report.identity.commit_sha.casefold() != revision.casefold()
        ):
            raise ModelSecurityPolicyError(
                "model security evidence does not match the exact requested revision"
            )
        assessment = _security_assessment(report)
        self._persist(assessment)
        _require_security_allowed(assessment, require_integrity=False)
        return assessment

    def record_verification(
        self,
        assessment: Mapping[str, object],
        verification: VerificationResult,
    ) -> Mapping[str, object]:
        updated = _assessment_with_verification(assessment, verification)
        self._persist(updated)
        _require_security_allowed(updated, require_integrity=True)
        return updated

    def record_cached_verification(
        self,
        repository: str,
        revision: str,
        verification: VerificationResult,
    ) -> Mapping[str, object]:
        return self.record_verification(
            self.require(repository, revision), verification
        )

    def require(self, repository: str, revision: str) -> Mapping[str, object]:
        assessment = next(
            (
                item
                for item in reversed(self._state.snapshots("model_security"))
                if item.get("repository") == repository
                and str(item.get("revision", "")).casefold() == revision.casefold()
            ),
            None,
        )
        if assessment is None:
            raise ModelSecurityPolicyError(
                "required exact-revision model security assessment is absent"
            )
        _require_security_allowed(assessment, require_integrity=True)
        return assessment

    @staticmethod
    def require_compatible(
        assessment: Mapping[str, object], runtime_installations: tuple[str, ...]
    ) -> None:
        compatibility = assessment.get("compatibility", ())
        if not isinstance(compatibility, (tuple, list)):
            raise ModelSecurityPolicyError("model compatibility evidence is invalid")
        by_runtime = {
            str(item.get("installation_id")): item
            for item in compatibility
            if isinstance(item, Mapping)
        }
        for installation_id in runtime_installations:
            item = by_runtime.get(installation_id)
            if item is not None and item.get("status") == "unsupported":
                raise ModelSecurityPolicyError(
                    f"model target is explicitly unsupported by Runtime Installation "
                    f"{installation_id!r}: {item.get('detail', 'no detail')}"
                )

    def _persist(self, assessment: Mapping[str, object]) -> None:
        payload = dict(assessment)
        fingerprint = hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        self._state.put_snapshot(
            {
                **payload,
                "kind": "model_security",
                "id": f"{payload['repository']}@{payload['revision']}",
                "version": fingerprint,
            }
        )


class RuntimeFilesystem(Protocol):
    """Remove one mlxctl-owned immutable runtime environment."""

    def remove(self, root: Path) -> None: ...


class LocalRuntimeFilesystem:
    """Filesystem implementation that does not invoke a shell."""

    def remove(self, root: Path) -> None:
        shutil.rmtree(root)


@dataclass(frozen=True, slots=True)
class CacheMovePlan:
    """A resumable copy-verify-publish cache move plan."""

    revision_id: str
    source: Path
    destination: Path
    bytes_to_copy: int
    steps: tuple[str, ...]
    cleanup_source: bool = False


class CacheMover(Protocol):
    """Plan and execute physical Cached Revision relocation."""

    def plan(self, revision: CachedRevision, destination: Path) -> CacheMovePlan: ...

    def execute(self, plan: CacheMovePlan) -> Path: ...


class VerifiedCacheMover:
    """Copy, content-verify, and atomically publish one cached snapshot."""

    def plan(self, revision: CachedRevision, destination: Path) -> CacheMovePlan:
        source = revision.snapshot_path.expanduser().resolve()
        target = destination.expanduser().resolve()
        if source == target or source in target.parents:
            raise SupplyPortError("cache move destination must be outside the source")
        return CacheMovePlan(
            revision_id=revision.revision_id,
            source=source,
            destination=target,
            bytes_to_copy=revision.size_on_disk,
            steps=(
                f"copy {source} to a staging directory",
                "verify every destination file against the source",
                f"atomically publish {target}",
                "offer confirmed source cleanup",
            ),
        )

    def execute(self, plan: CacheMovePlan) -> Path:
        if not plan.source.is_dir():
            raise FileNotFoundError(f"cached snapshot does not exist: {plan.source}")
        if plan.destination.exists():
            raise FileExistsError(
                f"cache move destination already exists: {plan.destination}"
            )
        plan.destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        token = hashlib.sha256(plan.revision_id.encode()).hexdigest()[:12]
        stage = plan.destination.with_name(
            f".{plan.destination.name}.mlxctl-staging-{token}"
        )
        shutil.copytree(plan.source, stage, symlinks=False, dirs_exist_ok=True)
        if _content_manifest(plan.source) != _content_manifest(stage):
            raise SupplyPortError("cache move verification failed")
        stage.replace(plan.destination)
        if plan.cleanup_source:
            shutil.rmtree(plan.source)
        return plan.destination


class RuntimeSupplyPort:
    """Apply Runtime Installation operations through RuntimeManager."""

    def __init__(
        self,
        manager: RuntimeManager,
        config_store: ConfigStore[MlxctlConfig],
        installation_root: Path,
        *,
        catalogue: RuntimeCatalogue | None = None,
        planner: RuntimeChangePlanner | None = None,
        filesystem: RuntimeFilesystem | None = None,
    ) -> None:
        self._manager = manager
        self._config_store = config_store
        self._installation_root = _prepare_runtime_root(installation_root)
        self._catalogue = catalogue or RuntimeCatalogue.load_builtin()
        self._planner = planner or RuntimeChangePlanner()
        self._filesystem = filesystem or LocalRuntimeFilesystem()

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        if operation == "runtime.install":
            return self._install(parameters)
        if operation == "runtime.adopt":
            return self._adopt(parameters)
        if operation == "runtime.update":
            return self._update(parameters)
        if operation == "runtime.rollback":
            return self._rollback(parameters)
        if operation == "runtime.remove":
            return self._remove(parameters)
        if operation == "runtime.prune":
            return self._prune(parameters)
        raise SupplyPortError(f"unsupported runtime operation: {operation}")

    def _install(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        runtime = _required_any(parameters, "runtime", "name")
        version = _optional(parameters, "version")
        channel = str(parameters.get("channel", "custom" if version else "tested"))
        if channel == "tested":
            bundle = self._tested_bundle(runtime, _optional(parameters, "bundle_id"))
            expected_version = _optional_any(
                parameters, "expected_version", "runtime_version", "version"
            )
            if expected_version is not None and expected_version != bundle.version:
                raise SupplyPortError(
                    f"tested bundle version {bundle.version!r} does not match "
                    f"expected version {expected_version!r}"
                )
            expected_digest = _optional_any(
                parameters, "expected_lock_digest", "lock_digest"
            )
            if expected_digest is not None and expected_digest != bundle.lock_sha256:
                raise SupplyPortError(
                    "tested bundle lock digest does not match the setup plan"
                )
            intended_id = bundle.bundle_id
            plan = _runtime_intent_plan(
                "install",
                intended_id,
                (
                    f"install exact tested bundle {bundle.bundle_id}",
                    "probe capabilities",
                    "publish desired state",
                ),
            )
            installation = self._manager.install_tested(
                bundle.bundle_id, self._installation_root
            )
            lock_sha256 = bundle.lock_sha256
        elif channel == "custom":
            if version is None:
                raise SupplyPortError("custom runtime installation requires version")
            python = str(parameters.get("python", "3.13"))
            intended_id = f"{runtime}-{version}-custom"
            plan = _runtime_intent_plan(
                "install",
                intended_id,
                (
                    f"install exact custom version {runtime} {version}",
                    "probe capabilities",
                    "publish desired state",
                ),
            )
            installation = self._manager.install_custom(
                runtime,
                version,
                python=python,
                installation_root=self._installation_root,
            )
            lock_sha256 = None
        else:
            raise SupplyPortError(f"unknown runtime installation channel: {channel}")
        self.record_managed_installation(installation)
        self.persist_runtime(self._config_store, installation)
        result = _runtime_result(installation, plan)
        result["lock_sha256"] = lock_sha256
        return result

    def _adopt(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        runtime = _required(parameters, "runtime")
        path = Path(_required(parameters, "path"))
        installation = self._manager.adopt_custom(runtime, path)
        plan = _runtime_intent_plan(
            "adopt",
            installation.installation_id,
            (
                f"probe external environment {installation.root}",
                "record exact launcher and capabilities",
                "register without taking filesystem ownership",
            ),
        )
        self.persist_runtime(self._config_store, installation)
        return _runtime_result(installation, plan)

    def _update(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        resource = _resource(parameters)
        config = self._config_store.load().value
        current = _runtime_installation(config.runtimes, resource)
        target_name = _optional(parameters, "target")
        version = _optional(parameters, "version")
        channel = str(
            parameters.get("channel", "custom" if version is not None else "tested")
        )
        if target_name:
            if version is not None or "channel" in parameters:
                raise SupplyPortError(
                    "runtime update target cannot be combined with channel or version"
                )
            target = _runtime_installation(config.runtimes, target_name)
        elif channel == "tested":
            if version is not None:
                raise SupplyPortError(
                    "tested runtime update does not accept a custom version"
                )
            bundle = self._tested_bundle(
                current.runtime, _optional(parameters, "bundle_id")
            )
            if bundle.bundle_id in config.runtimes:
                target = _runtime_installation(config.runtimes, bundle.bundle_id)
            else:
                target_result = self._install(
                    {
                        **dict(parameters),
                        "runtime": current.runtime,
                        "channel": "tested",
                    }
                )
                target = _runtime_installation(
                    self._config_store.load().value.runtimes,
                    str(target_result["installation_id"]),
                )
        elif channel == "custom":
            if version is None:
                raise SupplyPortError("custom runtime update requires an exact version")
            target_result = self._install(
                {
                    **dict(parameters),
                    "runtime": current.runtime,
                    "channel": "custom",
                }
            )
            target = _runtime_installation(
                self._config_store.load().value.runtimes,
                str(target_result["installation_id"]),
            )
        else:
            raise SupplyPortError(f"unknown runtime installation channel: {channel}")
        references = _runtime_references(config, resource)
        plan = self._planner.plan_update(
            current, target, referenced_services=references
        )
        _validate_runtime_service_options(config, resource, target)
        self._switch_runtime_references(resource, target.installation_id)
        return _runtime_result(target, plan)

    def _rollback(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        resource = _resource(parameters)
        target_name = _optional(parameters, "target")
        config = self._config_store.load().value
        current = _runtime_installation(config.runtimes, resource)
        if target_name is None:
            candidates = sorted(
                name
                for name, item in config.runtimes.items()
                if name != resource and item.definition == current.runtime
            )
            if not candidates:
                raise SupplyPortError(
                    f"no retained rollback installation for {resource!r}"
                )
            target_name = candidates[-1]
        target = _runtime_installation(config.runtimes, target_name)
        references = _runtime_references(config, resource)
        plan = self._planner.plan_rollback(
            current, target, referenced_services=references
        )
        _validate_runtime_service_options(config, resource, target)
        self._switch_runtime_references(resource, target.installation_id)
        return _runtime_result(target, plan)

    def _remove(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        resource = _resource(parameters)
        config = self._config_store.load().value
        installation = _runtime_installation(config.runtimes, resource)
        plan = self._planner.plan_remove(
            installation,
            referenced_services=_runtime_references(config, resource),
        )
        if not plan.allowed:
            raise SupplyPortError(
                f"runtime installation {resource!r} is referenced by "
                + ", ".join(plan.referenced_services)
            )
        _require_confirmed(parameters, "runtime removal")
        if installation.provenance != "adopted":
            self._validate_managed_installation(installation)
            self._filesystem.remove(installation.root)
        self._remove_runtime_record(resource)
        return {
            "installation_id": resource,
            "removed_environment": installation.provenance != "adopted",
            "plan": _plain_plan(plan),
        }

    def _prune(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        config = self._config_store.load().value
        retain = parameters.get("retain", 2)
        if type(retain) is not int or retain < 0:
            raise SupplyPortError("runtime prune retain must be a nonnegative integer")
        protected = {
            service.runtime_installation for service in config.services.values()
        }
        by_definition: dict[str, list[str]] = {}
        for name, installation in config.runtimes.items():
            by_definition.setdefault(installation.definition, []).append(name)
        for names in by_definition.values():
            protected.update(names[-retain:] if retain else ())
        candidates = [
            _runtime_installation(config.runtimes, name)
            for name in sorted(config.runtimes)
            if name not in protected
        ]
        plans = tuple(self._planner.plan_remove(item) for item in candidates)
        if candidates:
            _require_confirmed(parameters, "runtime pruning")
        for installation in candidates:
            if installation.provenance != "adopted":
                self._validate_managed_installation(installation)
        for installation in candidates:
            if installation.provenance != "adopted":
                self._filesystem.remove(installation.root)
            self._remove_runtime_record(installation.installation_id)
        return {
            "removed": [item.installation_id for item in candidates],
            "plans": [_plain_plan(item) for item in plans],
        }

    def record_managed_installation(self, installation: RuntimeInstallation) -> None:
        """Create independent evidence before registering a managed environment."""

        root = _validate_runtime_directory(
            installation.root,
            self._installation_root,
            installation.installation_id,
        )
        marker = root / ".mlxctl-runtime-owner.json"
        payload = json.dumps(
            {
                "installation_id": installation.installation_id,
                "owner": "mlxctl",
                "root": str(root),
                "schema_version": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        descriptor = os.open(
            marker,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise SupplyPortError("runtime ownership marker is not a private file")
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
            os.fsync(descriptor)
        except Exception:
            os.close(descriptor)
            marker.unlink(missing_ok=True)
            raise
        else:
            os.close(descriptor)

    def _validate_managed_installation(self, installation: RuntimeInstallation) -> None:
        root = _validate_runtime_directory(
            installation.root,
            self._installation_root,
            installation.installation_id,
        )
        marker = root / ".mlxctl-runtime-owner.json"
        try:
            descriptor = os.open(
                marker,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as error:
            raise SupplyPortError(
                f"runtime ownership marker is missing or unsafe: {marker}"
            ) from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise SupplyPortError(
                    f"runtime ownership marker is not a private file: {marker}"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(16 * 1024 + 1)
        finally:
            os.close(descriptor)
        if len(raw) > 16 * 1024:
            raise SupplyPortError("runtime ownership marker exceeds the size limit")
        expected = {
            "installation_id": installation.installation_id,
            "owner": "mlxctl",
            "root": str(root),
            "schema_version": 1,
        }
        try:
            observed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SupplyPortError("runtime ownership marker is invalid") from error
        if observed != expected:
            raise SupplyPortError(
                "runtime ownership marker does not match the registry"
            )

    def _tested_bundle(self, runtime: str, bundle_id: str | None):
        choices = tuple(
            bundle
            for bundle in self._catalogue.tested_bundles
            if bundle.runtime == runtime
            and (bundle_id is None or bundle.bundle_id == bundle_id)
        )
        if not choices:
            qualifier = f" bundle {bundle_id!r}" if bundle_id else ""
            raise SupplyPortError(f"no tested {runtime!r}{qualifier} is available")
        return sorted(choices, key=lambda item: (item.version, item.bundle_id))[-1]

    def _switch_runtime_references(self, current: str, target: str) -> None:
        def mutation(document) -> None:
            for service in document.get("services", {}).values():
                if service.get("runtime") == current:
                    service["runtime"] = target

        _edit_config(self._config_store, mutation)

    def _remove_runtime_record(self, resource: str) -> None:
        _edit_config(
            self._config_store,
            lambda document: document["runtimes"].pop(resource),
        )

    @staticmethod
    def persist_runtime(
        config_store: ConfigStore[MlxctlConfig], installation: RuntimeInstallation
    ) -> None:
        """Persist every exact field observed by RuntimeManager."""

        def mutation(document) -> None:
            runtimes = document.setdefault("runtimes", tomlkit.table())
            table = tomlkit.table()
            table["definition"] = installation.runtime
            table["version"] = installation.version
            table["provenance"] = installation.provenance
            table["root"] = str(installation.root)
            table["launcher"] = list(installation.launcher)
            table["capabilities"] = sorted(installation.capabilities)
            if installation.bundle_id is not None:
                table["bundle_id"] = installation.bundle_id
            runtimes[installation.installation_id] = table

        _edit_config(config_store, mutation)


class ModelSupplyPort:
    """Apply Model Installation and shared-cache operations."""

    def __init__(
        self,
        supply: ModelSupply,
        config_store: ConfigStore[MlxctlConfig],
        security: ExactRevisionModelSecurity,
        *,
        cache_mover: CacheMover | None = None,
    ) -> None:
        self._supply = supply
        self._config_store = config_store
        self._security = security
        self._cache_mover = cache_mover or VerifiedCacheMover()

    def search(self, query: str, *, mode: str = "curated", limit: int = 20):
        return self._supply.search(query, mode=mode, limit=limit)

    def inventory(self):
        return self._supply.inventory()

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        if operation == "model.install":
            return self._install(parameters)
        if operation == "model.repair":
            return self._repair(parameters)
        if operation == "model.update":
            return self._update(parameters)
        if operation == "model.rollback":
            return self._rollback(parameters)
        if operation == "model.cache.move":
            return self._move(parameters)
        if operation == "model.cache.evict":
            return self._evict(parameters)
        if operation == "model.cache.prune":
            return self._prune(parameters)
        raise SupplyPortError(f"unsupported model operation: {operation}")

    def _install(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        repository = _required(parameters, "repository")
        revision = str(parameters.get("revision", "main"))
        alias = str(
            parameters.get("alias") or repository.rstrip("/").rsplit("/", 1)[-1]
        )
        config = self._config_store.load().value
        runtimes = _model_runtime_observations(config, alias)
        result, security = self._install_exact(
            alias,
            repository,
            revision,
            offline=bool(parameters.get("offline", False)),
            runtimes=runtimes,
        )
        self._security.require_compatible(
            security, tuple(item.installation_id for item in runtimes)
        )
        installation_name = str(
            parameters.get("installation")
            or f"{alias}-{result.revision.commit_sha[:12]}"
        )
        self._persist_model(result, installation_name, alias)
        payload = _model_result(result, installation_name)
        payload["security"] = dict(security)
        return payload

    def _repair(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        config = self._config_store.load().value
        installation_name, alias = _resolve_model(config, _resource(parameters))
        installation = self._supplied_installation(installation_name)
        runtimes = _model_runtime_observations(config, alias)
        assessment = self._security.inspect(
            installation.revision.repo_id,
            installation.revision.commit_sha,
            runtimes=runtimes,
        )
        verification = self._supply.repair(installation)
        security = self._security.record_verification(assessment, verification)
        self._security.require_compatible(
            security, tuple(item.installation_id for item in runtimes)
        )
        return {
            "installation_name": installation.installation_id,
            "verification": asdict(verification),
            "security": dict(security),
        }

    def _update(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        resource = _resource(parameters)
        config = self._config_store.load().value
        installation_name, alias = _resolve_model(config, resource)
        current = config.models[installation_name]
        runtimes = _model_runtime_observations(config, alias)
        result, security = self._install_exact(
            alias,
            current.revision.repository,
            _required(parameters, "revision"),
            offline=bool(parameters.get("offline", False)),
            runtimes=runtimes,
        )
        self._security.require_compatible(
            security, tuple(item.installation_id for item in runtimes)
        )
        target_name = str(
            parameters.get("installation")
            or f"{alias}-{result.revision.commit_sha[:12]}"
        )
        self._persist_model(result, target_name, alias)
        payload = _model_result(result, target_name)
        payload["security"] = dict(security)
        payload["previous_installation"] = installation_name
        payload["plan"] = {
            "operation": "update",
            "steps": [
                f"install and verify {target_name}",
                f"repoint Model Alias {alias} to {target_name}",
                f"retain {installation_name} for rollback",
            ],
        }
        return payload

    def _install_exact(
        self,
        alias: str,
        repository: str,
        revision: str,
        *,
        offline: bool,
        runtimes: tuple[RuntimeObservation, ...],
    ) -> tuple[ModelInstallResult, Mapping[str, object]]:
        resolved = self._supply.resolve(repository, revision, offline=offline)
        if offline:
            assessment = self._security.require(repository, resolved.commit_sha)
        else:
            assessment = self._security.inspect(
                repository, resolved.commit_sha, runtimes=runtimes
            )
        result = self._supply.install(
            alias=alias,
            repo_id=repository,
            revision=resolved.commit_sha,
            offline=offline,
        )
        if (
            result.revision.repo_id != repository
            or result.revision.commit_sha.casefold() != resolved.commit_sha.casefold()
        ):
            raise ModelSecurityPolicyError(
                "installed model identity differs from its security assessment"
            )
        return result, self._security.record_verification(
            assessment, result.verification
        )

    def _rollback(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        _require_confirmed(parameters, "model rollback")
        resource = _resource(parameters)
        target = _required(parameters, "target")
        config = self._config_store.load().value
        current, alias = _resolve_model(config, resource)
        if target not in config.models:
            raise SupplyPortError(f"unknown Model Installation: {target!r}")
        if (
            config.models[target].revision.repository
            != config.models[current].revision.repository
        ):
            raise SupplyPortError("model rollback target must have the same repository")
        runtimes = _model_runtime_observations(config, alias)
        supplied_target = self._supplied_installation(target)
        assessment = self._security.inspect(
            supplied_target.revision.repo_id,
            supplied_target.revision.commit_sha,
            runtimes=runtimes,
        )
        assessment = self._security.record_verification(
            assessment, self._supply.verify(supplied_target)
        )
        self._security.require_compatible(
            assessment, tuple(item.installation_id for item in runtimes)
        )

        def mutation(document) -> None:
            document["aliases"][alias]["installation"] = target

        _edit_config(self._config_store, mutation)
        return {
            "alias": alias,
            "installation_name": target,
            "previous_installation": current,
            "plan": {
                "operation": "rollback",
                "steps": [
                    f"repoint Model Alias {alias} to {target}",
                    f"retain {current} until rollback is verified",
                ],
            },
        }

    def _move(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        revision = self._cached_revision(_resource(parameters))
        destination = Path(_required(parameters, "destination"))
        plan = self._cache_mover.plan(revision, destination)
        cleanup = bool(parameters.get("cleanup_source", False))
        if cleanup:
            _require_confirmed(parameters, "cache source cleanup")
        plan = replace(plan, cleanup_source=cleanup)
        published = self._cache_mover.execute(plan)
        return {"path": str(published), "plan": _plain_cache_plan(plan)}

    def _evict(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        revision = self._cached_revision(_resource(parameters))
        installations = self._supplied_installations()
        plan = self._supply.plan_cache_deletion(
            (revision.commit_sha,), installations=installations
        )
        if not plan.allowed:
            raise SupplyPortError(
                "Cached Revision is referenced by Model Installations: "
                + ", ".join(plan.blocked_by)
            )
        _require_confirmed(parameters, "cache eviction")
        plan.execute(approved=True)
        return {"plan": _plain_deletion_plan(plan)}

    def _prune(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        inventory = self._supply.inventory()
        protected = {
            item.revision.commit_sha for item in self._supplied_installations()
        }
        hashes = tuple(
            item.commit_sha
            for item in inventory.revisions
            if item.commit_sha not in protected
        )
        if not hashes:
            return {
                "plan": {
                    "allowed": True,
                    "revision_hashes": [],
                    "blocked_by": [],
                    "expected_freed_size": 0,
                }
            }
        plan = self._supply.plan_cache_deletion(hashes, installations=())
        _require_confirmed(parameters, "cache pruning")
        plan.execute(approved=True)
        return {"plan": _plain_deletion_plan(plan)}

    def _persist_model(
        self, result: ModelInstallResult, installation_name: str, alias: str
    ) -> None:
        def mutation(document) -> None:
            models = document.setdefault("models", tomlkit.table())
            model = tomlkit.table()
            model["repository"] = result.revision.repo_id
            model["revision"] = result.revision.commit_sha
            models[installation_name] = model
            aliases = document.setdefault("aliases", tomlkit.table())
            alias_table = tomlkit.table()
            alias_table["installation"] = installation_name
            aliases[alias] = alias_table

        _edit_config(self._config_store, mutation)

    def _supplied_installation(self, resource: str) -> SuppliedModelInstallation:
        config = self._config_store.load().value
        installation_name, _alias = _resolve_model(config, resource)
        desired = config.models[installation_name]
        cached = next(
            (
                item
                for item in self._supply.inventory().revisions
                if item.repo_id == desired.revision.repository
                and item.commit_sha == desired.revision.revision
            ),
            None,
        )
        snapshot = cached.snapshot_path if cached else Path("/")
        revision = SuppliedModelRevision(
            desired.revision.repository,
            desired.revision.revision,
            desired.revision.revision,
            "desired-state",
        )
        return SuppliedModelInstallation(
            installation_name,
            revision,
            revision.revision_id,
            snapshot,
            ModelProvenance(
                desired.revision.revision,
                desired.revision.revision,
                "desired-state",
            ),
        )

    def _supplied_installations(self) -> tuple[SuppliedModelInstallation, ...]:
        config = self._config_store.load().value
        return tuple(
            self._supplied_installation(name) for name in sorted(config.models)
        )

    def _cached_revision(self, resource: str) -> CachedRevision:
        choices = {}
        for item in self._supply.inventory().revisions:
            choices[item.revision_id] = item
            choices[item.commit_sha] = item
        try:
            return choices[resource]
        except KeyError as error:
            raise SupplyPortError(f"unknown Cached Revision: {resource!r}") from error


def _runtime_installation(
    runtimes: Mapping[str, ConfiguredRuntime], resource: str
) -> RuntimeInstallation:
    try:
        item = runtimes[resource]
    except KeyError as error:
        raise SupplyPortError(f"unknown Runtime Installation: {resource!r}") from error
    return RuntimeInstallation(
        item.installation_id,
        item.definition,
        item.version,
        item.provenance,
        Path(item.root),
        item.launcher,
        item.capabilities,
        item.bundle_id,
    )


def _runtime_references(config: MlxctlConfig, resource: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            name
            for name, service in config.services.items()
            if service.runtime_installation == resource
        )
    )


def _validate_runtime_service_options(
    config: MlxctlConfig, current: str, target: RuntimeInstallation
) -> None:
    for service_name, service in config.services.items():
        if service.runtime_installation != current:
            continue
        required = frozenset({"model", "host", "port", *service.options})
        missing = sorted(required - target.capabilities)
        if missing:
            raise SupplyPortError(
                f"runtime target {target.installation_id!r} cannot serve "
                f"{service_name!r}; missing exact capabilities: {', '.join(missing)}"
            )


def _resolve_model(config: MlxctlConfig, resource: str) -> tuple[str, str]:
    if resource in config.aliases:
        return config.aliases[resource].installation_name, resource
    if resource not in config.models:
        raise SupplyPortError(f"unknown Model Installation or Alias: {resource!r}")
    aliases = sorted(
        name
        for name, alias in config.aliases.items()
        if alias.installation_name == resource
    )
    return resource, aliases[0] if aliases else resource


def _model_runtime_observations(
    config: MlxctlConfig, alias: str
) -> tuple[RuntimeObservation, ...]:
    installation_ids = sorted(
        {
            service.runtime_installation
            for service in config.services.values()
            if str(service.model_alias) == alias
        }
    )
    return tuple(
        RuntimeObservation(
            installation_id=installation_id,
            runtime=config.runtimes[installation_id].definition,
            version=config.runtimes[installation_id].version,
            recognized_model_types=frozenset(),
            capabilities=config.runtimes[installation_id].capabilities,
            source="configured installation with probed option capabilities",
        )
        for installation_id in installation_ids
    )


def _runtime_intent_plan(
    operation: str, target: str, steps: tuple[str, ...]
) -> RuntimeChangePlan:
    return RuntimeChangePlan(operation, True, target, target, (), steps)


def _runtime_result(
    installation: RuntimeInstallation, plan: RuntimeChangePlan
) -> dict[str, object]:
    return {
        "installation_id": installation.installation_id,
        "runtime": installation.runtime,
        "version": installation.version,
        "provenance": installation.provenance,
        "root": str(installation.root),
        "launcher": list(installation.launcher),
        "capabilities": sorted(installation.capabilities),
        "bundle_id": installation.bundle_id,
        "plan": _plain_plan(plan),
    }


def _model_result(
    result: ModelInstallResult, installation_name: str
) -> dict[str, object]:
    return {
        "installation_id": installation_name,
        "installation_name": installation_name,
        "alias": result.alias.name,
        "repository": result.revision.repo_id,
        "requested_revision": result.revision.requested_revision,
        "revision": result.revision.commit_sha,
        "snapshot_path": str(result.cached.snapshot_path),
        "verification": asdict(result.verification),
        "plan": {
            "operation": "install",
            "steps": [
                f"resolve {result.revision.repo_id} to {result.revision.commit_sha}",
                "download and verify exact Cached Revision",
                f"persist Model Installation {installation_name}",
                f"point Model Alias {result.alias.name} to {installation_name}",
            ],
        },
    }


def _plain_plan(plan: RuntimeChangePlan) -> dict[str, object]:
    return {
        "operation": plan.operation,
        "allowed": plan.allowed,
        "current_installation": plan.current_installation,
        "target_installation": plan.target_installation,
        "referenced_services": list(plan.referenced_services),
        "steps": list(plan.steps),
    }


def _plain_cache_plan(plan: CacheMovePlan) -> dict[str, object]:
    return {
        "revision_id": plan.revision_id,
        "source": str(plan.source),
        "destination": str(plan.destination),
        "bytes_to_copy": plan.bytes_to_copy,
        "steps": list(plan.steps),
        "cleanup_source": plan.cleanup_source,
    }


def _plain_deletion_plan(plan) -> dict[str, object]:
    return {
        "allowed": plan.allowed,
        "revision_hashes": list(plan.revision_hashes),
        "blocked_by": list(plan.blocked_by),
        "expected_freed_size": plan.expected_freed_size,
    }


def _resource(parameters: Mapping[str, object]) -> str:
    return _required(parameters, "resource")


def _required(parameters: Mapping[str, object], name: str) -> str:
    value = parameters.get(name)
    if not isinstance(value, str) or not value:
        raise SupplyPortError(f"{name} is required")
    return value


def _required_any(parameters: Mapping[str, object], *names: str) -> str:
    value = _optional_any(parameters, *names)
    if value is None:
        raise SupplyPortError(f"{names[0]} is required")
    return value


def _optional(parameters: Mapping[str, object], name: str) -> str | None:
    value = parameters.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise SupplyPortError(f"{name} must be a nonempty string")
    return value


def _optional_any(parameters: Mapping[str, object], *names: str) -> str | None:
    for name in names:
        if name in parameters:
            return _optional(parameters, name)
    return None


def _require_confirmed(parameters: Mapping[str, object], operation: str) -> None:
    if parameters.get("confirmed") is not True:
        raise PermissionError(f"{operation} requires explicit confirmation")


def _edit_config(config_store: ConfigStore[MlxctlConfig], mutation) -> None:
    if not config_store.exists:
        config_store.import_text("schema_version = 1\n")
    config_store.edit(mutation)


def _content_manifest(root: Path) -> tuple[tuple[str, int, str], ...]:
    records = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        records.append(
            (str(path.relative_to(root)), path.stat().st_size, digest.hexdigest())
        )
    return tuple(records)


def _security_assessment(report: ModelIntelligenceReport) -> dict[str, object]:
    signals = [
        {
            "name": signal.name,
            "severity": signal.severity,
            "state": str(signal.state),
            "source": signal.source,
            "detail": signal.detail,
        }
        for signal in report.trust_signals
    ]
    scan = next(
        (item for item in report.trust_signals if item.name == "hub_security_scan"),
        None,
    )
    blockers: list[str] = []
    if scan is None or scan.severity == "unknown":
        blockers.append("security_scan_unavailable")
    elif scan.severity == "danger":
        blockers.append("known_security_finding")
    if any(item.name == "unsafe_serialization" for item in report.trust_signals):
        blockers.append("unsafe_serialization")
    overridable = []
    if any(item.name == "repository_code" for item in report.trust_signals):
        overridable.append("repository_code")
    if any(item.name == "remote_code_mapping" for item in report.trust_signals):
        overridable.append("remote_code")
    return {
        "policy_version": 1,
        "repository": report.identity.repo_id,
        "revision": report.identity.commit_sha,
        "hard_blockers": blockers,
        "overridable_risks": overridable,
        "signals": signals,
        "compatibility": [
            {
                "installation_id": item.installation_id,
                "runtime": item.runtime,
                "version": item.version,
                "status": item.status,
                "source": item.source,
                "detail": item.detail,
            }
            for item in getattr(report, "compatibility", ())
        ],
        "verification": {
            "status": "pending",
            "evidence": "not-yet-verified",
            "issues": [],
        },
    }


def _assessment_with_verification(
    assessment: Mapping[str, object], verification: VerificationResult
) -> dict[str, object]:
    blockers = [str(item) for item in assessment.get("hard_blockers", ())]
    if verification.status not in {"complete", "verified"} or verification.issues:
        blockers.append("integrity_mismatch")
    return {
        key: value
        for key, value in assessment.items()
        if key not in {"id", "kind", "version"}
    } | {
        "hard_blockers": list(dict.fromkeys(blockers)),
        "verification": {
            "status": verification.status,
            "evidence": verification.evidence,
            "issues": list(verification.issues),
        },
    }


def _require_security_allowed(
    assessment: Mapping[str, object], *, require_integrity: bool
) -> None:
    if assessment.get("policy_version") != 1:
        raise ModelSecurityPolicyError("model security policy evidence is unsupported")
    blockers = assessment.get("hard_blockers")
    if not isinstance(blockers, (tuple, list)) or not all(
        isinstance(item, str) for item in blockers
    ):
        raise ModelSecurityPolicyError("model security policy evidence is invalid")
    if blockers:
        raise ModelSecurityPolicyError(
            "model revision is blocked by security policy: "
            + ", ".join(sorted(blockers))
        )
    verification = assessment.get("verification")
    if require_integrity and (
        not isinstance(verification, Mapping)
        or verification.get("status") not in {"complete", "verified"}
        or verification.get("issues") not in ((), [])
    ):
        raise ModelSecurityPolicyError(
            "required exact-revision cache integrity evidence is absent"
        )


def _prepare_runtime_root(path: Path) -> Path:
    expanded = path.expanduser()
    expanded.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = expanded.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SupplyPortError(f"runtime root is not a managed directory: {expanded}")
    if metadata.st_uid != os.getuid():
        raise SupplyPortError(f"runtime root is not user-owned: {expanded}")
    os.chmod(expanded, 0o700, follow_symlinks=False)
    return expanded.resolve(strict=True)


def _validate_runtime_directory(
    path: Path, installation_root: Path, installation_id: str
) -> Path:
    candidate = path.expanduser()
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise SupplyPortError(
            f"managed runtime directory is missing: {path}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SupplyPortError(f"managed runtime is not a directory: {path}")
    if metadata.st_uid != os.getuid():
        raise SupplyPortError(f"managed runtime is not user-owned: {path}")
    if resolved.parent != installation_root or resolved.name != installation_id:
        raise SupplyPortError(
            "managed runtime must be an exact direct managed child matching its "
            "installation identity"
        )
    return resolved
