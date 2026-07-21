"""Production orchestration for authenticated Vite-owned Codex reconciliation."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

from mastic.application.application_upgrade_policy import (
    UpgradePolicyAssessmentDisposition,
    assess_unattended_upgrade,
    build_upgrade_candidate,
)
from mastic.application.current_release import resolve_current_release
from mastic.application.dispatch import ApplicationError
from mastic.application.external_application_lifecycle import (
    ArtifactCleanupOutcome,
    ArtifactClosureReleaser,
    MutationOutcome,
    OwnerUpgradeCommandError,
    PlanFollowUp,
    apply_owner_upgrade,
    build_owner_upgrade_preview,
)
from mastic.application.owner_reconciliation import (
    authorize_owner_reconciliation,
    classify_release_transition,
    trusted_owner_reconciliation_policy,
)
from mastic.application.serialization import to_plain_data
from mastic.domain.application_lifecycle import (
    ReleaseTransitionKind,
    UnattendedUpgradePolicy,
)
from mastic.domain.external_applications import (
    CurrentReleaseResolution,
    ExternalApplicationInstallation,
    InstallationObservation,
    ReleaseIntent,
)
from mastic.infrastructure.codex_artifact_closure import (
    CodexViteArtifactClosureVerifier,
    NpmCodexArtifactClosureMaterializer,
)
from mastic.infrastructure.codex_npm_authority import (
    NpmCodexArtifactMaterializer,
    NpmCodexReleaseAuthority,
)
from mastic.infrastructure.codex_vite_discovery import (
    CodexViteDiscovery,
    CodexViteDiscoveryError,
    CommandResult,
)
from mastic.infrastructure.codex_vite_lifecycle import (
    CodexViteOwnerLifecycle,
    SubprocessExactCommandRunner,
)
from mastic.infrastructure.owner_reconciliation_store import (
    OwnerReconciliationStore,
    PreparedOwnerReconciliation,
    PreparedOwnerReconciliationError,
)
from mastic.infrastructure.owner_command_tracker import (
    DurableOwnerCommandTracker,
    OwnerCommandTracker,
)
from mastic.infrastructure.planning_owner_upgrade_authorization import (
    PlanningRecordOwnerUpgradeAuthorizationVerifier,
    StaticPlanningPolicyRegistry,
)
from mastic.infrastructure.planning_record_authority import LocalGrantReceiptIssuer
from mastic.infrastructure.planning_record_repository import PlanningRecordRepository
from mastic.infrastructure.state_store import OperationalStateStore


CODEX_SELECTION = ExternalApplicationInstallation(
    application_identity="external-application:codex",
    installation_identity="application-installation:codex:vite",
    owner_identity="vite-plus/npm-global",
    release_intent=ReleaseIntent.current(channel="npm:latest"),
    platform="darwin",
    architecture="arm64",
)
_VITE_OWNERS = frozenset({"vite-plus/npm-global", "vite-plus/global-package"})
_RESOLUTION_MAXIMUM_AGE = timedelta(minutes=10)
_ASSESSMENT_MAXIMUM_AGE = timedelta(minutes=15)
_DISCOVERY_OUTPUT_MAX_BYTES = 1024 * 1024


def _selection(owner_identity: str) -> ExternalApplicationInstallation:
    if owner_identity not in _VITE_OWNERS:
        raise ApplicationError(
            "owner_mismatch", "The detected Codex owner is not supported."
        )
    return ExternalApplicationInstallation(
        application_identity=CODEX_SELECTION.application_identity,
        installation_identity=CODEX_SELECTION.installation_identity,
        owner_identity=owner_identity,
        release_intent=CODEX_SELECTION.release_intent,
        platform=CODEX_SELECTION.platform,
        architecture=CODEX_SELECTION.architecture,
    )


class OperationOwner(Protocol):
    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]: ...


class LegacyApplicationSupply(Protocol):
    def inventory(self): ...

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]: ...


class _CurrentResolver:
    def __init__(
        self,
        authority: NpmCodexReleaseAuthority,
        materializer: NpmCodexArtifactMaterializer,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._authority = authority
        self._materializer = materializer
        self._clock = clock

    def resolve(
        self,
        selected: ExternalApplicationInstallation,
        observed: InstallationObservation,
    ) -> CurrentReleaseResolution:
        return resolve_current_release(
            selected,
            observed,
            authority=self._authority,
            materializer=self._materializer,
            maximum_age=_RESOLUTION_MAXIMUM_AGE,
            resolver_policy_identity="npm-current-stable-materialization:v1",
            validation_profile_identity="codex-npm-archive-integrity:v1",
            clock=self._clock,
        )


class SubprocessDiscoveryRunner:
    """Run bounded discovery commands with a small non-secret environment."""

    def __init__(
        self,
        environment: Mapping[str, str],
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._environment = {
            key: value
            for key, value in environment.items()
            if key in {"HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR"}
        }
        self._timeout = timeout_seconds

    def run(self, argv: Sequence[str]) -> CommandResult:
        try:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                result = subprocess.run(
                    tuple(argv),
                    check=False,
                    stdout=stdout,
                    stderr=stderr,
                    shell=False,
                    env=self._environment,
                    timeout=self._timeout,
                )
                rendered = tuple(
                    self._bounded_output(stream) for stream in (stdout, stderr)
                )
        except (OSError, subprocess.SubprocessError) as error:
            raise CodexViteDiscoveryError("owner_command_unavailable") from error
        return CommandResult(result.returncode, rendered[0], rendered[1])

    @staticmethod
    def _bounded_output(stream) -> str:
        stream.seek(0, os.SEEK_END)
        if stream.tell() > _DISCOVERY_OUTPUT_MAX_BYTES:
            raise CodexViteDiscoveryError("owner_command_output_exceeded")
        stream.seek(0)
        return stream.read().decode("utf-8", errors="replace")


class LocalCodexOwnerReconciliation:
    """Prepare locally, mint local-user authority, and delegate exact application."""

    def __init__(
        self,
        *,
        discovery: CodexViteDiscovery,
        current: _CurrentResolver,
        closure_materializer: NpmCodexArtifactClosureMaterializer,
        lifecycle: CodexViteOwnerLifecycle,
        store: OwnerReconciliationStore,
        planning: PlanningRecordRepository,
        issuer: LocalGrantReceiptIssuer,
        remote: OperationOwner,
        uid: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._discovery = discovery
        self._current = current
        self._closure_materializer = closure_materializer
        self._lifecycle = lifecycle
        self._store = store
        self._planning = planning
        self._issuer = issuer
        self._remote = remote
        self._uid = uid
        self._clock = clock

    def preview(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        self._require_codex(operation, parameters)
        if operation != "application.upgrade":
            raise ApplicationError("operation_unavailable", operation)
        supplied = parameters.get("preview_fingerprint")
        if supplied is None:
            prepared = self._prepare()
            identity = self._store.put(prepared)
        elif isinstance(supplied, str):
            prepared = self._load(supplied)
            identity = prepared.identity
        else:
            raise ApplicationError("stale_preview", "invalid prepared preview identity")
        return {
            "application": "codex",
            "state": "upgrade_prepared",
            "installed_version": prepared.observation.installed_release,
            "target_version": prepared.resolution.exact_release,
            "release_channel": prepared.selected.release_intent.channel,
            "owner": prepared.selected.owner_identity,
            "owner_action": prepared.action.action_kind,
            "owner_preview_fingerprint": prepared.preview.fingerprint,
            "prepared_reconciliation_identity": identity,
            "preview_fingerprint": identity,
        }

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        self._require_codex(operation, parameters)
        if operation == "application.inspect":
            return self.inspect()
        if operation != "application.upgrade":
            raise ApplicationError("operation_unavailable", operation)
        if parameters.get("confirmed") is not True:
            raise ApplicationError(
                "confirmation_required", "application upgrade requires confirmation"
            )
        prepared_identity = parameters.get("preview_fingerprint")
        if not isinstance(prepared_identity, str):
            raise ApplicationError(
                "stale_preview",
                "application upgrade requires its exact prepared preview",
            )
        prepared = self._load(prepared_identity)
        if prepared.authorization is not None:
            raise ApplicationError(
                "stale_preview", "application upgrade preview was already authorized"
            )
        authorization, _trusted = authorize_owner_reconciliation(
            prepared.selected,
            prepared.preview,
            prepared.closure,
            repository=self._planning,
            issuer=self._issuer,
            uid=self._uid,
            clock=self._clock,
        )
        authorized = PreparedOwnerReconciliation(
            selected=prepared.selected,
            observation=prepared.observation,
            resolution=prepared.resolution,
            closure=prepared.closure,
            preview=prepared.preview,
            authorization=authorization,
        )
        authorized_identity = self._store.put(authorized)
        result = self._remote.execute(
            "application.upgrade",
            {
                "application": "codex",
                "prepared_reconciliation": authorized_identity,
                "operation_id": authorized_identity,
            },
        )
        return {
            **result,
            "application": "codex",
            "prepared_reconciliation_identity": authorized_identity,
        }

    def inspect(self) -> Mapping[str, object]:
        selected, observation, resolution = self._observe_current()
        transition = classify_release_transition(
            observation.installed_release or "", resolution.exact_release
        )
        return {
            "application": "codex",
            "installation": selected.installation_identity,
            "owner": observation.owner_identity,
            "release_channel": selected.release_intent.channel,
            "installed_version": observation.installed_release,
            "current_version": resolution.exact_release,
            "current_resolution_fingerprint": resolution.fingerprint,
            "current_resolution_expires_at": resolution.expires_at.isoformat(),
            "status": transition.value,
            "safe_owner_action_available": transition is ReleaseTransitionKind.UPGRADE,
            "next_actions": (
                ["mastic application upgrade codex"]
                if transition is ReleaseTransitionKind.UPGRADE
                else []
            ),
        }

    def _prepare(self) -> PreparedOwnerReconciliation:
        selected, observation, resolution = self._observe_current()
        transition = classify_release_transition(
            observation.installed_release or "", resolution.exact_release
        )
        if transition is ReleaseTransitionKind.SAME:
            raise ApplicationError("already_current", "Codex is already current")
        if transition is ReleaseTransitionKind.DOWNGRADE:
            raise ApplicationError(
                "downgrade_forbidden",
                "The selected Codex channel would downgrade the existing installation.",
            )
        if transition is not ReleaseTransitionKind.UPGRADE:
            raise ApplicationError(
                "release_transition_unknown",
                "The Codex release transition could not be classified safely.",
            )
        candidate = build_upgrade_candidate(
            selected, observation, resolution, transition=transition
        )
        policy = UnattendedUpgradePolicy(
            policy_identity="codex-current-owner-upgrade:v1",
            application_identity=selected.application_identity,
            owner_identity=selected.owner_identity,
            release_channel=selected.release_intent.channel,
            validation_profile_identity="codex-npm-archive-integrity:v1",
            data_bearing=False,
            maximum_backup_age=timedelta(hours=1),
        )
        assessment = assess_unattended_upgrade(policy, candidate, now=self._clock())
        if (
            assessment.disposition
            is not UpgradePolicyAssessmentDisposition.APPROVAL_REQUIRED
        ):
            raise ApplicationError(
                "upgrade_policy_blocked",
                "Codex owner upgrade policy did not admit this preparation.",
                details={"reason_codes": assessment.reason_codes},
            )
        closure = self._closure_materializer.materialize(resolution)
        try:
            action = self._lifecycle.preview_action(observation, closure)
            preview = build_owner_upgrade_preview(
                candidate, assessment, resolution, observation, closure, action
            )
            return PreparedOwnerReconciliation(
                selected=selected,
                observation=observation,
                resolution=resolution,
                closure=closure,
                preview=preview,
            )
        except Exception:
            self._closure_materializer.release(closure)
            raise

    def _observe_current(
        self,
    ) -> tuple[
        ExternalApplicationInstallation,
        InstallationObservation,
        CurrentReleaseResolution,
    ]:
        try:
            observation = self._discovery.discover(
                selected_installation_identity=CODEX_SELECTION.installation_identity,
                selected_release_channel=CODEX_SELECTION.release_intent.channel,
            )
            selected = _selection(observation.owner_identity)
            return selected, observation, self._current.resolve(selected, observation)
        except ApplicationError:
            raise
        except CodexViteDiscoveryError as error:
            raise ApplicationError(
                "codex_owner_unresolved",
                "A supported Vite-owned Codex installation could not be resolved.",
                details={"reason_code": error.reason_code},
            ) from error
        except Exception as error:
            reason = getattr(error, "reason_code", type(error).__name__)
            raise ApplicationError(
                "codex_current_unavailable",
                "Codex current-release evidence could not be resolved.",
                details={"reason_code": str(reason)},
            ) from error

    def _load(self, identity: str) -> PreparedOwnerReconciliation:
        try:
            prepared = self._store.load(identity)
        except PreparedOwnerReconciliationError as error:
            raise ApplicationError(
                "prepared_reconciliation_invalid",
                "The retained Codex reconciliation no longer validates.",
            ) from error
        if prepared is None:
            raise ApplicationError(
                "prepared_reconciliation_missing",
                "The retained Codex reconciliation is unavailable.",
            )
        return prepared

    @staticmethod
    def _require_codex(operation: str, parameters: Mapping[str, object]) -> None:
        if parameters.get("application") != "codex":
            raise ApplicationError(
                "invalid_parameter", f"{operation} supports only Codex"
            )


class DaemonCodexOwnerReconciliation:
    """Rehydrate verifier-only authority and apply one exact retained upgrade."""

    def __init__(
        self,
        *,
        discovery: CodexViteDiscovery,
        current: _CurrentResolver,
        lifecycle: CodexViteOwnerLifecycle,
        artifact_verifier: CodexViteArtifactClosureVerifier,
        store: OwnerReconciliationStore,
        planning: PlanningRecordRepository,
        owner_commands: OwnerCommandTracker,
        closure_releaser: ArtifactClosureReleaser,
        uid: int,
        transition: Callable[[str], AbstractContextManager[None]],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._discovery = discovery
        self._current = current
        self._lifecycle = lifecycle
        self._artifact_verifier = artifact_verifier
        self._store = store
        self._planning = planning
        self._owner_commands = owner_commands
        self._closure_releaser = closure_releaser
        self._uid = uid
        self._transition = transition
        self._clock = clock

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        if (
            operation != "application.upgrade"
            or parameters.get("application") != "codex"
        ):
            raise ApplicationError("operation_unavailable", operation)
        identity = parameters.get("prepared_reconciliation")
        if not isinstance(identity, str):
            raise ApplicationError(
                "invalid_parameter", "prepared reconciliation identity is required"
            )
        try:
            prepared = self._store.load(identity)
        except PreparedOwnerReconciliationError as error:
            raise ApplicationError(
                "prepared_reconciliation_invalid",
                "The retained Codex reconciliation no longer validates.",
            ) from error
        if prepared is None or prepared.authorization is None:
            raise ApplicationError(
                "prepared_reconciliation_missing",
                "An authorized retained Codex reconciliation is required.",
            )
        owner_command = self._owner_commands.inspect(
            prepared.action.argv, cwd=prepared.action.cwd
        )
        if owner_command.blocks_retry:
            return self._result(
                identity,
                mutation_outcome=MutationOutcome.NOT_ATTEMPTED.value,
                reason_code="owner_mutation_in_progress",
                owner_command_state=owner_command.state,
                target_version=prepared.preview.target_release,
                owner_mutation_attempted=False,
            )
        registry = StaticPlanningPolicyRegistry(
            (trusted_owner_reconciliation_policy(self._uid, prepared.selected),)
        )
        verifier = PlanningRecordOwnerUpgradeAuthorizationVerifier(
            self._planning,
            registry,
            self._lifecycle,
            maximum_assessment_age=_ASSESSMENT_MAXIMUM_AGE,
            clock=self._clock,
        )
        if self._already_converged(prepared, verifier):
            cleanup = self._release(prepared)
            return self._result(
                identity,
                mutation_outcome="verified",
                reason_code="already_converged",
                artifact_cleanup_outcome=cleanup.value,
                plan_follow_up=(
                    PlanFollowUp.NONE.value
                    if cleanup is ArtifactCleanupOutcome.VERIFIED
                    else PlanFollowUp.SUCCESSOR_REQUIRED.value
                ),
                target_version=prepared.preview.target_release,
                owner_mutation_attempted=False,
            )
        if (
            owner_command.requires_convergence_revalidation
            and not self._source_unchanged(prepared)
        ):
            return self._result(
                identity,
                mutation_outcome=MutationOutcome.UNKNOWN.value,
                reason_code="prior_owner_command_outcome_unknown",
                owner_command_state=owner_command.state,
                artifact_cleanup_outcome=ArtifactCleanupOutcome.REQUIRED.value,
                plan_follow_up=PlanFollowUp.SUCCESSOR_REQUIRED.value,
                target_version=prepared.preview.target_release,
                owner_mutation_attempted=True,
            )
        if self._clock() >= prepared.resolution.expires_at:
            cleanup = self._release(prepared)
            raise ApplicationError(
                "stale_resolution",
                "The retained Codex current-release resolution expired.",
                details={"artifact_cleanup_outcome": cleanup.value},
                next_actions=("prepare a new Codex upgrade",),
            )
        try:
            result = apply_owner_upgrade(
                prepared.selected,
                prepared.preview,
                prepared.authorization,
                prepared.closure,
                authorization_verifier=verifier,
                discovery=self._discovery,
                current_resolver=self._current,
                executor=self._lifecycle,
                artifact_releaser=_RetainClosure(),
                transition=self._transition,
                clock=self._clock,
            )
        except Exception as primary_error:
            recovered_convergence = False
            recovered_cleanup: ArtifactCleanupOutcome | None = None
            try:
                failed_command = self._owner_commands.inspect(
                    prepared.action.argv, cwd=prepared.action.cwd
                )
            except Exception as inspection_error:
                primary_error.add_note(
                    "owner command state could not be inspected after failure: "
                    f"{type(inspection_error).__name__}"
                )
            else:
                try:
                    terminal = not failed_command.blocks_retry and not (
                        failed_command.requires_convergence_revalidation
                    )
                    if failed_command.requires_convergence_revalidation:
                        recovered_convergence = self._already_converged(
                            prepared, verifier
                        )
                        terminal = recovered_convergence or self._source_unchanged(
                            prepared
                        )
                except Exception as revalidation_error:
                    primary_error.add_note(
                        "owner command outcome could not be revalidated after failure: "
                        f"{type(revalidation_error).__name__}"
                    )
                    terminal = False
                if terminal:
                    try:
                        cleanup = self._release(prepared)
                    except Exception as cleanup_error:
                        primary_error.add_note(
                            "artifact closure cleanup also failed: "
                            f"{type(cleanup_error).__name__}"
                        )
                    else:
                        recovered_cleanup = cleanup
                        if cleanup is ArtifactCleanupOutcome.REQUIRED:
                            primary_error.add_note(
                                "artifact closure cleanup also requires a successor"
                            )
            if recovered_convergence and recovered_cleanup is not None:
                return self._result(
                    identity,
                    mutation_outcome=MutationOutcome.VERIFIED.value,
                    reason_code="converged_after_owner_exception",
                    owner_command_state=failed_command.state,
                    artifact_cleanup_outcome=recovered_cleanup.value,
                    plan_follow_up=(
                        PlanFollowUp.NONE.value
                        if recovered_cleanup is ArtifactCleanupOutcome.VERIFIED
                        else PlanFollowUp.SUCCESSOR_REQUIRED.value
                    ),
                    target_version=prepared.preview.target_release,
                    owner_mutation_attempted=True,
                )
            raise
        if result.mutation_outcome is MutationOutcome.UNKNOWN:
            result = replace(
                result,
                plan_follow_up=PlanFollowUp.SUCCESSOR_REQUIRED,
                artifact_cleanup_outcome=ArtifactCleanupOutcome.REQUIRED,
            )
        else:
            cleanup = self._release(prepared)
            if cleanup is ArtifactCleanupOutcome.REQUIRED:
                result = replace(
                    result,
                    plan_follow_up=PlanFollowUp.SUCCESSOR_REQUIRED,
                    artifact_cleanup_outcome=cleanup,
                )
        plain_result = cast(Mapping[str, object], to_plain_data(result))
        return self._result(
            identity,
            **plain_result,
            target_version=prepared.preview.target_release,
            owner_mutation_attempted=(
                result.mutation_outcome is not MutationOutcome.NOT_ATTEMPTED
            ),
        )

    def _release(self, prepared: PreparedOwnerReconciliation) -> ArtifactCleanupOutcome:
        try:
            self._closure_releaser.release(prepared.closure)
        except OwnerUpgradeCommandError:
            return ArtifactCleanupOutcome.REQUIRED
        return ArtifactCleanupOutcome.VERIFIED

    def _source_unchanged(self, prepared: PreparedOwnerReconciliation) -> bool:
        try:
            observed = self._discovery.discover(
                selected_installation_identity=prepared.selected.installation_identity,
                selected_release_channel=prepared.selected.release_intent.channel,
            )
        except CodexViteDiscoveryError:
            return False
        return observed.state_fingerprint == prepared.observation.state_fingerprint

    def _already_converged(
        self,
        prepared: PreparedOwnerReconciliation,
        verifier: PlanningRecordOwnerUpgradeAuthorizationVerifier,
    ) -> bool:
        assert prepared.authorization is not None
        if not verifier.verify(
            prepared.selected,
            prepared.preview,
            prepared.authorization,
            prepared.closure,
        ):
            return False
        try:
            observed = self._discovery.discover(
                selected_installation_identity=prepared.selected.installation_identity,
                selected_release_channel=prepared.selected.release_intent.channel,
            )
            source = prepared.observation
            if observed.installed_release != prepared.preview.target_release or (
                observed.application_identity,
                observed.installation_identity,
                observed.owner_identity,
                observed.owner_runtime_identity,
                observed.release_channel,
                observed.platform,
                observed.architecture,
                observed.active_invocation,
                observed.reachable_invocations,
            ) != (
                source.application_identity,
                source.installation_identity,
                source.owner_identity,
                source.owner_runtime_identity,
                source.release_channel,
                source.platform,
                source.architecture,
                source.active_invocation,
                source.reachable_invocations,
            ):
                return False
            self._artifact_verifier.verify_staged(prepared.closure)
            self._artifact_verifier.verify_installed(prepared.closure, observed)
            return True
        except (CodexViteDiscoveryError, OwnerUpgradeCommandError):
            return False

    @staticmethod
    def _result(identity: str, **values: object) -> Mapping[str, object]:
        return {
            "application": "codex",
            "prepared_reconciliation_identity": identity,
            **values,
        }


class SetupApplicationReconciliation:
    """Keep setup on current Vite-owned Codex while delegating other supplies."""

    def __init__(
        self,
        legacy: LegacyApplicationSupply,
        codex: LocalCodexOwnerReconciliation,
    ) -> None:
        self._legacy = legacy
        self._codex = codex

    def inventory(self):
        return self._legacy.inventory()

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        if operation != "application.install":
            return self._legacy.execute(operation, parameters)
        raw_targets = parameters.get("application_targets", ())
        if not isinstance(raw_targets, Sequence) or isinstance(
            raw_targets, str | bytes
        ):
            raise ApplicationError(
                "invalid_parameter", "application targets are invalid"
            )
        targets = tuple(str(item) for item in raw_targets)
        if "codex" not in targets:
            return self._legacy.execute(operation, parameters)
        if parameters.get("offline") is True:
            raise ApplicationError(
                "current_release_requires_online_evidence",
                "Current Codex reconciliation requires fresh owner and npm evidence.",
            )
        inspection = self._codex.inspect()
        status = inspection.get("status")
        preserve_outdated = parameters.get("preserve_outdated_codex") is True
        if status == ReleaseTransitionKind.UPGRADE.value:
            if not preserve_outdated:
                raise ApplicationError(
                    "codex_upgrade_required",
                    "Review and confirm the exact current Codex owner upgrade before setup continues.",
                    details={
                        "installed_version": inspection.get("installed_version"),
                        "current_version": inspection.get("current_version"),
                        "owner": inspection.get("owner"),
                    },
                    next_actions=(
                        "mastic application upgrade codex",
                        "rerun mastic setup",
                        "rerun setup with --preserve-outdated-codex",
                    ),
                )
            result = {"preserved_outdated": True}
            version = inspection.get("installed_version")
        elif status == ReleaseTransitionKind.SAME.value:
            result = {}
            version = inspection.get("installed_version")
        else:
            raise ApplicationError(
                "codex_reconciliation_blocked",
                "Setup could not safely reconcile the selected Codex installation.",
                details={"status": status},
            )
        remaining = tuple(item for item in targets if item != "codex")
        legacy_result: Mapping[str, object] = {"applications": {}}
        if remaining:
            legacy_result = self._legacy.execute(
                operation, {**parameters, "application_targets": remaining}
            )
        applications = legacy_result.get("applications", {})
        combined = dict(applications) if isinstance(applications, Mapping) else {}
        combined["codex"] = {
            "version": version,
            "release_intent": (
                "exact" if result.get("preserved_outdated") is True else "current"
            ),
            "release_channel": CODEX_SELECTION.release_intent.channel,
            "owner": inspection.get("owner"),
            "provenance": (
                "explicitly-preserved"
                if result.get("preserved_outdated") is True
                else "observed"
            ),
            "preserved_outdated": result.get("preserved_outdated", False),
        }
        return {**legacy_result, "applications": combined}


class _RetainClosure:
    def release(self, _closure) -> None:
        """Retain exact material until durable operation closeout can be proven."""


def build_codex_dependencies(
    *,
    home: Path,
    stage_root: Path,
    state: OperationalStateStore,
    clock: Callable[[], datetime],
) -> tuple[
    CodexViteDiscovery,
    _CurrentResolver,
    NpmCodexArtifactClosureMaterializer,
    CodexViteArtifactClosureVerifier,
    CodexViteOwnerLifecycle,
    DurableOwnerCommandTracker,
]:
    """Compose trusted host and npm adapters without granting planning authority."""

    vp_home = home / ".vite-plus"
    base_environment = dict(os.environ)
    base_environment["HOME"] = str(home)
    discovery = CodexViteDiscovery(
        vp_home=vp_home,
        path=(
            vp_home / "bin",
            home / ".local/bin",
            Path("/usr/local/bin"),
            Path("/opt/homebrew/bin"),
        ),
        runner=SubprocessDiscoveryRunner(base_environment),
        observed_at=clock,
        platform="darwin",
        architecture="arm64",
    )
    current = _CurrentResolver(
        NpmCodexReleaseAuthority(), NpmCodexArtifactMaterializer(), clock=clock
    )
    closure_materializer = NpmCodexArtifactClosureMaterializer(stage_root=stage_root)
    owner_commands = DurableOwnerCommandTracker(
        state,
        installation_identity=CODEX_SELECTION.installation_identity,
    )
    artifact_verifier = CodexViteArtifactClosureVerifier(
        vp_home=vp_home,
        roots=discovery,
        runner=SubprocessExactCommandRunner(),
        base_environment=base_environment,
    )
    lifecycle = CodexViteOwnerLifecycle(
        vp_home=vp_home,
        discovery=discovery,
        artifact_verifier=artifact_verifier,
        runner=SubprocessExactCommandRunner(tracker=owner_commands),
        base_environment=base_environment,
    )
    return (
        discovery,
        current,
        closure_materializer,
        artifact_verifier,
        lifecycle,
        owner_commands,
    )
