"""Canonical version-2 Planning Records for authoritative mutation decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, Sequence

from .canonical import canonical_fingerprint, canonical_timestamp


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PLAN_PURPOSES = frozenset(
    {"validation", "activation", "reconciliation", "rollback", "removal"}
)
_LIFECYCLE_STATES = frozenset({"absent", "present", "active"})


class PlanDisposition(StrEnum):
    ELIGIBLE = "eligible"
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def _sequence(value: object, name: str) -> list[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{name} must be an array")
    return list(value)


def _exact_keys(value: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{name} has unsupported or missing fields")


def _string(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise ValueError(f"{name} must be a nonempty identity")
    return value


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _positive_integer(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _canonical_time(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{name} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise ValueError(f"{name} must be a canonical UTC timestamp") from error
    if canonical_timestamp(parsed) != value:
        raise ValueError(f"{name} must be a canonical UTC timestamp")
    return value


def _parsed_canonical_time(value: object, name: str) -> datetime:
    canonical = _canonical_time(value, name)
    return datetime.fromisoformat(canonical.removesuffix("Z") + "+00:00")


def _sorted_unique_strings(value: object, name: str) -> tuple[str, ...]:
    values = tuple(_string(item, name) for item in _sequence(value, name))
    if values != tuple(sorted(values)) or len(set(values)) != len(values):
        raise ValueError(f"{name} must be sorted and unique")
    return values


def _unique_strings(value: object, name: str) -> tuple[str, ...]:
    values = tuple(_string(item, name) for item in _sequence(value, name))
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must be unique")
    return values


def _sorted_unique_digests(value: object, name: str) -> tuple[str, ...]:
    values = tuple(_digest(item, name) for item in _sequence(value, name))
    if values != tuple(sorted(values)) or len(set(values)) != len(values):
        raise ValueError(f"{name} must be sorted and unique")
    return values


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Planning Record object keys must be strings")
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError("Planning Record contains a non-JSON value")


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _record_identity(
    record: Mapping[str, object], identity_field: str, name: str
) -> str:
    identity = _digest(record.get(identity_field), f"{name} identity")
    payload = {key: value for key, value in record.items() if key != identity_field}
    if canonical_fingerprint(payload) != identity:
        raise ValueError(f"{name} identity does not match its canonical record")
    return identity


def _versioned_record(
    record: Mapping[str, object], *, kind: str, identity_field: str, name: str
) -> str:
    if (
        record.get("schema_version") != 2
        or type(record.get("schema_version")) is not int
    ):
        raise ValueError(f"{name} requires schema version 2")
    if record.get("kind") != kind:
        raise ValueError(f"{name} kind must be {kind}")
    return _record_identity(record, identity_field, name)


@dataclass(frozen=True, slots=True)
class ScopeReference:
    kind: str
    id: str
    fingerprint: str

    @classmethod
    def from_mapping(cls, value: object) -> ScopeReference:
        item = _mapping(value, "declared scope")
        _exact_keys(item, {"kind", "id", "fingerprint"}, "declared scope")
        if item["kind"] != "declared_scope":
            raise ValueError("declared scope kind must be declared_scope")
        return cls(
            kind="declared_scope",
            id=_string(item["id"], "declared scope ID"),
            fingerprint=_digest(item["fingerprint"], "declared scope fingerprint"),
        )

    @property
    def identity(self) -> str:
        return canonical_fingerprint({"kind": self.kind, "id": self.id})

    def to_mapping(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id, "fingerprint": self.fingerprint}


@dataclass(frozen=True, slots=True)
class AuthorizationSubject:
    kind: str
    id: str
    fingerprint: str

    @classmethod
    def from_mapping(cls, value: object) -> AuthorizationSubject:
        item = _mapping(value, "authorization subject")
        _exact_keys(item, {"kind", "id", "fingerprint"}, "authorization subject")
        if item["kind"] != "local_user":
            raise ValueError("authorization subject kind must be local_user")
        return cls(
            kind="local_user",
            id=_string(item["id"], "authorization subject ID"),
            fingerprint=_digest(
                item["fingerprint"], "authorization subject fingerprint"
            ),
        )

    def to_mapping(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id, "fingerprint": self.fingerprint}


@dataclass(frozen=True, slots=True)
class GrantReceipt:
    kind: str
    verifier_id: str
    statement_fingerprint: str
    proof: str

    @classmethod
    def from_mapping(cls, value: object) -> GrantReceipt:
        item = _mapping(value, "grant receipt")
        _exact_keys(
            item,
            {"kind", "verifier_id", "statement_fingerprint", "proof"},
            "grant receipt",
        )
        if item["kind"] != "authenticated_grant_receipt":
            raise ValueError("grant receipt kind must be authenticated_grant_receipt")
        proof = _string(item["proof"], "grant receipt proof")
        if not proof.startswith("base64url:") or proof == "base64url:":
            raise ValueError("grant receipt proof must be an opaque base64url proof")
        return cls(
            kind="authenticated_grant_receipt",
            verifier_id=_string(item["verifier_id"], "grant receipt verifier ID"),
            statement_fingerprint=_digest(
                item["statement_fingerprint"], "approval statement fingerprint"
            ),
            proof=proof,
        )

    def to_mapping(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "verifier_id": self.verifier_id,
            "statement_fingerprint": self.statement_fingerprint,
            "proof": self.proof,
        }


@dataclass(frozen=True, slots=True)
class OwnerUpgradeRequest:
    installation_identity: str
    release_artifact_identity: str
    source_state_fingerprint: str
    preview_fingerprint: str
    owner_action_fingerprint: str
    artifact_closure_fingerprint: str

    @classmethod
    def from_mapping(cls, value: object) -> OwnerUpgradeRequest:
        item = _mapping(value, "owner-upgrade request")
        _exact_keys(
            item,
            {
                "installation_identity",
                "release_artifact_identity",
                "source_state_fingerprint",
                "preview_fingerprint",
                "owner_action_fingerprint",
                "artifact_closure_fingerprint",
            },
            "owner-upgrade request",
        )
        return cls(
            installation_identity=_string(
                item["installation_identity"], "installation identity"
            ),
            release_artifact_identity=_digest(
                item["release_artifact_identity"], "release artifact identity"
            ),
            source_state_fingerprint=_digest(
                item["source_state_fingerprint"], "source state fingerprint"
            ),
            preview_fingerprint=_digest(
                item["preview_fingerprint"], "preview fingerprint"
            ),
            owner_action_fingerprint=_digest(
                item["owner_action_fingerprint"], "owner action fingerprint"
            ),
            artifact_closure_fingerprint=_digest(
                item["artifact_closure_fingerprint"],
                "artifact closure fingerprint",
            ),
        )

    def to_mapping(self) -> dict[str, str]:
        return {
            "installation_identity": self.installation_identity,
            "release_artifact_identity": self.release_artifact_identity,
            "source_state_fingerprint": self.source_state_fingerprint,
            "preview_fingerprint": self.preview_fingerprint,
            "owner_action_fingerprint": self.owner_action_fingerprint,
            "artifact_closure_fingerprint": self.artifact_closure_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class OwnerUpgradeMutation:
    mutation_id: str
    step_id: str
    target_id: str
    plan_target_fingerprint: str
    capability_port: str
    operation: str
    request: OwnerUpgradeRequest
    expected_current_fingerprint: str
    recovery: Mapping[str, object]

    @classmethod
    def from_mapping(cls, value: object) -> OwnerUpgradeMutation:
        item = _mapping(value, "owner-upgrade mutation")
        _exact_keys(
            item,
            {
                "mutation_id",
                "step_id",
                "target_id",
                "plan_target_fingerprint",
                "capability_port",
                "operation",
                "request",
                "expected_current",
                "recovery",
            },
            "owner-upgrade mutation",
        )
        if item["capability_port"] != "external_application_installation_lifecycle":
            raise ValueError(
                "owner upgrade uses the external application lifecycle port"
            )
        if item["operation"] != "upgrade":
            raise ValueError("owner-upgrade mutation operation must be upgrade")
        request = OwnerUpgradeRequest.from_mapping(item["request"])
        expected = _mapping(item["expected_current"], "expected current")
        _exact_keys(expected, {"fingerprint"}, "expected current")
        expected_fingerprint = _digest(
            expected["fingerprint"], "expected-current fingerprint"
        )
        if expected_fingerprint != request.source_state_fingerprint:
            raise ValueError("expected current must equal the source stable state")
        recovery = _validated_restore(item["recovery"], request.installation_identity)
        return cls(
            mutation_id=_string(item["mutation_id"], "mutation ID"),
            step_id=_string(item["step_id"], "step ID"),
            target_id=_string(item["target_id"], "target ID"),
            plan_target_fingerprint=_digest(
                item["plan_target_fingerprint"], "Plan Target fingerprint"
            ),
            capability_port="external_application_installation_lifecycle",
            operation="upgrade",
            request=request,
            expected_current_fingerprint=expected_fingerprint,
            recovery=recovery,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "mutation_id": self.mutation_id,
            "step_id": self.step_id,
            "target_id": self.target_id,
            "plan_target_fingerprint": self.plan_target_fingerprint,
            "capability_port": self.capability_port,
            "operation": self.operation,
            "request": self.request.to_mapping(),
            "expected_current": {"fingerprint": self.expected_current_fingerprint},
            "recovery": _thaw(self.recovery),
        }


def _validated_restore(
    value: object, installation_identity: str
) -> Mapping[str, object]:
    recovery = _mapping(value, "owner-upgrade recovery")
    _exact_keys(
        recovery,
        {"mode", "capability_port", "operation", "request"},
        "owner-upgrade recovery",
    )
    if (
        recovery["mode"] != "restore"
        or recovery["capability_port"] != "external_application_installation_lifecycle"
        or recovery["operation"] != "restore"
    ):
        raise ValueError("owner-upgrade recovery must be an exact owner restore")
    request = _mapping(recovery["request"], "restore request")
    _exact_keys(
        request,
        {"installation_identity", "release_artifact_identity"},
        "restore request",
    )
    if request["installation_identity"] != installation_identity:
        raise ValueError("restore request must target the upgraded installation")
    _digest(request["release_artifact_identity"], "restore artifact identity")
    return _freeze(recovery)  # type: ignore[return-value]


@dataclass(frozen=True, slots=True, init=False)
class Plan:
    plan_identity: str
    blueprint_identity: str
    scope: ScopeReference
    purpose: str
    created_at: str
    owner_upgrade_mutations: tuple[OwnerUpgradeMutation, ...]
    _record: object = field(repr=False, compare=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Plan:
        record = _mapping(value, "Plan")
        _exact_keys(
            record,
            {
                "schema_version",
                "kind",
                "plan_identity",
                "blueprint_identity",
                "scope",
                "purpose",
                "created_at",
                "targets",
                "required_steps",
                "proposed_declarations",
                "mutations",
            },
            "Plan",
        )
        plan_identity = _versioned_record(
            record, kind="mastic.plan", identity_field="plan_identity", name="Plan"
        )
        blueprint_identity = _digest(record["blueprint_identity"], "Blueprint identity")
        scope = ScopeReference.from_mapping(record["scope"])
        purpose = _string(record["purpose"], "Plan purpose")
        if purpose not in _PLAN_PURPOSES:
            raise ValueError("Plan purpose is invalid")
        created_at = _canonical_time(record["created_at"], "Plan creation time")
        targets = _validate_targets(record["targets"], purpose)
        steps = _validate_steps(record["required_steps"], purpose, targets)
        declarations = _sequence(record["proposed_declarations"], "declarations")
        if declarations:
            raise ValueError("owner-upgrade Plan cannot propose Desired State")
        mutations = _validate_mutations(record["mutations"], targets, steps)
        instance = object.__new__(cls)
        object.__setattr__(instance, "plan_identity", plan_identity)
        object.__setattr__(instance, "blueprint_identity", blueprint_identity)
        object.__setattr__(instance, "scope", scope)
        object.__setattr__(instance, "purpose", purpose)
        object.__setattr__(instance, "created_at", created_at)
        object.__setattr__(instance, "owner_upgrade_mutations", mutations)
        object.__setattr__(instance, "_record", _freeze(record))
        return instance

    @property
    def owner_upgrade_mutation(self) -> OwnerUpgradeMutation:
        if len(self.owner_upgrade_mutations) != 1:
            raise ValueError("Plan does not contain exactly one owner-upgrade mutation")
        return self.owner_upgrade_mutations[0]

    @property
    def target_ids(self) -> tuple[str, ...]:
        record = self.to_mapping()
        return tuple(str(item["target_id"]) for item in record["targets"])

    def to_mapping(self) -> dict[str, object]:
        result = _thaw(self._record)
        if not isinstance(result, dict):
            raise AssertionError("frozen Plan record is not an object")
        return result


def _validate_targets(value: object, purpose: str) -> dict[str, str]:
    items = [_mapping(item, "Plan Target") for item in _sequence(value, "Plan targets")]
    if not items:
        raise ValueError("Plan requires at least one target")
    keys: list[tuple[str, str]] = []
    targets: dict[str, str] = {}
    for item in items:
        _exact_keys(
            item,
            {
                "target_id",
                "target_kind",
                "subject_fingerprint",
                "plan_target_fingerprint",
                "lifecycle_applicability",
                "expected_lifecycle_state",
                "operational_contract_ref",
            },
            "Plan Target",
        )
        target_id = _string(item["target_id"], "target ID")
        if item["target_kind"] != "external_application_installation":
            raise ValueError("owner-upgrade target must be an external installation")
        _digest(item["subject_fingerprint"], "target subject fingerprint")
        target_fingerprint = _digest(
            item["plan_target_fingerprint"], "Plan Target fingerprint"
        )
        preimage = {
            "purpose": purpose,
            "target": {
                key: value
                for key, value in item.items()
                if key != "plan_target_fingerprint"
            },
        }
        if canonical_fingerprint(preimage) != target_fingerprint:
            raise ValueError("Plan Target fingerprint does not match its target")
        applicability = item["lifecycle_applicability"]
        expected = item["expected_lifecycle_state"]
        if applicability != "applicable" or expected not in _LIFECYCLE_STATES:
            raise ValueError("owner-upgrade target must have applicable lifecycle")
        _exact_reference(
            item["operational_contract_ref"],
            "operational_contract",
            "operational contract",
        )
        if target_id in targets or target_fingerprint in targets.values():
            raise ValueError("Plan Target identities must be unique")
        targets[target_id] = target_fingerprint
        keys.append((target_id, target_fingerprint))
    if keys != sorted(keys):
        raise ValueError("Plan targets must be canonically sorted")
    return targets


def _exact_reference(value: object, kind: str, name: str) -> dict[str, str]:
    item = _mapping(value, name)
    _exact_keys(item, {"kind", "id", "fingerprint"}, name)
    if item["kind"] != kind:
        raise ValueError(f"{name} kind must be {kind}")
    return {
        "kind": kind,
        "id": _string(item["id"], f"{name} ID"),
        "fingerprint": _digest(item["fingerprint"], f"{name} fingerprint"),
    }


def _validate_steps(
    value: object, purpose: str, targets: Mapping[str, str]
) -> dict[str, tuple[str, str, tuple[str, ...]]]:
    items = [_mapping(item, "Plan step") for item in _sequence(value, "Plan steps")]
    if not items:
        raise ValueError("Plan requires at least one step")
    result: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    fingerprints: set[str] = set()
    order: list[tuple[str, str]] = []
    dependencies: dict[str, tuple[str, ...]] = {}
    for item in items:
        _exact_keys(
            item,
            {
                "step_id",
                "step_fingerprint",
                "target_id",
                "plan_target_fingerprint",
                "execution_kind",
                "mutation_ids",
                "depends_on",
                "skip_rule_id",
                "reuse_rule_id",
            },
            "Plan step",
        )
        step_id = _string(item["step_id"], "step ID")
        fingerprint = _digest(item["step_fingerprint"], "step fingerprint")
        target_id = _string(item["target_id"], "step target ID")
        target_fingerprint = _digest(
            item["plan_target_fingerprint"], "step Plan Target fingerprint"
        )
        if targets.get(target_id) != target_fingerprint:
            raise ValueError("Plan step must reference one exact Plan Target")
        if item["execution_kind"] != "mutation":
            raise ValueError("owner-upgrade Plan step must be a mutation")
        mutation_ids = _unique_strings(item["mutation_ids"], "mutation IDs")
        if not mutation_ids:
            raise ValueError("mutation step requires mutation IDs")
        depends_on = _sorted_unique_strings(item["depends_on"], "step dependencies")
        if item["skip_rule_id"] is not None:
            _string(item["skip_rule_id"], "skip rule ID")
        if item["reuse_rule_id"] is not None:
            _string(item["reuse_rule_id"], "reuse rule ID")
        preimage = {
            "purpose": purpose,
            "step": {
                key: entry for key, entry in item.items() if key != "step_fingerprint"
            },
        }
        if canonical_fingerprint(preimage) != fingerprint:
            raise ValueError("step fingerprint does not match its Plan step")
        if step_id in result or fingerprint in fingerprints:
            raise ValueError("Plan step identities must be unique")
        result[step_id] = (target_id, target_fingerprint, mutation_ids)
        fingerprints.add(fingerprint)
        dependencies[step_id] = depends_on
        order.append((step_id, fingerprint))
    if order != sorted(order):
        raise ValueError("Plan steps must be canonically sorted")
    if any(
        dependency not in result or dependency == step_id
        for step_id, required in dependencies.items()
        for dependency in required
    ):
        raise ValueError("Plan step dependency is dangling or self-referential")
    _reject_dependency_cycles(dependencies)
    return result


def _reject_dependency_cycles(dependencies: Mapping[str, tuple[str, ...]]) -> None:
    visiting: set[str] = set()
    complete: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in visiting:
            raise ValueError("Plan step dependency graph must be acyclic")
        if step_id in complete:
            return
        visiting.add(step_id)
        for dependency in dependencies[step_id]:
            visit(dependency)
        visiting.remove(step_id)
        complete.add(step_id)

    for step_id in dependencies:
        visit(step_id)


def _validate_mutations(
    value: object,
    targets: Mapping[str, str],
    steps: Mapping[str, tuple[str, str, tuple[str, ...]]],
) -> tuple[OwnerUpgradeMutation, ...]:
    mutations = tuple(
        OwnerUpgradeMutation.from_mapping(item)
        for item in _sequence(value, "Plan mutations")
    )
    if not mutations:
        raise ValueError("owner-upgrade Plan requires a mutation")
    if tuple(item.mutation_id for item in mutations) != tuple(
        sorted(item.mutation_id for item in mutations)
    ) or len({item.mutation_id for item in mutations}) != len(mutations):
        raise ValueError("Plan mutations must be sorted and unique")
    referenced: set[str] = set()
    for mutation in mutations:
        step = steps.get(mutation.step_id)
        if (
            step is None
            or step[0] != mutation.target_id
            or step[1] != mutation.plan_target_fingerprint
            or targets.get(mutation.target_id) != mutation.plan_target_fingerprint
            or mutation.mutation_id not in step[2]
            or mutation.request.installation_identity != mutation.target_id
        ):
            raise ValueError("owner-upgrade mutation references do not resolve")
        referenced.add(mutation.mutation_id)
    declared = {mutation_id for step in steps.values() for mutation_id in step[2]}
    if referenced != declared:
        raise ValueError("Plan step mutation IDs must equal the mutation set")
    return mutations


@dataclass(frozen=True, slots=True, init=False)
class PlanApproval:
    approval_identity: str
    authorization_subject: AuthorizationSubject
    plan_identity: str
    plan_purpose: str
    policy_fingerprint: str
    evidence_set_fingerprint: str
    applicable_claim_ids: tuple[str, ...]
    rule_ids: tuple[str, ...]
    override_rule_ids: tuple[str, ...]
    granted_at: str
    valid_until: str | None
    grant_receipt: GrantReceipt
    _record: object = field(repr=False, compare=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PlanApproval:
        record = _mapping(value, "Plan Approval")
        _exact_keys(
            record,
            {
                "schema_version",
                "kind",
                "approval_identity",
                "authorization_subject",
                "plan_identity",
                "plan_purpose",
                "policy_fingerprint",
                "evidence_set_fingerprint",
                "applicable_claim_ids",
                "rule_ids",
                "override_rule_ids",
                "granted_at",
                "valid_until",
                "grant_receipt",
            },
            "Plan Approval",
        )
        approval_identity = _versioned_record(
            record,
            kind="mastic.plan-approval",
            identity_field="approval_identity",
            name="Plan Approval",
        )
        subject = AuthorizationSubject.from_mapping(record["authorization_subject"])
        plan_identity = _digest(record["plan_identity"], "Plan identity")
        purpose = _string(record["plan_purpose"], "Plan purpose")
        if purpose not in _PLAN_PURPOSES:
            raise ValueError("Plan Approval purpose is invalid")
        policy = _digest(record["policy_fingerprint"], "policy fingerprint")
        evidence = _digest(
            record["evidence_set_fingerprint"], "Evidence-set fingerprint"
        )
        claims = _sorted_unique_digests(
            record["applicable_claim_ids"], "applicable Claim identities"
        )
        rules = _sorted_unique_strings(record["rule_ids"], "approval rule IDs")
        overrides = _sorted_unique_strings(
            record["override_rule_ids"], "override rule IDs"
        )
        if not (rules or overrides) or set(rules) & set(overrides):
            raise ValueError(
                "approval rule and override sets must be nonempty and disjoint"
            )
        granted_datetime = _parsed_canonical_time(
            record["granted_at"], "approval grant time"
        )
        granted_at = canonical_timestamp(granted_datetime)
        valid_value = record["valid_until"]
        valid_datetime = (
            None
            if valid_value is None
            else _parsed_canonical_time(valid_value, "approval validity endpoint")
        )
        valid_until = (
            None if valid_datetime is None else canonical_timestamp(valid_datetime)
        )
        if valid_datetime is not None and valid_datetime <= granted_datetime:
            raise ValueError("Plan Approval validity window must be nonempty")
        receipt = GrantReceipt.from_mapping(record["grant_receipt"])
        statement = {
            key: entry
            for key, entry in record.items()
            if key not in {"approval_identity", "grant_receipt"}
        }
        statement_fingerprint = canonical_fingerprint(statement)
        if receipt.statement_fingerprint != statement_fingerprint:
            raise ValueError("grant receipt does not bind the Approval statement")
        instance = object.__new__(cls)
        for name, entry in (
            ("approval_identity", approval_identity),
            ("authorization_subject", subject),
            ("plan_identity", plan_identity),
            ("plan_purpose", purpose),
            ("policy_fingerprint", policy),
            ("evidence_set_fingerprint", evidence),
            ("applicable_claim_ids", claims),
            ("rule_ids", rules),
            ("override_rule_ids", overrides),
            ("granted_at", granted_at),
            ("valid_until", valid_until),
            ("grant_receipt", receipt),
            ("_record", _freeze(record)),
        ):
            object.__setattr__(instance, name, entry)
        return instance

    @property
    def statement_fingerprint(self) -> str:
        return self.grant_receipt.statement_fingerprint

    def to_mapping(self) -> dict[str, object]:
        result = _thaw(self._record)
        if not isinstance(result, dict):
            raise AssertionError("frozen Plan Approval record is not an object")
        return result


@dataclass(frozen=True, slots=True, init=False)
class PlanAssessment:
    assessment_identity: str
    plan_identity: str
    blueprint_identity: str
    scope: ScopeReference
    purpose: str
    target_ids: tuple[str, ...]
    evaluated_at: str
    policy_fingerprint: str
    evidence_set_fingerprint: str
    disposition: PlanDisposition
    approval_identities: tuple[str, ...]
    applicable_approval_identities: tuple[str, ...]
    _record: object = field(repr=False, compare=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PlanAssessment:
        record = _mapping(value, "Plan Assessment")
        _exact_keys(
            record,
            {
                "schema_version",
                "kind",
                "assessment_identity",
                "plan",
                "evaluation",
                "evidence_ids",
                "policy_inputs",
                "claims",
                "claim_qualifications",
                "claim_applicability",
                "claim_conflicts",
                "positions",
                "approvals",
                "policy_assessment",
                "operational_assessment",
                "issues",
            },
            "Plan Assessment",
        )
        identity = _versioned_record(
            record,
            kind="mastic.plan-assessment",
            identity_field="assessment_identity",
            name="Plan Assessment",
        )
        projection = _validate_plan_projection(record["plan"])
        evidence_ids = _sorted_unique_digests(
            record["evidence_ids"], "assessment Evidence identities"
        )
        evaluated_at, policy_fingerprint, evidence_fingerprint = _validate_evaluation(
            record["evaluation"], projection, evidence_ids
        )
        _validate_policy_inputs(record["policy_inputs"])
        for key in (
            "claims",
            "claim_qualifications",
            "claim_applicability",
            "claim_conflicts",
            "issues",
        ):
            _validate_canonical_object_array(record[key], key)
        positions = _mapping(record["positions"], "assessment positions")
        _exact_keys(positions, {"support", "permission", "searches"}, "positions")
        for key in ("support", "permission", "searches"):
            _validate_canonical_object_array(positions[key], f"position {key}")
        approval_ids = _validate_approval_references(record["approvals"])
        disposition, applicable = _validate_policy_assessment(
            record["policy_assessment"], approval_ids
        )
        _validate_operational_assessment(record["operational_assessment"])
        instance = object.__new__(cls)
        values = (
            ("assessment_identity", identity),
            ("plan_identity", projection["plan_identity"]),
            ("blueprint_identity", projection["blueprint_identity"]),
            ("scope", projection["scope"]),
            ("purpose", projection["purpose"]),
            ("target_ids", projection["target_ids"]),
            ("evaluated_at", evaluated_at),
            ("policy_fingerprint", policy_fingerprint),
            ("evidence_set_fingerprint", evidence_fingerprint),
            ("disposition", disposition),
            ("approval_identities", approval_ids),
            ("applicable_approval_identities", applicable),
            ("_record", _freeze(record)),
        )
        for name, entry in values:
            object.__setattr__(instance, name, entry)
        return instance

    @property
    def plan_projection(self) -> dict[str, object]:
        return {
            "plan_identity": self.plan_identity,
            "blueprint_identity": self.blueprint_identity,
            "scope": self.scope.to_mapping(),
            "purpose": self.purpose,
            "target_ids": list(self.target_ids),
        }

    def to_mapping(self) -> dict[str, object]:
        result = _thaw(self._record)
        if not isinstance(result, dict):
            raise AssertionError("frozen Plan Assessment record is not an object")
        return result


def _validate_plan_projection(value: object) -> dict[str, object]:
    projection = _mapping(value, "assessment Plan projection")
    _exact_keys(
        projection,
        {"plan_identity", "blueprint_identity", "scope", "purpose", "target_ids"},
        "assessment Plan projection",
    )
    purpose = _string(projection["purpose"], "assessment Plan purpose")
    if purpose not in _PLAN_PURPOSES:
        raise ValueError("assessment Plan purpose is invalid")
    targets = _sorted_unique_strings(
        projection["target_ids"], "assessment target identities"
    )
    if not targets:
        raise ValueError("assessment requires Plan Targets")
    return {
        "plan_identity": _digest(projection["plan_identity"], "Plan identity"),
        "blueprint_identity": _digest(
            projection["blueprint_identity"], "Blueprint identity"
        ),
        "scope": ScopeReference.from_mapping(projection["scope"]),
        "purpose": purpose,
        "target_ids": targets,
    }


def _validate_evaluation(
    value: object, projection: Mapping[str, object], evidence_ids: tuple[str, ...]
) -> tuple[str, str, str]:
    evaluation = _mapping(value, "assessment evaluation")
    _exact_keys(
        evaluation,
        {"evaluated_at", "policy_selection", "policy", "evidence_set_fingerprint"},
        "assessment evaluation",
    )
    evaluated_at = _canonical_time(evaluation["evaluated_at"], "evaluation time")
    policy = _mapping(evaluation["policy"], "selected policy")
    _exact_keys(
        policy,
        {
            "id",
            "version",
            "fingerprint",
            "input_requirements",
            "rules",
            "candidate_selection",
        },
        "selected policy",
    )
    _string(policy["id"], "policy ID")
    _positive_integer(policy["version"], "policy version")
    policy_fingerprint = _digest(policy["fingerprint"], "policy fingerprint")
    policy_payload = {
        key: entry for key, entry in policy.items() if key != "fingerprint"
    }
    if canonical_fingerprint(policy_payload) != policy_fingerprint:
        raise ValueError("policy fingerprint does not match the selected policy")
    _validate_canonical_object_array(policy["input_requirements"], "policy inputs")
    rules = _validate_rule_definitions(policy["rules"])
    candidate = _mapping(policy["candidate_selection"], "candidate-selection rule")
    _exact_keys(candidate, {"rule_id", "version"}, "candidate-selection rule")
    _string(candidate["rule_id"], "candidate-selection rule ID")
    _positive_integer(candidate["version"], "candidate-selection rule version")
    selection = _mapping(evaluation["policy_selection"], "Policy Selection")
    _exact_keys(
        selection,
        {
            "kind",
            "id",
            "fingerprint",
            "scope_identity",
            "purpose",
            "policy_fingerprint",
            "authority_ref",
        },
        "Policy Selection",
    )
    if selection["kind"] != "policy_selection":
        raise ValueError("Policy Selection kind is invalid")
    _string(selection["id"], "Policy Selection ID")
    selection_fingerprint = _digest(
        selection["fingerprint"], "Policy Selection fingerprint"
    )
    selection_payload = {
        key: entry for key, entry in selection.items() if key != "fingerprint"
    }
    if canonical_fingerprint(selection_payload) != selection_fingerprint:
        raise ValueError("Policy Selection fingerprint does not match")
    scope = projection["scope"]
    if not isinstance(scope, ScopeReference):
        raise AssertionError("validated assessment scope is not a ScopeReference")
    if (
        selection["scope_identity"] != scope.identity
        or selection["purpose"] != projection["purpose"]
        or selection["policy_fingerprint"] != policy_fingerprint
    ):
        raise ValueError("Policy Selection does not bind the assessment context")
    _exact_reference(
        selection["authority_ref"], "local_policy_authority", "policy authority"
    )
    evidence_fingerprint = _digest(
        evaluation["evidence_set_fingerprint"], "Evidence-set fingerprint"
    )
    if canonical_fingerprint(list(evidence_ids)) != evidence_fingerprint:
        raise ValueError("Evidence-set fingerprint does not match assessment Evidence")
    if len(rules) != len(set(rules)):
        raise ValueError("policy rule identities must be unique")
    return evaluated_at, policy_fingerprint, evidence_fingerprint


def _validate_rule_definitions(value: object) -> tuple[str, ...]:
    rules = [_mapping(item, "policy rule") for item in _sequence(value, "policy rules")]
    identities: list[str] = []
    for rule in rules:
        rule_id = _string(rule.get("rule_id"), "policy rule ID")
        _freeze(rule)
        identities.append(rule_id)
    if identities != sorted(identities):
        raise ValueError("policy rules must be sorted by rule ID")
    return tuple(identities)


def _validate_policy_inputs(value: object) -> None:
    inputs = _mapping(value, "policy inputs")
    _exact_keys(
        inputs,
        {
            "fingerprint",
            "claim_ids",
            "claim_conflict_ids",
            "evidence_ids",
            "discovery_evidence_ids",
        },
        "policy inputs",
    )
    fingerprint = _digest(inputs["fingerprint"], "policy-input fingerprint")
    for key in (
        "claim_ids",
        "claim_conflict_ids",
        "evidence_ids",
        "discovery_evidence_ids",
    ):
        _sorted_unique_digests(inputs[key], f"policy-input {key}")
    payload = {key: entry for key, entry in inputs.items() if key != "fingerprint"}
    if canonical_fingerprint(payload) != fingerprint:
        raise ValueError("policy-input fingerprint does not match")


def _validate_canonical_object_array(value: object, name: str) -> None:
    items = [_mapping(item, name) for item in _sequence(value, name)]
    frozen = [_freeze(item) for item in items]
    encoded = [canonical_fingerprint(_thaw(item)) for item in frozen]
    if len(set(encoded)) != len(encoded):
        raise ValueError(f"{name} contains duplicate records")


def _validate_approval_references(value: object) -> tuple[str, ...]:
    references = [
        _mapping(item, "Approval reference")
        for item in _sequence(value, "Approval references")
    ]
    identities: list[str] = []
    for reference in references:
        _exact_keys(reference, {"identity", "kind"}, "Approval reference")
        if reference["kind"] != "mastic.plan-approval":
            raise ValueError("Approval reference kind is invalid")
        identities.append(_digest(reference["identity"], "Approval identity"))
    if identities != sorted(identities) or len(set(identities)) != len(identities):
        raise ValueError("Approval references must be sorted and unique")
    return tuple(identities)


def _validate_policy_assessment(
    value: object, approval_ids: tuple[str, ...]
) -> tuple[PlanDisposition, tuple[str, ...]]:
    assessment = _mapping(value, "Policy Assessment")
    _exact_keys(
        assessment,
        {
            "disposition",
            "applicable_claim_ids",
            "claim_conflict_ids",
            "rule_evaluations",
            "approval_evaluation",
        },
        "Policy Assessment",
    )
    try:
        disposition = PlanDisposition(assessment["disposition"])
    except (TypeError, ValueError) as error:
        raise ValueError("Plan Disposition is invalid") from error
    _sorted_unique_digests(
        assessment["applicable_claim_ids"], "applicable Claim identities"
    )
    _sorted_unique_digests(
        assessment["claim_conflict_ids"], "Claim Conflict identities"
    )
    approval_evaluation = _mapping(
        assessment["approval_evaluation"], "Approval evaluation"
    )
    _exact_keys(
        approval_evaluation,
        {"requirement", "evaluations"},
        "Approval evaluation",
    )
    requirement = approval_evaluation["requirement"]
    if requirement not in {"not_required", "missing", "evaluated"}:
        raise ValueError("Approval evaluation requirement is invalid")
    evaluations = [
        _mapping(item, "Approval applicability")
        for item in _sequence(
            approval_evaluation["evaluations"], "Approval applicability evaluations"
        )
    ]
    evaluated_ids: list[str] = []
    applicable: list[str] = []
    for evaluation in evaluations:
        _exact_keys(
            evaluation,
            {"approval_identity", "value"},
            "Approval applicability",
        )
        identity = _digest(evaluation["approval_identity"], "Approval identity")
        if evaluation["value"] not in {"applicable", "inapplicable"}:
            raise ValueError("Approval applicability value is invalid")
        evaluated_ids.append(identity)
        if evaluation["value"] == "applicable":
            applicable.append(identity)
    if evaluated_ids != sorted(evaluated_ids) or len(set(evaluated_ids)) != len(
        evaluated_ids
    ):
        raise ValueError("Approval evaluations must be sorted and unique")
    if requirement == "evaluated":
        if not approval_ids or tuple(evaluated_ids) != approval_ids:
            raise ValueError("evaluated Approvals must equal Approval references")
    elif approval_ids or evaluations:
        raise ValueError("missing or unneeded Approval cannot carry evaluations")
    results = _validate_rule_evaluations(
        assessment["rule_evaluations"], set(applicable)
    )
    reduced = (
        PlanDisposition.BLOCKED
        if "blocked" in results
        else PlanDisposition.APPROVAL_REQUIRED
        if "approval_required" in results
        else PlanDisposition.ELIGIBLE
    )
    if reduced is not disposition:
        raise ValueError("Plan Disposition does not equal its rule reduction")
    return disposition, tuple(applicable)


def _validate_rule_evaluations(value: object, applicable: set[str]) -> set[str]:
    evaluations = [
        _mapping(item, "rule evaluation")
        for item in _sequence(value, "rule evaluations")
    ]
    rule_ids: list[str] = []
    results: set[str] = set()
    for evaluation in evaluations:
        _exact_keys(
            evaluation,
            {
                "rule_id",
                "result",
                "claim_ids",
                "claim_conflict_ids",
                "evidence_ids",
                "approval_identity",
            },
            "rule evaluation",
        )
        rule_ids.append(_string(evaluation["rule_id"], "rule ID"))
        result = evaluation["result"]
        if result not in {"satisfied", "approval_required", "blocked"}:
            raise ValueError("rule evaluation result is invalid")
        results.add(str(result))
        for key in ("claim_ids", "claim_conflict_ids", "evidence_ids"):
            _sorted_unique_digests(evaluation[key], f"rule {key}")
        approval = evaluation["approval_identity"]
        if approval is not None:
            identity = _digest(approval, "rule Approval identity")
            if result != "satisfied" or identity not in applicable:
                raise ValueError("rule names an inapplicable Approval")
    if rule_ids != sorted(rule_ids) or len(set(rule_ids)) != len(rule_ids):
        raise ValueError("rule evaluations must be sorted and unique")
    return results


def _validate_operational_assessment(value: object) -> None:
    assessment = _mapping(value, "Plan Operational Assessment")
    _exact_keys(
        assessment,
        {"completion", "targets", "summary"},
        "Plan Operational Assessment",
    )
    completion = _mapping(assessment["completion"], "Plan Completion")
    _exact_keys(
        completion,
        {"value", "required_step_ids", "step_satisfactions"},
        "Plan Completion",
    )
    if completion["value"] not in {"partial", "complete"}:
        raise ValueError("Plan Completion value is invalid")
    _sorted_unique_strings(completion["required_step_ids"], "required step IDs")
    _validate_canonical_object_array(
        completion["step_satisfactions"], "step satisfactions"
    )
    _validate_canonical_object_array(assessment["targets"], "operational targets")
    _mapping(assessment["summary"], "operational summary")


@dataclass(frozen=True, slots=True, init=False)
class CurrentPlanPointer:
    pointer_identity: str
    scope: ScopeReference
    scope_identity: str
    pointer_version: int
    expected_predecessor_identity: str | None
    plan_identity: str
    plan_purpose: str
    assessment_identity: str
    updated_at: str
    _record: object = field(repr=False, compare=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CurrentPlanPointer:
        record = _mapping(value, "current Plan pointer")
        _exact_keys(
            record,
            {
                "schema_version",
                "kind",
                "pointer_identity",
                "scope",
                "scope_identity",
                "pointer_version",
                "expected_predecessor_identity",
                "plan_identity",
                "plan_purpose",
                "assessment_identity",
                "updated_at",
            },
            "current Plan pointer",
        )
        identity = _versioned_record(
            record,
            kind="mastic.current-plan-pointer",
            identity_field="pointer_identity",
            name="current Plan pointer",
        )
        scope = ScopeReference.from_mapping(record["scope"])
        scope_identity = _digest(record["scope_identity"], "scope identity")
        if scope_identity != scope.identity:
            raise ValueError("scope identity does not match the declared scope lineage")
        version = _positive_integer(record["pointer_version"], "pointer version")
        predecessor_value = record["expected_predecessor_identity"]
        predecessor = (
            None
            if predecessor_value is None
            else _digest(predecessor_value, "expected predecessor identity")
        )
        if (version == 1) != (predecessor is None):
            raise ValueError(
                "pointer predecessor must match first or successor version"
            )
        plan_identity = _digest(record["plan_identity"], "Plan identity")
        purpose = _string(record["plan_purpose"], "Plan purpose")
        if purpose not in _PLAN_PURPOSES:
            raise ValueError("current Plan pointer purpose is invalid")
        assessment_identity = _digest(
            record["assessment_identity"], "Plan Assessment identity"
        )
        updated_at = _canonical_time(record["updated_at"], "pointer update time")
        instance = object.__new__(cls)
        for name, entry in (
            ("pointer_identity", identity),
            ("scope", scope),
            ("scope_identity", scope_identity),
            ("pointer_version", version),
            ("expected_predecessor_identity", predecessor),
            ("plan_identity", plan_identity),
            ("plan_purpose", purpose),
            ("assessment_identity", assessment_identity),
            ("updated_at", updated_at),
            ("_record", _freeze(record)),
        ):
            object.__setattr__(instance, name, entry)
        return instance

    def to_mapping(self) -> dict[str, object]:
        result = _thaw(self._record)
        if not isinstance(result, dict):
            raise AssertionError("frozen current Plan pointer is not an object")
        return result
