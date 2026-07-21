"""Immutable durable preparation for exact owner-native reconciliation."""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping, cast

from mastic.application.external_application_lifecycle import (
    AuthorizedOwnerUpgrade,
    OwnerUpgradeAction,
    OwnerUpgradePreview,
    VerifiedArtifact,
    VerifiedArtifactClosure,
)
from mastic.domain.canonical import (
    canonical_fingerprint,
    canonical_timestamp,
    require_sha256,
)
from mastic.domain.external_applications import (
    CurrentReleaseResolution,
    ExternalApplicationInstallation,
    InstallationObservation,
    ReleaseIntent,
    ReleaseIntentKind,
)
from mastic.infrastructure.state_store import OperationalStateStore


_SNAPSHOT_KIND = "prepared_owner_reconciliation"
_RECORD_VERSION = 1


class PreparedOwnerReconciliationError(RuntimeError):
    """Durable preparation could not be trusted or rehydrated."""


@dataclass(frozen=True, slots=True)
class PreparedOwnerReconciliation:
    """Exact immutable material selected for one authorized owner upgrade."""

    selected: ExternalApplicationInstallation
    observation: InstallationObservation
    resolution: CurrentReleaseResolution
    closure: VerifiedArtifactClosure
    preview: OwnerUpgradePreview
    authorization: AuthorizedOwnerUpgrade | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.selected, ExternalApplicationInstallation):
            raise ValueError("prepared reconciliation selection is required")
        if not isinstance(self.observation, InstallationObservation):
            raise ValueError("prepared reconciliation observation is required")
        if not isinstance(self.resolution, CurrentReleaseResolution):
            raise ValueError("prepared reconciliation current resolution is required")
        if not isinstance(self.closure, VerifiedArtifactClosure):
            raise ValueError("prepared reconciliation artifact closure is required")
        if not isinstance(self.preview, OwnerUpgradePreview):
            raise ValueError("prepared reconciliation preview is required")
        if self.authorization is not None and not isinstance(
            self.authorization, AuthorizedOwnerUpgrade
        ):
            raise ValueError("prepared reconciliation authorization is invalid")

        selection = self.selected
        observation = self.observation
        resolution = self.resolution
        closure = self.closure
        action = self.preview.action
        preview = self.preview
        references = self.authorization
        selected_subject = (
            selection.application_identity,
            selection.installation_identity,
            selection.owner_identity,
            selection.release_intent.channel,
            selection.platform,
            selection.architecture,
        )
        if selected_subject != (
            observation.application_identity,
            observation.installation_identity,
            observation.owner_identity,
            observation.release_channel,
            observation.platform,
            observation.architecture,
        ):
            raise ValueError("prepared selection and observation do not bind")
        if selected_subject[1:] != (
            resolution.installation_identity,
            resolution.owner_identity,
            resolution.release_channel,
            resolution.platform,
            resolution.architecture,
        ):
            raise ValueError("prepared selection and current resolution do not bind")
        if resolution.installation_observation_fingerprint != observation.fingerprint:
            raise ValueError(
                "prepared current resolution does not bind the observation"
            )
        if (
            closure.application_identity != selection.application_identity
            or closure.exact_release != resolution.exact_release
            or action.owner_identity != selection.owner_identity
            or action.target_release != resolution.exact_release
            or action.artifact_closure_fingerprint != closure.fingerprint
        ):
            raise ValueError("prepared artifact closure and owner action do not bind")
        expected_preview = (
            selection.application_identity,
            selection.installation_identity,
            observation.fingerprint,
            observation.state_fingerprint,
            selection.owner_identity,
            observation.owner_installation_identity,
            observation.owner_runtime_identity,
            selection.release_intent.channel,
            selection.platform,
            selection.architecture,
            observation.installed_release,
            resolution.exact_release,
            resolution.artifact_digest,
            resolution.resolved_target_fingerprint,
            closure.fingerprint,
            action.fingerprint,
        )
        actual_preview = (
            preview.application_identity,
            preview.installation_identity,
            preview.source_observation_fingerprint,
            preview.source_state_fingerprint,
            preview.owner_identity,
            preview.owner_installation_identity,
            preview.owner_runtime_identity,
            preview.release_channel,
            preview.platform,
            preview.architecture,
            preview.source_release,
            preview.target_release,
            preview.target_artifact_digest,
            preview.resolved_target_fingerprint,
            preview.artifact_closure_fingerprint,
            preview.action.fingerprint,
        )
        if expected_preview != actual_preview or preview.action != action:
            raise ValueError("prepared preview does not bind the exact material")
        if (
            references is not None
            and references.preview_fingerprint != preview.fingerprint
        ):
            raise ValueError("prepared authorization does not bind the preview")

    @property
    def action(self) -> OwnerUpgradeAction:
        """Return the owner action bound into the exact preview."""

        return self.preview.action

    @property
    def identity(self) -> str:
        """Identity of exact semantic inputs and their retained material locations."""

        return canonical_fingerprint(
            {
                "schema": "prepared-owner-reconciliation:v1",
                "selection_fingerprint": self.selected.fingerprint,
                "observation_fingerprint": self.observation.fingerprint,
                "observation_state_fingerprint": self.observation.state_fingerprint,
                "current_resolution_fingerprint": self.resolution.fingerprint,
                "resolved_target_fingerprint": (
                    self.resolution.resolved_target_fingerprint
                ),
                "artifact_closure_fingerprint": self.closure.fingerprint,
                "artifact_location_fingerprint": _artifact_location_fingerprint(
                    self.closure
                ),
                "action_fingerprint": self.action.fingerprint,
                "preview_fingerprint": self.preview.fingerprint,
                "authorization_references_fingerprint": (
                    self.authorization.fingerprint
                    if self.authorization is not None
                    else None
                ),
            }
        )


