"""Supported-v1 guided setup and product removal plans.

The planner is deliberately side-effect free.  Interfaces show and edit its
exact plan, while the Supervisor executes steps and persists the returned
evidence.  This keeps interactive and noninteractive setup on one resumable
contract.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import StrEnum
from ipaddress import ip_address
from types import MappingProxyType
from typing import Callable, Mapping, Sequence
from urllib.parse import urlsplit

from mlxctl.domain.resources import ActivationPolicy, ResourceName


class StepState(StrEnum):
    READY = "ready"
    COMPLETE = "complete"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class SetupPreflight:
    platform: str
    machine: str
    memory_bytes: int
    disk_free_bytes: int
    online: bool


@dataclass(frozen=True, slots=True)
class ExactSetupSelection:
    runtime_name: str
    runtime_version: str
    runtime_lock_digest: str
    model_repository: str
    model_revision: str
    trust_grants: tuple[str, ...] | None
    service_name: str
    gateway_endpoint: str
    model_alias: str | None = None
    service_route: str | None = None
    activation: str = "manual"
    pinned: bool = False
    service_options: Mapping[str, object] = field(default_factory=dict)
    clients: tuple[str, ...] = ()
    sampling_profiles: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    context_window: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_alias", self.model_alias or self.service_name)
        object.__setattr__(
            self, "service_route", self.service_route or self.service_name
        )
        object.__setattr__(self, "clients", tuple(self.clients))
        if self.trust_grants is not None:
            object.__setattr__(self, "trust_grants", tuple(self.trust_grants))
        object.__setattr__(
            self,
            "sampling_profiles",
            MappingProxyType(
                {
                    str(name): MappingProxyType(dict(settings))
                    for name, settings in self.sampling_profiles.items()
                }
            ),
        )
        if not isinstance(self.service_options, Mapping):
            raise ValueError("service_options must be a JSON-like object")
        object.__setattr__(
            self,
            "service_options",
            _freeze_json_mapping(self.service_options, "service_options"),
        )

    def validate_exact(self) -> None:
        required = {
            "runtime_name": self.runtime_name,
            "runtime_version": self.runtime_version,
            "runtime_lock_digest": self.runtime_lock_digest,
            "model_repository": self.model_repository,
            "model_revision": self.model_revision,
            "service_name": self.service_name,
            "gateway_endpoint": self.gateway_endpoint,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"exact setup selection requires {', '.join(missing)}")
        if self.trust_grants is None:
            raise ValueError("exact setup selection requires explicit trust_grants")
        for name in (self.service_name, self.model_alias, self.service_route):
            ResourceName(name or "")
        try:
            ActivationPolicy(self.activation)
        except (TypeError, ValueError) as error:
            raise ValueError("activation must be manual or supervisor") from error
        if type(self.pinned) is not bool:
            raise ValueError("pinned must be boolean")
        lock_algorithm, separator, lock_value = self.runtime_lock_digest.partition(":")
        if (
            separator != ":"
            or lock_algorithm != "sha256"
            or len(lock_value) != 64
            or any(character not in "0123456789abcdef" for character in lock_value)
        ):
            raise ValueError("runtime_lock_digest must be an exact sha256 digest")
        if len(self.model_revision) not in {40, 64}:
            raise ValueError("model_revision must be an exact commit or content digest")
        if any(
            character not in "0123456789abcdef" for character in self.model_revision
        ):
            raise ValueError("model_revision must be lowercase hexadecimal")
        endpoint = urlsplit(self.gateway_endpoint)
        try:
            address = ip_address(endpoint.hostname or "")
            port = endpoint.port
        except ValueError as error:
            raise ValueError(
                "gateway_endpoint must be a literal HTTP loopback URL"
            ) from error
        if (
            endpoint.scheme != "http"
            or not address.is_loopback
            or port is None
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("gateway_endpoint must be a literal HTTP loopback URL")


@dataclass(frozen=True, slots=True)
class RecommendedProfile:
    name: str
    minimum_memory_bytes: int
    selection: ExactSetupSelection
    minimum_disk_bytes: int = 0


@dataclass(frozen=True, slots=True)
class SetupRequest:
    selection: ExactSetupSelection | None = None
    noninteractive: bool = False
    confirmed: bool = False


@dataclass(frozen=True, slots=True)
class PlanStep:
    id: str
    title: str
    inputs: Mapping[str, object]
    fingerprint: str
    state: StepState
    reason: str = ""
    network_required: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", MappingProxyType(dict(self.inputs)))


@dataclass(frozen=True, slots=True)
class SetupEvidence:
    step_id: str
    fingerprint: str
    state: StepState
    detail: str = ""

    @classmethod
    def complete(cls, step: PlanStep, detail: str = "") -> SetupEvidence:
        return cls(step.id, step.fingerprint, StepState.COMPLETE, detail)


@dataclass(frozen=True, slots=True)
class SetupPlan:
    profile_name: str
    selection: ExactSetupSelection
    preflight: SetupPreflight
    steps: tuple[PlanStep, ...]
    offline: bool
    editable: bool
    confirmation_required: bool


@dataclass(frozen=True, slots=True)
class SetupPreview:
    profile_name: str
    editable: bool
    runtime: str
    runtime_lock_digest: str
    model_repository: str
    model_revision: str
    trust_grants: tuple[str, ...]
    service_name: str
    model_alias: str
    service_route: str
    activation: str
    pinned: bool
    service_options: Mapping[str, object]
    gateway_endpoint: str
    clients: tuple[str, ...]
    sampling_profiles: Mapping[str, Mapping[str, object]]
    context_window: int | None
    steps: tuple[PlanStep, ...]
    offline_note: str


@dataclass(frozen=True, slots=True)
class PlanExecutionResult:
    evidence: tuple[SetupEvidence, ...]
    complete: bool


class PlanExecutionError(RuntimeError):
    def __init__(self, step_id: str, message: str) -> None:
        super().__init__(f"{step_id}: {message}")
        self.step_id = step_id


@dataclass(frozen=True, slots=True)
class RemovalInventory:
    running_services: tuple[str, ...] = ()
    registered: bool = False
    client_integrations: tuple[str, ...] = ()
    product_owned_paths: tuple[str, ...] = ()
    product_owned_bytes: int = 0
    shared_cache_paths: tuple[str, ...] = ()
    shared_cache_bytes: int = 0
    references: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    unrelated_settings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "running_services", tuple(self.running_services))
        object.__setattr__(self, "client_integrations", tuple(self.client_integrations))
        object.__setattr__(self, "product_owned_paths", tuple(self.product_owned_paths))
        object.__setattr__(self, "shared_cache_paths", tuple(self.shared_cache_paths))
        object.__setattr__(self, "unrelated_settings", tuple(self.unrelated_settings))
        object.__setattr__(
            self,
            "references",
            MappingProxyType(
                {str(key): tuple(value) for key, value in self.references.items()}
            ),
        )


@dataclass(frozen=True, slots=True)
class RemovalPlan:
    steps: tuple[PlanStep, ...]
    references: Mapping[str, tuple[str, ...]]
    freed_bytes_estimate: int
    retained_paths: tuple[str, ...]
    retained_bytes_estimate: int
    retained_settings: tuple[str, ...]


StepExecutor = Callable[[PlanStep], SetupEvidence]
EvidenceRecorder = Callable[[SetupEvidence], object]


class SetupPlanner:
    """Create exact guided, unattended, resume, and removal plans."""

    def __init__(self, recommended_profiles: Sequence[RecommendedProfile]) -> None:
        profiles = tuple(
            sorted(recommended_profiles, key=lambda item: item.minimum_memory_bytes)
        )
        if not profiles:
            raise ValueError("at least one recommended profile is required")
        for profile in profiles:
            profile.selection.validate_exact()
        self._profiles = profiles

    @property
    def expert_template(self) -> ExactSetupSelection:
        """Return an editable shape, never an implicit machine recommendation."""

        return self._profiles[0].selection

    def plan(
        self,
        preflight: SetupPreflight,
        request: SetupRequest | None = None,
        *,
        evidence: Sequence[SetupEvidence] = (),
    ) -> SetupPlan:
        self._validate_machine(preflight)
        request = request or SetupRequest()
        if request.selection is None:
            profile = self._recommend(preflight.memory_bytes, preflight.disk_free_bytes)
            selection = profile.selection
        else:
            profile = None
            selection = request.selection
        selection.validate_exact()
        if request.noninteractive:
            if request.selection is None:
                raise ValueError(
                    "noninteractive setup requires an explicit exact selection"
                )
            if not request.confirmed:
                raise ValueError("noninteractive setup must be explicitly confirmed")

        evidence_by_step = {item.step_id: item for item in evidence}
        specifications = self._setup_specs(preflight, selection)
        steps: list[PlanStep] = []
        dependency_blocked = False
        for step_id, title, inputs, network_required in specifications:
            fingerprint = _fingerprint(step_id, inputs)
            prior = evidence_by_step.get(step_id)
            if (
                prior is not None
                and prior.state is StepState.COMPLETE
                and prior.fingerprint == fingerprint
            ):
                state = StepState.COMPLETE
                reason = "Matching completion evidence is present."
            elif dependency_blocked:
                state = StepState.BLOCKED
                reason = "A required earlier step is blocked."
            elif network_required and not preflight.online:
                state = StepState.BLOCKED
                reason = "The machine is offline and no matching completion evidence is present."
                dependency_blocked = True
            else:
                state = StepState.READY
                reason = ""
            steps.append(
                PlanStep(
                    step_id, title, inputs, fingerprint, state, reason, network_required
                )
            )

        return SetupPlan(
            profile_name=profile.name if profile is not None else "custom",
            selection=selection,
            preflight=preflight,
            steps=tuple(steps),
            offline=not preflight.online,
            editable=not request.noninteractive,
            confirmation_required=not request.confirmed,
        )

    def preview(self, plan: SetupPlan) -> SetupPreview:
        selection = plan.selection
        return SetupPreview(
            profile_name=plan.profile_name,
            editable=plan.editable,
            runtime=f"{selection.runtime_name}=={selection.runtime_version}",
            runtime_lock_digest=selection.runtime_lock_digest,
            model_repository=selection.model_repository,
            model_revision=selection.model_revision,
            trust_grants=selection.trust_grants or (),
            service_name=selection.service_name,
            model_alias=selection.model_alias or selection.service_name,
            service_route=selection.service_route or selection.service_name,
            activation=selection.activation,
            pinned=selection.pinned,
            service_options=selection.service_options,
            gateway_endpoint=selection.gateway_endpoint,
            clients=selection.clients,
            sampling_profiles=selection.sampling_profiles,
            context_window=selection.context_window,
            steps=plan.steps,
            offline_note=(
                "No completed evidence can be assumed while offline; network artifacts without matching evidence are blocked."
                if plan.offline
                else "Online preflight succeeded."
            ),
        )

    def apply(
        self,
        plan: SetupPlan,
        execute: StepExecutor,
        *,
        evidence: Sequence[SetupEvidence] = (),
        record: EvidenceRecorder | None = None,
    ) -> PlanExecutionResult:
        known = {(item.step_id, item.fingerprint): item for item in evidence}
        ordered = list(evidence)
        for step in plan.steps:
            prior = known.get((step.id, step.fingerprint))
            if step.state is StepState.COMPLETE and prior is None:
                prior = SetupEvidence.complete(step)
                known[(step.id, step.fingerprint)] = prior
                ordered.append(prior)
            if prior is not None and prior.state is StepState.COMPLETE:
                continue
            if step.state is StepState.BLOCKED:
                raise PlanExecutionError(step.id, step.reason)
            try:
                completed = execute(step)
            except Exception as error:
                raise PlanExecutionError(step.id, str(error)) from error
            if (
                completed.step_id != step.id
                or completed.fingerprint != step.fingerprint
                or completed.state is not StepState.COMPLETE
            ):
                raise PlanExecutionError(
                    step.id, "executor returned invalid completion evidence"
                )
            known[(step.id, step.fingerprint)] = completed
            ordered.append(completed)
            if record is not None:
                record(completed)
        return PlanExecutionResult(
            tuple(ordered),
            all(step.state is not StepState.BLOCKED for step in plan.steps),
        )

    def plan_removal(self, inventory: RemovalInventory) -> RemovalPlan:
        specs: list[tuple[str, str, Mapping[str, object]]] = []
        if inventory.running_services:
            specs.extend(
                (
                    (
                        "service.drain",
                        "Drain running Inference Services",
                        {"services": inventory.running_services},
                    ),
                    (
                        "service.stop",
                        "Stop running Inference Services",
                        {"services": inventory.running_services},
                    ),
                )
            )
        if inventory.registered:
            specs.append(("supervisor.unregister", "Unregister the Supervisor", {}))
        if inventory.client_integrations:
            specs.append(
                (
                    "client.remove",
                    "Remove only mlxctl-owned client fields",
                    {"clients": inventory.client_integrations},
                )
            )
        if inventory.product_owned_paths:
            specs.append(
                (
                    "state.remove",
                    "Remove product-owned state",
                    {"paths": inventory.product_owned_paths},
                )
            )
        steps = tuple(
            PlanStep(
                step_id, title, inputs, _fingerprint(step_id, inputs), StepState.READY
            )
            for step_id, title, inputs in specs
        )
        return RemovalPlan(
            steps=steps,
            references=inventory.references,
            freed_bytes_estimate=inventory.product_owned_bytes,
            retained_paths=inventory.shared_cache_paths,
            retained_bytes_estimate=inventory.shared_cache_bytes,
            retained_settings=inventory.unrelated_settings,
        )

    def apply_removal(
        self,
        plan: RemovalPlan,
        execute: StepExecutor,
        *,
        evidence: Sequence[SetupEvidence] = (),
        record: EvidenceRecorder | None = None,
    ) -> PlanExecutionResult:
        synthetic = SetupPlan(
            profile_name="removal",
            selection=self._profiles[0].selection,
            preflight=SetupPreflight("darwin", "arm64", 0, 0, True),
            steps=plan.steps,
            offline=False,
            editable=False,
            confirmation_required=True,
        )
        return self.apply(synthetic, execute, evidence=evidence, record=record)

    def _recommend(self, memory_bytes: int, disk_free_bytes: int) -> RecommendedProfile:
        eligible = [
            profile
            for profile in self._profiles
            if profile.minimum_memory_bytes <= memory_bytes
            and profile.minimum_disk_bytes <= disk_free_bytes
        ]
        if not eligible:
            smallest = self._profiles[0]
            raise ValueError(
                "no recommended setup profile fits this Mac: "
                f"{smallest.name!r} requires at least "
                f"{smallest.minimum_memory_bytes} bytes of memory and "
                f"{smallest.minimum_disk_bytes} bytes of free disk; "
                "use expert setup to select a smaller exact model"
            )
        return eligible[-1]

    @staticmethod
    def _validate_machine(preflight: SetupPreflight) -> None:
        if preflight.platform != "darwin" or preflight.machine != "arm64":
            raise ValueError("mlxctl setup requires an Apple-silicon Mac")
        if preflight.memory_bytes <= 0 or preflight.disk_free_bytes < 0:
            raise ValueError("preflight memory and disk facts must be nonnegative")

    @staticmethod
    def _setup_specs(
        preflight: SetupPreflight, selection: ExactSetupSelection
    ) -> tuple[tuple[str, str, Mapping[str, object], bool], ...]:
        common = {
            "runtime": selection.runtime_name,
            "runtime_version": selection.runtime_version,
            "model_repository": selection.model_repository,
            "model_revision": selection.model_revision,
            "service": selection.service_name,
            "model_alias": selection.model_alias,
            "route": selection.service_route,
            "activation": selection.activation,
            "pinned": selection.pinned,
            "options": selection.service_options,
        }
        return (
            (
                "preflight",
                "Validate this Apple-silicon Mac",
                {
                    "platform": preflight.platform,
                    "machine": preflight.machine,
                    "memory_bytes": preflight.memory_bytes,
                    "disk_free_bytes": preflight.disk_free_bytes,
                },
                False,
            ),
            (
                "runtime.install",
                "Install and probe the exact Runtime Installation",
                {
                    "name": selection.runtime_name,
                    "version": selection.runtime_version,
                    "lock_digest": selection.runtime_lock_digest,
                },
                True,
            ),
            (
                "model.install",
                "Install and verify the exact Model Revision",
                {
                    "repository": selection.model_repository,
                    "revision": selection.model_revision,
                    "alias": selection.model_alias,
                    "trust_grants": selection.trust_grants,
                },
                True,
            ),
            ("service.configure", "Configure the Inference Service", common, False),
            (
                "gateway.configure",
                "Configure the stable Gateway route",
                {
                    "endpoint": selection.gateway_endpoint,
                    "service": selection.service_name,
                    "route": selection.service_route,
                },
                False,
            ),
            (
                "client.configure",
                "Configure selected clients",
                {
                    "clients": selection.clients,
                    "service": selection.service_name,
                    "route": selection.service_route,
                    "endpoint": selection.gateway_endpoint,
                    "sampling_profiles": selection.sampling_profiles,
                },
                False,
            ),
            ("service.start", "Start the Inference Service", common, False),
            (
                "verify.request",
                "Send the first real inference request through the Gateway",
                {
                    "endpoint": selection.gateway_endpoint,
                    "model": selection.service_route,
                    "request": "Respond with exactly: mlxctl ready",
                },
                False,
            ),
        )


def _fingerprint(step_id: str, inputs: Mapping[str, object]) -> str:
    payload = json.dumps(
        {"step": step_id, "inputs": inputs},
        default=_json_default,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _json_default(value: object) -> object:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"unsupported plan value: {type(value).__name__}")


def _freeze_json_mapping(
    value: Mapping[str, object], scope: str
) -> Mapping[str, object]:
    frozen: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{scope} keys must be strings")
        frozen[key] = _freeze_json_value(item, f"{scope}.{key}")
    return MappingProxyType(frozen)


def _freeze_json_value(value: object, scope: str) -> object:
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if math.isfinite(value):
            return value
        raise ValueError(f"{scope} must be finite")
    if isinstance(value, Mapping):
        return _freeze_json_mapping(value, scope)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json_value(item, f"{scope}[{index}]")
            for index, item in enumerate(value)
        )
    raise ValueError(f"{scope} contains a non-JSON value")
