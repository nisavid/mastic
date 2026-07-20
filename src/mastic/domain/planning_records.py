"""Canonical version-2 Planning Records for authoritative mutation decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, Sequence

from .canonical import canonical_fingerprint, canonical_json_bytes, canonical_timestamp


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CANONICAL_TIMESTAMP = re.compile(
    r"(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"T(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]{1,9}))?Z\Z"
)
_PLAN_PURPOSES = frozenset(
    {"validation", "activation", "reconciliation", "rollback", "removal"}
)
_LIFECYCLE_STATES = frozenset({"absent", "present", "active"})
_ASSESSMENT_SORT_KEYS = {
    "claims": ("claim_identity",),
    "claim_qualifications": ("claim_identity",),
    "claim_applicability": ("claim_identity",),
    "claim_conflicts": ("conflict_identity",),
}
_POSITION_SORT_KEYS = {
    "support": ("claim_identity",),
    "permission": ("claim_identity",),
    "searches": ("search_identity",),
}
_SUPPORTED_ASSESSMENT_ISSUES = {
    "lifecycle_not_observed": (
        "Target lifecycle has not been observed.",
        ("inspect target lifecycle",),
    ),
    "condition_not_observed": (
        "Target condition has not been observed.",
        ("run the target canary",),
    ),
}


class PlanDisposition(StrEnum):
    ELIGIBLE = "eligible"
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"


@dataclass(frozen=True, order=True, slots=True)
class CanonicalInstant:
    value: datetime
    nanosecond_remainder: int
    canonical: str = field(compare=False)

    def plus(self, duration: timedelta) -> CanonicalInstant:
        if not isinstance(duration, timedelta):
            raise TypeError("canonical instant duration must be a timedelta")
        shifted = self.value + duration
        nanoseconds = shifted.microsecond * 1_000 + self.nanosecond_remainder
        whole = canonical_timestamp(shifted.replace(microsecond=0)).removesuffix("Z")
        fraction = f"{nanoseconds:09d}".rstrip("0")
        canonical = f"{whole}.{fraction}Z" if fraction else f"{whole}Z"
        return CanonicalInstant(
            shifted,
            self.nanosecond_remainder,
            canonical,
        )


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


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be nonempty trimmed text")
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
    return parse_canonical_time(value, name).canonical


def parse_canonical_time(value: object, name: str) -> CanonicalInstant:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a canonical UTC timestamp")
    matched = _CANONICAL_TIMESTAMP.fullmatch(value)
    if matched is None:
        raise ValueError(f"{name} must be a canonical UTC timestamp")
    fraction = matched.group("fraction")
    if fraction is not None and fraction.endswith("0"):
        raise ValueError(f"{name} must be a canonical UTC timestamp")
    nanoseconds = 0 if fraction is None else int(fraction.ljust(9, "0"))
    try:
        parsed = datetime(
            int(matched.group("year")),
            int(matched.group("month")),
            int(matched.group("day")),
            int(matched.group("hour")),
            int(matched.group("minute")),
            int(matched.group("second")),
            nanoseconds // 1_000,
            tzinfo=UTC,
        )
    except ValueError as error:
        raise ValueError(f"{name} must be a canonical UTC timestamp") from error
    return CanonicalInstant(parsed, nanoseconds % 1_000, value)


def datetime_instant(value: datetime, name: str) -> CanonicalInstant:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    normalized = value.astimezone(UTC)
    return CanonicalInstant(normalized, 0, canonical_timestamp(normalized))


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
    declared = [mutation_id for step in steps.values() for mutation_id in step[2]]
    if len(declared) != len(set(declared)):
        raise ValueError("each Plan mutation must belong to exactly one Plan step")
    if referenced != set(declared):
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
        granted_instant = parse_canonical_time(
            record["granted_at"], "approval grant time"
        )
        granted_at = granted_instant.canonical
        valid_value = record["valid_until"]
        valid_instant = (
            None
            if valid_value is None
            else parse_canonical_time(valid_value, "approval validity endpoint")
        )
        valid_until = None if valid_instant is None else valid_instant.canonical
        if valid_instant is not None and valid_instant <= granted_instant:
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
        if evidence_ids:
            raise ValueError(
                "Plan Assessment Evidence requires a trusted Evidence resolver"
            )
        (
            evaluated_at,
            policy_fingerprint,
            evidence_fingerprint,
            policy_rules,
        ) = _validate_evaluation(record["evaluation"], projection, evidence_ids)
        _validate_policy_inputs(record["policy_inputs"])
        for key in (
            "claims",
            "claim_qualifications",
            "claim_applicability",
            "claim_conflicts",
        ):
            items = _validate_canonical_object_array(
                record[key], name=key, primary_keys=_ASSESSMENT_SORT_KEYS[key]
            )
            if items:
                raise ValueError(f"{key} require trusted Planning Record resolvers")
        positions = _mapping(record["positions"], "assessment positions")
        _exact_keys(positions, {"support", "permission", "searches"}, "positions")
        for key in ("support", "permission", "searches"):
            items = _validate_canonical_object_array(
                positions[key],
                name=f"position {key}",
                primary_keys=_POSITION_SORT_KEYS[key],
            )
            if items:
                raise ValueError(
                    f"position {key} requires trusted Planning Record resolvers"
                )
        target_ids = projection["target_ids"]
        if not isinstance(target_ids, tuple):
            raise AssertionError("validated Plan projection targets are malformed")
        issue_ids = _validate_issues(record["issues"], target_ids)
        approval_ids = _validate_approval_references(record["approvals"])
        disposition, applicable = _validate_policy_assessment(
            record["policy_assessment"], approval_ids, policy_rules
        )
        _validate_operational_assessment(
            record["operational_assessment"], target_ids, issue_ids
        )
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
) -> tuple[str, str, str, Mapping[str, tuple[str, bool]]]:
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
            "approval_authority",
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
    input_requirements = _validate_canonical_object_array(
        policy["input_requirements"],
        name="policy inputs",
        primary_keys=(),
    )
    if input_requirements:
        raise ValueError(
            "policy input requirements require trusted Planning Record resolvers"
        )
    rules = _validate_rule_definitions(policy["rules"])
    _validate_approval_authority(policy["approval_authority"], rules)
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
    return evaluated_at, policy_fingerprint, evidence_fingerprint, rules


def _validate_rule_definitions(value: object) -> dict[str, tuple[str, bool]]:
    rules = [_mapping(item, "policy rule") for item in _sequence(value, "policy rules")]
    definitions: dict[str, tuple[str, bool]] = {}
    for rule in rules:
        rule_id = _string(rule.get("rule_id"), "policy rule ID")
        result = rule.get("result")
        overridable = rule.get("overridable")
        if result not in {"satisfied", "approval_required", "blocked"}:
            raise ValueError("policy rule result is invalid")
        if type(overridable) is not bool or (overridable and result != "blocked"):
            raise ValueError("policy rule overridability is invalid")
        _freeze(rule)
        if rule_id in definitions:
            raise ValueError("policy rule identities must be unique")
        definitions[rule_id] = (str(result), overridable)
    if tuple(definitions) != tuple(sorted(definitions)):
        raise ValueError("policy rules must be sorted by rule ID")
    return definitions


def _validate_approval_authority(
    value: object, rules: Mapping[str, tuple[str, bool]]
) -> None:
    authority = _mapping(value, "policy Approval authority")
    _exact_keys(
        authority,
        {"subject_fingerprints", "rule_ids", "override_rule_ids"},
        "policy Approval authority",
    )
    subjects = _sorted_unique_digests(
        authority["subject_fingerprints"], "authorized subject fingerprints"
    )
    ordinary = _sorted_unique_strings(
        authority["rule_ids"], "authorized Approval rule IDs"
    )
    overrides = _sorted_unique_strings(
        authority["override_rule_ids"], "authorized Override rule IDs"
    )
    if (ordinary or overrides) and not subjects:
        raise ValueError("policy Approval authority requires authorized subjects")
    if set(ordinary) & set(overrides):
        raise ValueError("policy Approval and Override rule sets must be disjoint")
    if any(rules.get(rule_id) != ("approval_required", False) for rule_id in ordinary):
        raise ValueError("policy ordinary Approval rule is not approval-required")
    if any(rules.get(rule_id) != ("blocked", True) for rule_id in overrides):
        raise ValueError("policy Override rule is not overridable-blocked")


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
        if _sorted_unique_digests(inputs[key], f"policy-input {key}"):
            raise ValueError(
                f"policy-input {key} require trusted Planning Record resolvers"
            )
    payload = {key: entry for key, entry in inputs.items() if key != "fingerprint"}
    if canonical_fingerprint(payload) != fingerprint:
        raise ValueError("policy-input fingerprint does not match")


def _validate_canonical_object_array(
    value: object,
    *,
    name: str,
    primary_keys: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    items = [_mapping(item, name) for item in _sequence(value, name)]
    encoded = [canonical_json_bytes(item) for item in items]
    if len(set(encoded)) != len(encoded):
        raise ValueError(f"{name} contains duplicate records")
    ordering: list[tuple[object, ...]] = []
    for item, canonical in zip(items, encoded, strict=True):
        ordering.append(
            tuple(_string(item.get(key), f"{name} {key}") for key in primary_keys)
            + (canonical,)
        )
    if ordering != sorted(ordering):
        raise ValueError(f"{name} must be canonically sorted")
    return tuple(items)


def _validate_issues(
    value: object, target_ids: tuple[str, ...]
) -> dict[str, tuple[str, str]]:
    issues = _validate_canonical_object_array(
        value, name="issues", primary_keys=("issue_identity",)
    )
    identities: dict[str, tuple[str, str]] = {}
    for issue in issues:
        _exact_keys(
            issue,
            {
                "issue_identity",
                "category",
                "code",
                "scope",
                "message",
                "evidence_ids",
                "next_actions",
            },
            "assessment issue",
        )
        identity = _versioned_embedded_identity(
            issue, "issue_identity", "assessment issue"
        )
        if issue["category"] not in {"observation", "execution", "policy"}:
            raise ValueError("assessment issue category is invalid")
        code = _string(issue["code"], "assessment issue code")
        supported = _SUPPORTED_ASSESSMENT_ISSUES.get(code)
        if supported is None:
            raise ValueError("assessment issue code is not supported")
        message = _text(issue["message"], "assessment issue message")
        scope = _mapping(issue["scope"], "assessment issue scope")
        _exact_keys(scope, {"kind", "id"}, "assessment issue scope")
        if scope["kind"] != "target" or scope["id"] not in target_ids:
            raise ValueError("assessment issue scope does not name a Plan target")
        if _sorted_unique_digests(
            issue["evidence_ids"], "assessment issue Evidence identities"
        ):
            raise ValueError(
                "assessment issue Evidence requires a trusted Evidence resolver"
            )
        actions = [
            _text(item, "assessment issue next action")
            for item in _sequence(
                issue["next_actions"], "assessment issue next actions"
            )
        ]
        if len(set(actions)) != len(actions):
            raise ValueError("assessment issue next actions must be unique")
        if (message, tuple(actions)) != supported:
            raise ValueError("assessment issue text does not match its stable code")
        identities[identity] = (str(scope["id"]), code)
    return identities


def _versioned_embedded_identity(
    record: Mapping[str, object], identity_field: str, name: str
) -> str:
    identity = _digest(record[identity_field], f"{name} identity")
    payload = {key: value for key, value in record.items() if key != identity_field}
    if canonical_fingerprint(payload) != identity:
        raise ValueError(f"{name} identity does not match its canonical record")
    return identity


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
    value: object,
    approval_ids: tuple[str, ...],
    policy_rules: Mapping[str, tuple[str, bool]],
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
    applicable_claims = _sorted_unique_digests(
        assessment["applicable_claim_ids"], "applicable Claim identities"
    )
    conflicts = _sorted_unique_digests(
        assessment["claim_conflict_ids"], "Claim Conflict identities"
    )
    if applicable_claims or conflicts:
        raise ValueError("Policy Assessment Claims require trusted resolvers")
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
        assessment["rule_evaluations"], set(applicable), policy_rules
    )
    expected_requirement = (
        "evaluated"
        if approval_ids
        else "missing"
        if "approval_required" in results
        else "not_required"
    )
    if requirement != expected_requirement:
        raise ValueError("Approval requirement does not match policy rules")
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


def _validate_rule_evaluations(
    value: object,
    applicable: set[str],
    policy_rules: Mapping[str, tuple[str, bool]],
) -> set[str]:
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
        rule_id = _string(evaluation["rule_id"], "rule ID")
        rule_ids.append(rule_id)
        definition = policy_rules.get(rule_id)
        if definition is None:
            raise ValueError("rule evaluation is not selected by policy")
        base_result, overridable = definition
        result = evaluation["result"]
        if result not in {"satisfied", "approval_required", "blocked"}:
            raise ValueError("rule evaluation result is invalid")
        results.add(str(result))
        for key in ("claim_ids", "claim_conflict_ids", "evidence_ids"):
            if _sorted_unique_digests(evaluation[key], f"rule {key}"):
                raise ValueError(f"rule {key} require trusted record resolvers")
        approval = evaluation["approval_identity"]
        if approval is None:
            if result != base_result:
                raise ValueError("unapproved rule result differs from selected policy")
        else:
            identity = _digest(approval, "rule Approval identity")
            if (
                result != "satisfied"
                or identity not in applicable
                or not (
                    base_result == "approval_required"
                    or (base_result == "blocked" and overridable)
                )
            ):
                raise ValueError("rule names an inapplicable Approval")
    if rule_ids != list(policy_rules):
        raise ValueError("rule evaluations must exactly cover selected policy rules")
    return results


def _validate_operational_assessment(
    value: object,
    plan_target_ids: tuple[str, ...],
    issue_targets: Mapping[str, tuple[str, str]],
) -> None:
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
    satisfactions = _validate_canonical_object_array(
        completion["step_satisfactions"],
        name="step satisfactions",
        primary_keys=("step_id", "step_fingerprint"),
    )
    if satisfactions:
        raise ValueError("Step satisfaction requires a trusted Step Evidence resolver")
    if completion["value"] != "partial":
        raise ValueError("evidence-free Plan Completion must be partial")
    targets = _validate_canonical_object_array(
        assessment["targets"],
        name="operational targets",
        primary_keys=("target_id", "plan_target_fingerprint"),
    )
    target_ids: list[str] = []
    referenced_issues: set[str] = set()
    for target in targets:
        _exact_keys(
            target,
            {
                "target_id",
                "target_kind",
                "subject_fingerprint",
                "plan_target_fingerprint",
                "completion",
                "lifecycle",
                "operational_condition",
                "issue_ids",
            },
            "operational target",
        )
        target_id = _string(target["target_id"], "operational target ID")
        target_ids.append(target_id)
        if target["target_kind"] != "external_application_installation":
            raise ValueError("operational target kind is invalid")
        _digest(target["subject_fingerprint"], "operational target subject")
        _digest(target["plan_target_fingerprint"], "operational Plan Target")
        if target["completion"] != "partial":
            raise ValueError("evidence-free target Completion must be partial")
        lifecycle_issues = _validate_unobserved_projection(
            target["lifecycle"],
            "target lifecycle",
            target_id,
            "lifecycle_not_observed",
            issue_targets,
        )
        condition_issues = _validate_unobserved_projection(
            target["operational_condition"],
            "operational condition",
            target_id,
            "condition_not_observed",
            issue_targets,
        )
        declared_issues = frozenset(
            _sorted_unique_digests(target["issue_ids"], "operational target issues")
        )
        if declared_issues != lifecycle_issues | condition_issues:
            raise ValueError("operational target issues do not match its projections")
        referenced_issues.update(declared_issues)
    if tuple(target_ids) != plan_target_ids:
        raise ValueError("operational targets must exactly project Plan targets")
    if referenced_issues != set(issue_targets):
        raise ValueError("assessment issues must be referenced by their target")
    _validate_unobserved_summary(assessment["summary"], plan_target_ids)


def _validate_unobserved_projection(
    value: object,
    name: str,
    target_id: str,
    expected_code: str,
    issue_targets: Mapping[str, tuple[str, str]],
) -> frozenset[str]:
    projection = _mapping(value, name)
    _exact_keys(projection, {"observation", "issue_ids"}, name)
    if projection["observation"] != "not_observed":
        raise ValueError(f"evidence-free {name} must be not_observed")
    references = frozenset(
        _sorted_unique_digests(projection["issue_ids"], f"{name} issue identities")
    )
    if not references or any(
        issue_targets.get(identity) != (target_id, expected_code)
        for identity in references
    ):
        raise ValueError(f"{name} issues are missing or unresolved")
    return references


def _validate_unobserved_summary(value: object, target_ids: tuple[str, ...]) -> None:
    summary = _mapping(value, "operational summary")
    _exact_keys(
        summary,
        {
            "by_completion",
            "by_lifecycle_state",
            "by_operational_condition",
            "lifecycle_not_observed",
            "lifecycle_not_applicable",
            "condition_not_observed",
            "condition_not_applicable",
        },
        "operational summary",
    )
    completion = _mapping(summary["by_completion"], "Completion summary")
    _exact_keys(completion, {"partial", "complete"}, "Completion summary")
    if _sorted_unique_strings(
        completion["partial"], "partial target IDs"
    ) != target_ids or _sorted_unique_strings(
        completion["complete"], "complete target IDs"
    ):
        raise ValueError("Completion summary does not match partial targets")
    lifecycle = _mapping(summary["by_lifecycle_state"], "lifecycle summary")
    _exact_keys(
        lifecycle,
        {"absent", "present", "transitioning", "active"},
        "lifecycle summary",
    )
    condition = _mapping(summary["by_operational_condition"], "condition summary")
    _exact_keys(
        condition,
        {"functional", "degraded", "nonfunctional"},
        "condition summary",
    )
    if any(
        _sorted_unique_strings(items, f"{name} summary target IDs")
        for name, items in (*lifecycle.items(), *condition.items())
    ):
        raise ValueError("evidence-free observed summary buckets must be empty")
    if (
        _sorted_unique_strings(
            summary["lifecycle_not_observed"], "unobserved lifecycle target IDs"
        )
        != target_ids
        or _sorted_unique_strings(
            summary["condition_not_observed"], "unobserved condition target IDs"
        )
        != target_ids
        or _sorted_unique_strings(
            summary["lifecycle_not_applicable"],
            "inapplicable lifecycle target IDs",
        )
        or _sorted_unique_strings(
            summary["condition_not_applicable"],
            "inapplicable condition target IDs",
        )
    ):
        raise ValueError("unobserved summary does not exactly partition targets")


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