class OwnerReconciliationStore:
    """Persist and rehydrate immutable prepared owner reconciliation snapshots."""

    def __init__(self, state: OperationalStateStore, *, staging_root: Path) -> None:
        if not isinstance(state, OperationalStateStore):
            raise TypeError("owner reconciliation store requires operational state")
        if not isinstance(staging_root, Path) or not staging_root.is_absolute():
            raise ValueError("owner reconciliation staging root must be absolute")
        self._state = state
        self._staging_root = staging_root

    def put(self, prepared: PreparedOwnerReconciliation) -> str:
        """Persist one exact preparation idempotently and return its identity."""

        if not isinstance(prepared, PreparedOwnerReconciliation):
            raise TypeError("owner reconciliation store requires exact preparation")
        _verify_material(prepared.closure, self._staging_root)
        record = _to_record(prepared)
        self._state.put_snapshot(
            {
                "kind": _SNAPSHOT_KIND,
                "id": prepared.identity,
                "version": prepared.identity,
                "record_version": _RECORD_VERSION,
                "record": record,
            }
        )
        return prepared.identity

    def load(self, prepared_identity: str) -> PreparedOwnerReconciliation | None:
        """Load one exact preparation, rejecting stale, missing, or altered material."""

        require_sha256(prepared_identity, "prepared reconciliation identity")
        snapshot = self._state.snapshot(
            _SNAPSHOT_KIND, prepared_identity, version=prepared_identity
        )
        if snapshot is None:
            return None
        try:
            if (
                snapshot.get("kind") != _SNAPSHOT_KIND
                or snapshot.get("id") != prepared_identity
                or snapshot.get("version") != prepared_identity
                or snapshot.get("record_version") != _RECORD_VERSION
            ):
                raise ValueError("snapshot envelope changed")
            record = _mapping(snapshot.get("record"), "record")
            prepared = _from_record(record, self._staging_root)
            if prepared.identity != prepared_identity:
                raise ValueError("prepared identity changed")
            _verify_material(prepared.closure, self._staging_root)
            return prepared
        except PreparedOwnerReconciliationError:
            raise
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise PreparedOwnerReconciliationError("prepared_state_invalid") from error


def _to_record(prepared: PreparedOwnerReconciliation) -> dict[str, object]:
    selection = prepared.selected
    observation = prepared.observation
    resolution = prepared.resolution
    closure = prepared.closure
    action = prepared.action
    preview = prepared.preview
    references = prepared.authorization
    return {
        "selection": {
            "application_identity": selection.application_identity,
            "installation_identity": selection.installation_identity,
            "owner_identity": selection.owner_identity,
            "release_intent": {
                "kind": selection.release_intent.kind.value,
                "channel": selection.release_intent.channel,
                "exact_release": selection.release_intent.exact_release,
            },
            "platform": selection.platform,
            "architecture": selection.architecture,
            "fingerprint": selection.fingerprint,
        },
        "observation": {
            **observation.payload(),
            "fingerprint": observation.fingerprint,
            "state_fingerprint": observation.state_fingerprint,
        },
        "current_resolution": {
            **resolution.canonical_payload(),
            "fingerprint": resolution.fingerprint,
            "resolved_target_fingerprint": resolution.resolved_target_fingerprint,
        },
        "artifact_closure": {
            "application_identity": closure.application_identity,
            "exact_release": closure.exact_release,
            "staging_directory": str(closure.staging_directory),
            "cache_directory": str(closure.cache_directory),
            "artifacts": [
                {
                    **artifact.canonical_payload(),
                    "staged_path": str(artifact.staged_path),
                    "fingerprint": artifact.fingerprint,
                }
                for artifact in closure.artifacts
            ],
            "fingerprint": closure.fingerprint,
            "location_fingerprint": _artifact_location_fingerprint(closure),
        },
        "action": {
            "owner_identity": action.owner_identity,
            "action_kind": action.action_kind,
            "argv": list(action.argv),
            "cwd": str(action.cwd),
            "environment": [list(item) for item in action.environment],
            "target_release": action.target_release,
            "artifact_closure_fingerprint": action.artifact_closure_fingerprint,
            "fingerprint": action.fingerprint,
        },
        "preview": {
            "application_identity": preview.application_identity,
            "installation_identity": preview.installation_identity,
            "plan_purpose": preview.plan_purpose,
            "source_observation_fingerprint": (preview.source_observation_fingerprint),
            "source_state_fingerprint": preview.source_state_fingerprint,
            "owner_identity": preview.owner_identity,
            "owner_installation_identity": preview.owner_installation_identity,
            "owner_runtime_identity": preview.owner_runtime_identity,
            "release_channel": preview.release_channel,
            "platform": preview.platform,
            "architecture": preview.architecture,
            "source_release": preview.source_release,
            "target_release": preview.target_release,
            "target_artifact_digest": preview.target_artifact_digest,
            "resolved_target_fingerprint": preview.resolved_target_fingerprint,
            "candidate_fingerprint": preview.candidate_fingerprint,
            "policy_assessment_fingerprint": preview.policy_assessment_fingerprint,
            "artifact_closure_fingerprint": preview.artifact_closure_fingerprint,
            "rollback_source_release": preview.rollback_source_release,
            "action_fingerprint": action.fingerprint,
            "fingerprint": preview.fingerprint,
        },
        "authorization_references": (
            {
                "plan_identity": references.plan_identity,
                "approval_identity": references.approval_identity,
                "assessment_identity": references.assessment_identity,
                "preview_fingerprint": references.preview_fingerprint,
                "fingerprint": references.fingerprint,
            }
            if references is not None
            else None
        ),
        "identity": prepared.identity,
    }


def _from_record(
    record: Mapping[str, object], staging_root: Path
) -> PreparedOwnerReconciliation:
    selection_record = _mapping(record["selection"], "selection")
    intent_record = _mapping(selection_record["release_intent"], "release intent")
    selection = ExternalApplicationInstallation(
        application_identity=_string(selection_record, "application_identity"),
        installation_identity=_string(selection_record, "installation_identity"),
        owner_identity=_string(selection_record, "owner_identity"),
        release_intent=ReleaseIntent(
            kind=ReleaseIntentKind(_string(intent_record, "kind")),
            channel=_string(intent_record, "channel"),
            exact_release=_optional_string(intent_record, "exact_release"),
        ),
        platform=_string(selection_record, "platform"),
        architecture=_string(selection_record, "architecture"),
    )
    _expect(selection.fingerprint, selection_record, "fingerprint")

    observation_record = _mapping(record["observation"], "observation")
    observation = InstallationObservation(
        application_identity=_string(observation_record, "application_identity"),
        installation_identity=_string(observation_record, "installation_identity"),
        owner_identity=_string(observation_record, "owner_identity"),
        owner_installation_identity=_string(
            observation_record, "owner_installation_identity"
        ),
        owner_runtime_identity=_string(observation_record, "owner_runtime_identity"),
        release_channel=_string(observation_record, "release_channel"),
        platform=_string(observation_record, "platform"),
        architecture=_string(observation_record, "architecture"),
        installed_release=_optional_string(observation_record, "installed_release"),
        installed_artifact_digest=_optional_string(
            observation_record, "installed_artifact_digest"
        ),
        active_invocation=_optional_string(observation_record, "active_invocation"),
        reachable_invocations=_strings(observation_record, "reachable_invocations"),
        observed_at=_timestamp(observation_record, "observed_at"),
    )
    _expect(observation.fingerprint, observation_record, "fingerprint")
    _expect(observation.state_fingerprint, observation_record, "state_fingerprint")

    resolution_record = _mapping(record["current_resolution"], "current resolution")
    resolution = CurrentReleaseResolution(
        installation_identity=_string(resolution_record, "installation_identity"),
        installation_observation_fingerprint=_string(
            resolution_record, "installation_observation_fingerprint"
        ),
        owner_identity=_string(resolution_record, "owner_identity"),
        release_channel=_string(resolution_record, "release_channel"),
        platform=_string(resolution_record, "platform"),
        architecture=_string(resolution_record, "architecture"),
        exact_release=_string(resolution_record, "exact_release"),
        artifact_coordinate=_string(resolution_record, "artifact_coordinate"),
        artifact_digest=_string(resolution_record, "artifact_digest"),
        authority_identity=_string(resolution_record, "authority_identity"),
        authority_response_digest=_string(
            resolution_record, "authority_response_digest"
        ),
        observed_at=_timestamp(resolution_record, "observed_at"),
        expires_at=_timestamp(resolution_record, "expires_at"),
        resolver_policy_identity=_string(resolution_record, "resolver_policy_identity"),
        validation_profile_identity=_string(
            resolution_record, "validation_profile_identity"
        ),
    )
    _expect(resolution.fingerprint, resolution_record, "fingerprint")
    _expect(
        resolution.resolved_target_fingerprint,
        resolution_record,
        "resolved_target_fingerprint",
    )

    closure_record = _mapping(record["artifact_closure"], "artifact closure")
    staging_directory = Path(_string(closure_record, "staging_directory"))
    cache_directory = Path(_string(closure_record, "cache_directory"))
    artifact_records = _mappings(closure_record, "artifacts")
    artifacts = tuple(
        _artifact_from_record(artifact_record) for artifact_record in artifact_records
    )
    _verify_paths(
        staging_root,
        staging_directory,
        cache_directory,
        tuple(artifact.staged_path for artifact in artifacts),
    )
    closure = VerifiedArtifactClosure(
        application_identity=_string(closure_record, "application_identity"),
        exact_release=_string(closure_record, "exact_release"),
        artifacts=artifacts,
        staging_directory=staging_directory,
        cache_directory=cache_directory,
    )
    _expect(closure.fingerprint, closure_record, "fingerprint")
    _expect(
        _artifact_location_fingerprint(closure),
        closure_record,
        "location_fingerprint",
    )

    action_record = _mapping(record["action"], "action")
    action = OwnerUpgradeAction(
        owner_identity=_string(action_record, "owner_identity"),
        action_kind=_string(action_record, "action_kind"),
        argv=_strings(action_record, "argv"),
        cwd=Path(_string(action_record, "cwd")),
        environment=_pairs(action_record, "environment"),
        target_release=_string(action_record, "target_release"),
        artifact_closure_fingerprint=_string(
            action_record, "artifact_closure_fingerprint"
        ),
    )
    _expect(action.fingerprint, action_record, "fingerprint")

    preview_record = _mapping(record["preview"], "preview")
    preview = OwnerUpgradePreview(
        application_identity=_string(preview_record, "application_identity"),
        installation_identity=_string(preview_record, "installation_identity"),
        plan_purpose=_string(preview_record, "plan_purpose"),
        source_observation_fingerprint=_string(
            preview_record, "source_observation_fingerprint"
        ),
        source_state_fingerprint=_string(preview_record, "source_state_fingerprint"),
        owner_identity=_string(preview_record, "owner_identity"),
        owner_installation_identity=_string(
            preview_record, "owner_installation_identity"
        ),
        owner_runtime_identity=_string(preview_record, "owner_runtime_identity"),
        release_channel=_string(preview_record, "release_channel"),
        platform=_string(preview_record, "platform"),
        architecture=_string(preview_record, "architecture"),
        source_release=_string(preview_record, "source_release"),
        target_release=_string(preview_record, "target_release"),
        target_artifact_digest=_string(preview_record, "target_artifact_digest"),
        resolved_target_fingerprint=_string(
            preview_record, "resolved_target_fingerprint"
        ),
        candidate_fingerprint=_string(preview_record, "candidate_fingerprint"),
        policy_assessment_fingerprint=_string(
            preview_record, "policy_assessment_fingerprint"
        ),
        artifact_closure_fingerprint=_string(
            preview_record, "artifact_closure_fingerprint"
        ),
        rollback_source_release=_string(preview_record, "rollback_source_release"),
        action=action,
    )
    _expect(action.fingerprint, preview_record, "action_fingerprint")
    _expect(preview.fingerprint, preview_record, "fingerprint")

    references_value = record["authorization_references"]
    references = None
    if references_value is not None:
        references_record = _mapping(references_value, "authorization references")
        references = AuthorizedOwnerUpgrade(
            plan_identity=_string(references_record, "plan_identity"),
            approval_identity=_string(references_record, "approval_identity"),
            assessment_identity=_string(references_record, "assessment_identity"),
            preview_fingerprint=_string(references_record, "preview_fingerprint"),
        )
        _expect(references.fingerprint, references_record, "fingerprint")

    prepared = PreparedOwnerReconciliation(
        selected=selection,
        observation=observation,
        resolution=resolution,
        closure=closure,
        preview=preview,
        authorization=references,
    )
    _expect(prepared.identity, record, "identity")
    return prepared


def _artifact_from_record(record: Mapping[str, object]) -> VerifiedArtifact:
    artifact = VerifiedArtifact(
        role=_string(record, "role"),
        package_identity=_string(record, "package_identity"),
        exact_release=_string(record, "exact_release"),
        coordinate=_string(record, "coordinate"),
        archive_digest=_string(record, "archive_digest"),
        installed_payload_digest=_string(record, "installed_payload_digest"),
        staged_path=Path(_string(record, "staged_path")),
    )
    _expect(artifact.fingerprint, record, "fingerprint")
    return artifact


def _artifact_location_fingerprint(closure: VerifiedArtifactClosure) -> str:
    return canonical_fingerprint(
        {
            "staging_directory": str(closure.staging_directory),
            "cache_directory": str(closure.cache_directory),
            "artifacts": [
                {"role": artifact.role, "staged_path": str(artifact.staged_path)}
                for artifact in closure.artifacts
            ],
        }
    )


def _verify_material(closure: VerifiedArtifactClosure, staging_root: Path) -> None:
    _verify_paths(
        staging_root,
        closure.staging_directory,
        closure.cache_directory,
        tuple(artifact.staged_path for artifact in closure.artifacts),
    )
    try:
        stage_metadata = closure.staging_directory.lstat()
        cache_metadata = closure.cache_directory.lstat()
        if (
            stat.S_ISLNK(stage_metadata.st_mode)
            or not stat.S_ISDIR(stage_metadata.st_mode)
            or stat.S_ISLNK(cache_metadata.st_mode)
            or not stat.S_ISDIR(cache_metadata.st_mode)
        ):
            raise PreparedOwnerReconciliationError("prepared_material_missing")
        for artifact in closure.artifacts:
            metadata = artifact.staged_path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise PreparedOwnerReconciliationError("prepared_material_missing")
            algorithm, expected = artifact.archive_digest.split(":", 1)
            digest = hashlib.new(algorithm)
            with artifact.staged_path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise PreparedOwnerReconciliationError("prepared_material_changed")
    except FileNotFoundError as error:
        raise PreparedOwnerReconciliationError("prepared_material_missing") from error
    except (OSError, ValueError) as error:
        raise PreparedOwnerReconciliationError("prepared_material_invalid") from error


def _verify_paths(
    staging_root: Path,
    staging_directory: Path,
    cache_directory: Path,
    artifact_paths: tuple[Path, ...],
) -> None:
    paths = (staging_directory, cache_directory, *artifact_paths)
    if any(not path.is_absolute() for path in paths):
        raise PreparedOwnerReconciliationError("prepared_path_invalid")
    root = staging_root.resolve(strict=False)
    if any(not path.resolve(strict=False).is_relative_to(root) for path in paths):
        raise PreparedOwnerReconciliationError("prepared_path_invalid")


def _mapping(value: object, noun: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{noun} must be an object")
    return cast(Mapping[str, object], value)


def _mappings(
    record: Mapping[str, object], key: str
) -> tuple[Mapping[str, object], ...]:
    value = record[key]
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return tuple(_mapping(item, key) for item in value)


def _string(record: Mapping[str, object], key: str) -> str:
    value = record[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _optional_string(record: Mapping[str, object], key: str) -> str | None:
    value = record[key]
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")
    return value


def _strings(record: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = record[key]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a string list")
    return tuple(cast(list[str], value))


def _pairs(record: Mapping[str, object], key: str) -> tuple[tuple[str, str], ...]:
    value = record[key]
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a pair list")
    pairs: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
        ):
            raise ValueError(f"{key} must be a string pair list")
        pairs.append((item[0], item[1]))
    return tuple(pairs)


def _timestamp(record: Mapping[str, object], key: str) -> datetime:
    value = _string(record, key)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if canonical_timestamp(parsed) != value:
        raise ValueError(f"{key} must be canonical")
    return parsed


def _expect(actual: str, record: Mapping[str, object], key: str) -> None:
    if _string(record, key) != actual:
        raise ValueError(f"{key} changed")
