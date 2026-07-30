import hashlib
import json
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import fields, replace
from pathlib import Path

from mastic.application.application_targets import (
    APPLICATION_CANARY_CONTRACTS,
    application_canary_evidence_sha256,
)
from mastic.application.dispatch import ApplicationError
from mastic.application.setup import (
    CapacityProfile,
    ExactSetupSelection,
    RecommendedProfile,
    RemovalInventory,
    SetupEvidence,
    SetupIntent,
    SetupResolver,
    SetupPreflight,
    StepState,
)
from mastic.application.setup_operation import (
    ActivateSupervisor,
    ConfigureApplicationTarget,
    ConfigureGateway,
    ConfigureService,
    DrainService,
    InstallApplications,
    InstallModel,
    InstallRuntime,
    RemoveApplications,
    RemoveApplicationTarget,
    RemoveState,
    SetupNestedOperations,
    SetupOperation,
    StartService,
    StopService,
    TestApplicationTarget,
    TrustModel,
    UnregisterSupervisor,
    VerifyGatewayRequest,
    combined_readiness,
)
from mastic.infrastructure.state_store import OperationalStateStore
from mastic.infrastructure.setup_port import (
    DurableSetupOutcomeProvider,
    OperationalSetupEvidenceStore,
    OperationalSetupPlanStore,
)


GIB = 1024**3
MODEL_REPOSITORY = "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit"
MODEL_REVISION = "70a3aa32c7feef511182bf16aa332f37e8d82014"
SELECTION_SHA256 = "7316e2d9b7271228199254ed30b0d89f243d4ad821502fbbc074c5a9654f5f60"
READY_RESPONSE_SHA256 = hashlib.sha256(b"mastic ready").hexdigest()


class FakeOwner:
    def __init__(self, results=None, *, fail=None):
        self.calls = []
        self.results = dict(results or {})
        self.fail = fail

    def execute(self, operation, parameters):
        self.calls.append((operation, dict(parameters)))
        if operation == self.fail:
            raise RuntimeError(f"{operation} interrupted")
        result = self.results.get(operation, {})
        return dict(result(parameters) if callable(result) else result)

    def execute_typed(self, result_key, operation):
        self.calls.append(operation)
        if result_key == self.fail:
            raise RuntimeError(f"{result_key} interrupted")
        result = self.results.get(result_key, {})
        parameters = {
            field.name: getattr(operation, field.name) for field in fields(operation)
        }
        return dict(result(parameters) if callable(result) else result)


class FakeSetupCapabilities:
    def __init__(
        self,
        *,
        runtime,
        model,
        config,
        applications,
        application_targets,
        supervisor,
        verifier,
    ):
        self.runtime = runtime
        self.model = model
        self.config = config
        self.applications = applications
        self.application_targets = application_targets
        self.supervisor = supervisor
        self.verifier = verifier

    def activate_supervisor(self, operation: ActivateSupervisor):
        return self.supervisor.execute_typed("supervisor.start", operation)

    def install_runtime(self, operation: InstallRuntime):
        return self.runtime.execute_typed("runtime.install", operation)

    def install_model(self, operation: InstallModel):
        return self.model.execute_typed("model.install", operation)

    def trust_model(self, operation: TrustModel):
        return self.config.execute_typed("model.trust", operation)

    def configure_service(self, operation: ConfigureService):
        return self.config.execute_typed("service.create", operation)

    def configure_gateway(self, operation: ConfigureGateway):
        return self.config.execute_typed("gateway.configure", operation)

    def install_applications(self, operation: InstallApplications):
        return self.applications.execute_typed("application.install", operation)

    def configure_application_target(self, operation: ConfigureApplicationTarget):
        return self.application_targets.execute_typed(
            "application-target.configure", operation
        )

    def start_service(self, operation: StartService):
        return self.supervisor.execute_typed("service.start", operation)

    def test_application_target(self, operation: TestApplicationTarget):
        return self.application_targets.execute_typed(
            "application-target.test", operation
        )

    def verify_gateway(self, operation: VerifyGatewayRequest):
        return self.verifier.execute_typed("verify.request", operation)

    def drain_service(self, operation: DrainService):
        return self.supervisor.execute_typed("service.drain", operation)

    def stop_service(self, operation: StopService):
        return self.supervisor.execute_typed("service.stop", operation)

    def unregister_supervisor(self, operation: UnregisterSupervisor):
        return self.supervisor.execute_typed("supervisor.unregister", operation)

    def remove_application_target(self, operation: RemoveApplicationTarget):
        return self.application_targets.execute_typed(
            "application-target.remove", operation
        )

    def remove_applications(self, operation: RemoveApplications):
        return self.applications.execute_typed("application.remove", operation)

    def remove_state(self, operation: RemoveState):
        return self.config.execute_typed("state.remove", operation)


class FakeEvidenceStore:
    def __init__(self):
        self.items = {"setup": [], "removal": []}

    def load(self, scope):
        return tuple(self.items[scope])

    def record(self, scope, evidence):
        self.items[scope].append(evidence)


class FakePlanStore:
    def __init__(self):
        self.plan = None
        self.calls_before_record = None

    def record(self, plan):
        self.plan = dict(plan)

    def load(self):
        return self.plan


class FakeOperationalState:
    def __init__(self):
        self.rows = []

    def put_snapshot(self, snapshot):
        self.rows.append(dict(snapshot))
        return dict(snapshot)

    def snapshots(self, kind):
        return tuple(row for row in self.rows if row["kind"] == kind)

    def snapshot_history(self, kind):
        return self.snapshots(kind)


def selection(*, revision=MODEL_REVISION, trust=()):
    return ExactSetupSelection(
        runtime_name="optiq",
        runtime_version="0.3.3",
        runtime_lock_digest="sha256:" + "a" * 64,
        model_repository=MODEL_REPOSITORY,
        model_revision=revision,
        trust_grants=trust,
        service_name="coding",
        model_alias="qwen-optiq",
        service_route="engineering",
        activation="supervisor",
        pinned=True,
        service_options={
            "kv_config": "kv_config.json",
            "mtp": True,
            "runtime": {"draft_tokens": 4},
        },
        gateway_endpoint="http://127.0.0.1:8766/v1",
        application_targets=("codex", "hindsight"),
        application_target_options={"hindsight": {"profile": "default"}},
        context_window=32768,
    )


def validated_performance_profile(*, plan_sha256: str) -> dict[str, object]:
    return {
        "id": "phase1-qwen36-optiq-apple-silicon",
        "version": 1,
        "status": "validated",
        "host": {
            "platform": "darwin",
            "machine": "arm64",
            "minimum_memory_bytes": 48 * GIB,
            "macos_major_versions": [15, 26],
        },
        "plan": {
            "selection_sha256": plan_sha256,
            "application_versions": {"codex": "0.144.1", "hindsight": "0.8.4"},
        },
        "metrics": {
            "codex.native_canary.duration_seconds": {
                "unit": "seconds",
                "expected": {"maximum": 60.0},
                "degraded": {"minimum_exclusive": 60.0},
            },
            "hindsight.native_canary.duration_seconds": {
                "unit": "seconds",
                "expected": {"maximum": 180.0},
                "degraded": {"minimum_exclusive": 180.0},
            },
        },
    }


def canary_phases(target: str) -> list[str]:
    return list(APPLICATION_CANARY_CONTRACTS[target].phases)


def canary_evidence_sha256(target: str, *, service: str = "coding") -> str:
    contract = APPLICATION_CANARY_CONTRACTS[target]
    return application_canary_evidence_sha256(
        target=target,
        profile=contract.profile,
        service=service,
        phases=contract.phases,
        exact_contract=True,
    )


class SetupOperationTests(unittest.TestCase):
    def setUp(self):
        compact = RecommendedProfile("compact", 16 * GIB, selection(revision="1" * 40))
        workstation = RecommendedProfile("workstation", 64 * GIB, selection())
        capacities = (
            CapacityProfile(
                "balanced",
                "Balanced",
                131_072,
                6,
                5_737_807_872,
                2 * GIB,
                "Parallel work.",
            ),
            CapacityProfile(
                "long-context",
                "Long context",
                196_608,
                4,
                5_737_807_872,
                2 * GIB,
                "Larger requests.",
            ),
        )
        self.resolver = SetupResolver(
            (compact, workstation),
            capacity_profiles=capacities,
        )
        self.facts = SetupPreflight(
            "darwin", "arm64", 96 * GIB, 500 * GIB, True, os_version="26.5"
        )
        self.runtime = FakeOwner(
            {
                "runtime.install": {
                    "installation_id": "optiq-0.3.3-tested",
                    "runtime": "optiq",
                    "version": "0.3.3",
                    "provenance": "tested",
                    "bundle_id": "optiq-0.3.3-py3.13-macos-arm64",
                    "lock_sha256": "a" * 64,
                }
            }
        )
        self.model = FakeOwner(
            {
                "model.install": {
                    "installation_id": "qwen-optiq@" + MODEL_REVISION,
                    "alias": "coding",
                    "revision": MODEL_REVISION,
                }
            }
        )
        self.config = FakeOwner()
        self.applications = FakeOwner(
            {
                "application.install": {
                    "applications": {
                        "codex": {
                            "version": "0.150.0",
                            "release_intent": "current",
                            "release_channel": "npm:latest",
                            "provenance": "reconciled",
                        },
                        "hindsight": {
                            "version": "0.8.4",
                            "provenance": "installed",
                        },
                    }
                }
            }
        )
        self.application_targets = FakeOwner(
            {
                "application-target.test": lambda parameters: {
                    "profile": parameters["profile"],
                    "response": {
                        "ok": True,
                        "exact_contract": True,
                        "duration_seconds": 12.0,
                        "phases": canary_phases(parameters["application_target"]),
                        "evidence_sha256": canary_evidence_sha256(
                            parameters["application_target"]
                        ),
                    },
                }
            }
        )
        self.supervisor = FakeOwner()
        self.verifier = FakeOwner(
            {"verify.request": {"ok": True, "text": "mastic ready"}}
        )
        self.evidence = FakeEvidenceStore()
        self.inventory = RemovalInventory(
            running_services=("coding",),
            registered=True,
            application_target_integrations=("codex", "hindsight"),
            product_owned_paths=("~/.config/mastic", "~/.local/state/mastic"),
            product_owned_bytes=2 * GIB,
            shared_cache_paths=("~/.cache/huggingface/hub/models--qwen",),
            shared_cache_bytes=40 * GIB,
            unrelated_settings=("Codex theme", "Hindsight bank ID"),
        )

    def test_codex_canary_evidence_contract_digest_is_pinned(self) -> None:
        # Changing this digest requires an intentional evidence-contract update.
        self.assertEqual(
            canary_evidence_sha256("codex"),
            "b6907b0ba664ab5b09aeaa80ef7361821b20e8bc1f4fc6413cdf9e0b7dc584d0",
        )

    def port(
        self,
        *,
        resolver=None,
        preflight=None,
        model=None,
        facts=None,
        performance_profile=None,
        applications=None,
        config=None,
        evidence=None,
        inventory=None,
        transition=None,
        removal_transition=None,
        plan_store=None,
    ):
        selected_model = self.model if model is None else model
        selected_config = self.config if config is None else config
        selected_applications = (
            self.applications if applications is None else applications
        )
        selected_facts = self.facts if facts is None else facts
        capabilities = FakeSetupCapabilities(
            runtime=self.runtime,
            model=selected_model,
            config=selected_config,
            applications=selected_applications,
            application_targets=self.application_targets,
            supervisor=self.supervisor,
            verifier=self.verifier,
        )
        return SetupOperation(
            self.resolver if resolver is None else resolver,
            preflight=preflight
            if preflight is not None
            else (
                lambda offline: replace(
                    selected_facts,
                    online=selected_facts.online and not offline,
                )
            ),
            nested_operations=SetupNestedOperations(
                desired_state=capabilities,
                runtime_supply=capabilities,
                model_supply=capabilities,
                application_lifecycle=capabilities,
                application_configuration=capabilities,
                native_canaries=capabilities,
                service_lifecycle=capabilities,
                gateway_verification=capabilities,
                state_removal=capabilities,
            ),
            evidence=self.evidence if evidence is None else evidence,
            removal_inventory=lambda: (
                self.inventory if inventory is None else inventory
            ),
            performance_profile=performance_profile,
            transition=transition,
            removal_transition=removal_transition,
            plan_store=plan_store,
        )

    def test_confirmed_setup_records_content_free_exact_plan_before_mutation(self):
        plan_store = FakePlanStore()

        def record(plan):
            plan_store.calls_before_record = [
                *self.runtime.calls,
                *self.model.calls,
                *self.config.calls,
                *self.applications.calls,
                *self.application_targets.calls,
                *self.supervisor.calls,
                *self.verifier.calls,
            ]
            plan_store.plan = dict(plan)

        plan_store.record = record
        port = self.port(plan_store=plan_store)
        preview = port.preview({})

        port.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )

        self.assertEqual(plan_store.calls_before_record, [])
        self.assertEqual(
            plan_store.plan["plan_identity"], preview["preview_fingerprint"]
        )
        self.assertEqual(plan_store.plan["application_targets"], ("codex", "hindsight"))
        self.assertTrue(plan_store.plan["steps"])
        for step in plan_store.plan["steps"]:
            with self.subTest(step=step.get("id")):
                self.assertLessEqual({"id", "fingerprint", "state"}, set(step))
                self.assertLessEqual(
                    set(step),
                    {"id", "fingerprint", "state", "expected_result"},
                )
        material_contracts = {
            step["id"]: step["expected_result"]
            for step in plan_store.plan["steps"]
            if "expected_result" in step
        }
        self.assertEqual(
            material_contracts,
            {
                "runtime.install": {
                    "runtime": "optiq",
                    "version": "0.3.3",
                    "provenance": "tested",
                    "lock_sha256": "a" * 64,
                },
                "model.install": {"revision": MODEL_REVISION},
            },
        )
        self.assertEqual(
            set(plan_store.plan),
            {
                "plan_identity",
                "steps",
                "application_targets",
                "performance_binding",
            },
        )
        self.assertEqual(
            set(plan_store.plan["performance_binding"]),
            {
                "selection_sha256",
                "application_versions",
                "platform",
                "machine",
                "memory_bytes",
                "macos_major",
                "service",
            },
        )
        encoded = json.dumps(plan_store.plan)
        for forbidden in (
            "prompt",
            "messages",
            "credentials",
            "model_repository",
            MODEL_REPOSITORY,
        ):
            self.assertNotIn(forbidden, encoded)

    def test_durable_outcome_survives_store_recomposition_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            state = OperationalStateStore(state_path)
            evidence = OperationalSetupEvidenceStore(state)
            plans = OperationalSetupPlanStore(state)
            port = self.port(evidence=evidence, plan_store=plans)
            preview = port.preview({})
            result = port.execute(
                "setup",
                {
                    "confirmed": True,
                    "preview_fingerprint": preview["preview_fingerprint"],
                },
            )

            reopened = OperationalStateStore(state_path)
            outcome = DurableSetupOutcomeProvider(
                OperationalSetupPlanStore(reopened),
                OperationalSetupEvidenceStore(reopened),
            ).outcome()

            self.assertEqual(outcome["completion"], result["completion"])
            self.assertEqual(outcome["readiness"], result["readiness"])
            self.assertEqual(
                outcome["application_target_readiness"],
                result["application_target_readiness"],
            )
            stored = reopened.snapshot("setup_plan", "active")
            self.assertEqual(
                set(stored),
                {
                    "kind",
                    "id",
                    "version",
                    "plan_identity",
                    "steps",
                    "application_targets",
                    "performance_binding",
                },
            )

            malformed = FakePlanStore()
            malformed.plan = {
                **stored,
                "steps": [{"id": "application.canary.codex"}],
            }
            conservative = DurableSetupOutcomeProvider(
                malformed, OperationalSetupEvidenceStore(reopened)
            ).outcome()
            self.assertEqual(conservative["completion"], "partial")
            self.assertEqual(conservative["readiness"], "unverified")

    def test_durable_outcome_keeps_missing_and_malformed_canaries_fail_closed(self):
        plans = FakePlanStore()
        plans.plan = {
            "plan_identity": "a" * 64,
            "steps": (
                {"id": "preflight", "fingerprint": "preflight-v1"},
                {"id": "application.canary.codex", "fingerprint": "canary-v1"},
            ),
            "application_targets": ("codex",),
        }
        evidence = FakeEvidenceStore()
        evidence.record(
            "setup",
            SetupEvidence("preflight", "preflight-v1", StepState.COMPLETE, "{}"),
        )
        provider = DurableSetupOutcomeProvider(plans, evidence)

        missing = provider.outcome()

        self.assertEqual(missing["completion"], "partial")
        self.assertEqual(missing["readiness"], "pending")
        self.assertEqual(missing["application_target_readiness"], {"codex": "pending"})

        evidence.record(
            "setup",
            SetupEvidence(
                "application.canary.codex",
                "canary-v1",
                StepState.COMPLETE,
                '{"result":{"performance":{"band":"expected"}}}',
            ),
        )
        malformed = provider.outcome()

        self.assertEqual(malformed["completion"], "partial")
        self.assertEqual(malformed["readiness"], "unverified")
        self.assertEqual(
            malformed["application_target_readiness"], {"codex": "unverified"}
        )

    def test_durable_outcome_reobserves_every_selected_application_target(self):
        plans = FakePlanStore()
        plans.plan = {
            "plan_identity": "a" * 64,
            "steps": (
                {
                    "id": "application.canary.codex",
                    "fingerprint": "codex-v1",
                    "state": "skipped",
                },
                {
                    "id": "application.canary.hindsight",
                    "fingerprint": "hindsight-v1",
                    "state": "skipped",
                },
            ),
            "application_targets": ("codex", "hindsight"),
            "performance_binding": {
                "selection_sha256": SELECTION_SHA256,
                "application_versions": {
                    "codex": "0.144.1",
                    "hindsight": "0.8.4",
                },
                "platform": "darwin",
                "machine": "arm64",
                "memory_bytes": 96 * GIB,
                "macos_major": 26,
                "service": "coding",
            },
        }
        evidence = FakeEvidenceStore()
        for target in ("codex", "hindsight"):
            evidence.record(
                "setup",
                SetupEvidence(
                    f"application.canary.{target}",
                    f"{target}-v1",
                    StepState.SKIPPED,
                    "",
                ),
            )
        inspections = FakeOwner(
            {
                "application-target.inspect": lambda parameters: {
                    "state": (
                        "healthy"
                        if parameters["application_target"] == "codex"
                        else "drifted"
                    ),
                    "detail": "managed state changed",
                    "next_actions": [
                        "mastic application-target configure hindsight --help"
                    ],
                    "credential": "must not escape the inspection boundary",
                }
            }
        )

        outcome = DurableSetupOutcomeProvider(
            plans, evidence, application_targets=inspections
        ).outcome()

        self.assertEqual(
            inspections.calls,
            [
                (
                    "application-target.inspect",
                    {"application_target": "codex"},
                ),
                (
                    "application-target.inspect",
                    {"application_target": "hindsight"},
                ),
            ],
        )
        self.assertEqual(outcome["completion"], "complete")
        self.assertEqual(
            outcome["application_target_readiness"],
            {"codex": "unverified", "hindsight": "unverified"},
        )
        self.assertEqual(
            outcome["application_target_issues"],
            (
                {
                    "code": "application_target_drifted",
                    "application_target": "hindsight",
                    "state": "drifted",
                    "message": "managed state changed",
                    "next_actions": (
                        "mastic application-target configure hindsight --help",
                    ),
                },
            ),
        )
        encoded = json.dumps(outcome)
        self.assertNotIn("credential", encoded)
        self.assertNotIn("must not escape the inspection boundary", encoded)

    def test_unknown_or_empty_target_readiness_fails_closed(self):
        self.assertEqual(combined_readiness({}).value, "unverified")
        self.assertEqual(
            combined_readiness({"codex": "future-state"}).value,
            "unverified",
        )
        self.assertEqual(
            combined_readiness({"codex": "ready", "hindsight": "future-state"}).value,
            "unverified",
        )

    def test_durable_outcome_fails_closed_when_target_observation_is_unknown(self):
        plans = FakePlanStore()
        plans.plan = {
            "plan_identity": "a" * 64,
            "steps": (
                {
                    "id": "application.canary.codex",
                    "fingerprint": "codex-v1",
                    "state": "skipped",
                },
            ),
            "application_targets": ("codex",),
            "performance_binding": {
                "selection_sha256": SELECTION_SHA256,
                "application_versions": {
                    "codex": "0.144.1",
                    "hindsight": "0.8.4",
                },
                "platform": "darwin",
                "machine": "arm64",
                "memory_bytes": 96 * GIB,
                "macos_major": 26,
                "service": "coding",
            },
        }
        evidence = FakeEvidenceStore()
        evidence.record(
            "setup",
            SetupEvidence(
                "application.canary.codex",
                "codex-v1",
                StepState.SKIPPED,
                "",
            ),
        )
        inspections = FakeOwner(fail="application-target.inspect")

        outcome = DurableSetupOutcomeProvider(
            plans, evidence, application_targets=inspections
        ).outcome()

        self.assertEqual(outcome["completion"], "complete")
        self.assertEqual(
            outcome["application_target_readiness"], {"codex": "unverified"}
        )
        self.assertEqual(
            outcome["application_target_issues"][0]["code"],
            "application_target_observation_failed",
        )
        self.assertEqual(
            outcome["application_target_issues"][0]["next_actions"],
            ("mastic application-target inspect codex",),
        )

    def test_durable_gateway_verification_requires_the_exact_contract_digest(self):
        plans = FakePlanStore()
        plans.plan = {
            "plan_identity": "a" * 64,
            "steps": ({"id": "verify.request", "fingerprint": "verify-v1"},),
            "application_targets": (),
        }
        evidence = FakeEvidenceStore()
        inspections = FakeOwner(fail="application-target.inspect")

        def outcome_for(digest, *, state=StepState.COMPLETE):
            evidence.items["setup"] = [
                SetupEvidence(
                    "verify.request",
                    "verify-v1",
                    state,
                    json.dumps({"result": {"ok": True, "response_sha256": digest}}),
                )
            ]
            return DurableSetupOutcomeProvider(
                plans, evidence, application_targets=inspections
            ).outcome()

        self.assertEqual(
            outcome_for(READY_RESPONSE_SHA256)["readiness"],
            "ready",
        )
        self.assertEqual(outcome_for("b" * 64)["readiness"], "unverified")
        skipped = outcome_for(READY_RESPONSE_SHA256, state=StepState.SKIPPED)
        self.assertEqual(skipped["completion"], "partial")
        self.assertEqual(skipped["readiness"], "unverified")
        self.assertEqual(inspections.calls, [])

    def test_durable_canary_recomputes_the_persisted_performance_band(self):
        plans = FakePlanStore()
        plans.plan = {
            "plan_identity": "a" * 64,
            "steps": (
                {
                    "id": "application.canary.codex",
                    "fingerprint": "canary-v1",
                    "state": "ready",
                },
            ),
            "application_targets": ("codex",),
            "performance_binding": {
                "selection_sha256": "a" * 64,
                "application_versions": {
                    "codex": "0.144.1",
                    "hindsight": "0.8.4",
                },
                "platform": "darwin",
                "machine": "arm64",
                "memory_bytes": 96 * GIB,
                "macos_major": 26,
                "service": "coding",
            },
        }
        evidence = FakeEvidenceStore()
        evidence.items["setup"] = [
            SetupEvidence(
                "application.canary.codex",
                "canary-v1",
                StepState.COMPLETE,
                json.dumps(
                    {
                        "result": {
                            "profile": "coding",
                            "service": "coding",
                            "ok": True,
                            "exact_contract": True,
                            "phases": canary_phases("codex"),
                            "evidence_sha256": canary_evidence_sha256("codex"),
                            "performance": {
                                "metric": "codex.native_canary.duration_seconds",
                                "value": 999.0,
                                "unit": "seconds",
                                "band": "expected",
                                "profile_id": "phase1-qwen36-optiq-apple-silicon",
                                "profile_version": 1,
                            },
                        }
                    }
                ),
            )
        ]
        inspections = FakeOwner(
            {
                "application-target.inspect": {
                    "state": "healthy",
                    "detail": "managed state matches",
                }
            }
        )

        outcome = DurableSetupOutcomeProvider(
            plans,
            evidence,
            validated_performance_profile(plan_sha256="a" * 64),
            application_targets=inspections,
        ).outcome()

        self.assertEqual(outcome["completion"], "partial")
        self.assertEqual(outcome["readiness"], "unverified")
        self.assertEqual(
            outcome["application_target_readiness"], {"codex": "unverified"}
        )

    def test_durable_canary_requires_the_machine_bound_plan_binding(self):
        plans = FakePlanStore()
        plans.plan = {
            "plan_identity": "a" * 64,
            "steps": (
                {
                    "id": "application.canary.codex",
                    "fingerprint": "canary-v1",
                    "state": "ready",
                },
            ),
            "application_targets": ("codex",),
        }
        profile = validated_performance_profile(plan_sha256="c" * 64)
        evidence = FakeEvidenceStore()
        evidence.items["setup"] = [
            SetupEvidence(
                "application.canary.codex",
                "canary-v1",
                StepState.COMPLETE,
                json.dumps(
                    {
                        "result": {
                            "profile": "coding",
                            "service": "coding",
                            "ok": True,
                            "exact_contract": True,
                            "phases": canary_phases("codex"),
                            "evidence_sha256": canary_evidence_sha256("codex"),
                            "performance": {
                                "metric": "codex.native_canary.duration_seconds",
                                "value": 12.0,
                                "unit": "seconds",
                                "band": "expected",
                                "profile_id": profile["id"],
                                "profile_version": profile["version"],
                            },
                        }
                    }
                ),
            )
        ]
        inspections = FakeOwner(
            {"application-target.inspect": {"state": "healthy", "detail": "ok"}}
        )

        with self.subTest("without_binding"):
            without_binding = DurableSetupOutcomeProvider(
                plans, evidence, profile, application_targets=inspections
            ).outcome()
            self.assertEqual(without_binding["completion"], "partial")
            self.assertEqual(without_binding["readiness"], "unverified")
        plans.plan["performance_binding"] = {
            "selection_sha256": "c" * 64,
            "application_versions": {"codex": "0.144.1", "hindsight": "0.8.4"},
            "platform": "darwin",
            "machine": "arm64",
            "memory_bytes": 96 * GIB,
            "macos_major": 26,
            "service": "coding",
        }
        with self.subTest("matching"):
            matching = DurableSetupOutcomeProvider(
                plans, evidence, profile, application_targets=inspections
            ).outcome()
            self.assertEqual(matching["readiness"], "ready")
        canary = evidence.items["setup"][0]
        wrong_service_detail = json.loads(canary.detail)
        wrong_service_detail["result"].update(
            {
                "service": "wrong",
                "evidence_sha256": canary_evidence_sha256("codex", service="wrong"),
            }
        )
        evidence.items["setup"][0] = replace(
            canary, detail=json.dumps(wrong_service_detail)
        )
        with self.subTest("wrong_service"):
            wrong_service = DurableSetupOutcomeProvider(
                plans, evidence, profile, application_targets=inspections
            ).outcome()
            self.assertEqual(wrong_service["completion"], "partial")
            self.assertEqual(wrong_service["readiness"], "unverified")
        evidence.items["setup"][0] = canary
        plans.plan["performance_binding"] = {
            **plans.plan["performance_binding"],
            "selection_sha256": "d" * 64,
        }
        with self.subTest("wrong_plan"):
            wrong_plan = DurableSetupOutcomeProvider(
                plans, evidence, profile, application_targets=inspections
            ).outcome()
            self.assertEqual(wrong_plan["completion"], "complete")
            self.assertEqual(wrong_plan["readiness"], "unverified")

    def test_durable_skipped_canary_requires_a_structurally_valid_plan_binding(
        self,
    ) -> None:
        plans = FakePlanStore()
        plans.plan = {
            "plan_identity": "a" * 64,
            "steps": (
                {
                    "id": "application.canary.codex",
                    "fingerprint": "canary-v1",
                    "state": "skipped",
                },
            ),
            "application_targets": ("codex",),
        }
        evidence = FakeEvidenceStore()
        evidence.items["setup"] = [
            SetupEvidence(
                "application.canary.codex",
                "canary-v1",
                StepState.SKIPPED,
                "",
            )
        ]
        profile = validated_performance_profile(plan_sha256="c" * 64)

        with self.subTest("missing"):
            missing = DurableSetupOutcomeProvider(plans, evidence, profile).outcome()
            self.assertEqual(missing["completion"], "partial")
        plans.plan["performance_binding"] = {
            "selection_sha256": "not-a-digest",
            "application_versions": {
                "codex": "0.144.1",
                "hindsight": "0.8.4",
            },
            "platform": "darwin",
            "machine": "arm64",
            "memory_bytes": 96 * GIB,
            "macos_major": 26,
            "service": "coding",
        }
        with self.subTest("malformed"):
            malformed = DurableSetupOutcomeProvider(plans, evidence, profile).outcome()
            self.assertEqual(malformed["completion"], "partial")
        plans.plan["performance_binding"] = {
            **plans.plan["performance_binding"],
            "selection_sha256": "d" * 64,
        }
        with self.subTest("exact_alternate_plan"):
            exact_alternate_plan = DurableSetupOutcomeProvider(
                plans, evidence, profile
            ).outcome()
            self.assertEqual(exact_alternate_plan["completion"], "complete")
            self.assertEqual(exact_alternate_plan["readiness"], "unverified")
        plans.plan["steps"] = (
            {
                **plans.plan["steps"][0],
                "state": "ready",
            },
        )
        with self.subTest("unauthorized"):
            unauthorized = DurableSetupOutcomeProvider(
                plans, evidence, profile
            ).outcome()
            self.assertEqual(unauthorized["completion"], "partial")

    def test_plan_store_can_reactivate_an_exact_prior_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            state = OperationalStateStore(Path(directory) / "state.sqlite3")
            plans = OperationalSetupPlanStore(state)
            base = {
                "steps": ({"id": "verify.request", "fingerprint": "verify-v1"},),
                "application_targets": (),
            }

            plans.record({**base, "plan_identity": "a" * 64})
            plans.record({**base, "plan_identity": "b" * 64})
            plans.record({**base, "plan_identity": "a" * 64})

            self.assertEqual(plans.load()["plan_identity"], "a" * 64)
            self.assertEqual(len(state.snapshots("setup_plan")), 1)

    def test_plan_store_rejects_fields_outside_the_content_free_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            state = OperationalStateStore(Path(directory) / "state.sqlite3")
            plans = OperationalSetupPlanStore(state)

            with self.assertRaisesRegex(ValueError, "unsupported fields"):
                plans.record(
                    {
                        "plan_identity": "a" * 64,
                        "steps": (
                            {"id": "verify.request", "fingerprint": "verify-v1"},
                        ),
                        "application_targets": (),
                        "prompt": "must not persist",
                    }
                )

            self.assertEqual(state.snapshots("setup_plan"), ())

    def test_preview_is_exact_machine_aware_editable_and_side_effect_free(self):
        preview = self.port().preview({"profile": "recommended"})

        self.assertEqual(preview["state"], "review_required")
        self.assertEqual(preview["profile"], "workstation")
        self.assertTrue(preview["editable"])
        self.assertEqual(preview["selection"]["runtime"], "optiq==0.3.3")
        self.assertEqual(preview["selection"]["model_revision"], MODEL_REVISION)
        self.assertEqual(preview["selection"]["model_alias"], "qwen-optiq")
        self.assertEqual(preview["selection"]["service_route"], "engineering")
        self.assertEqual(
            preview["selection"]["application_target_options"]["hindsight"]["profile"],
            "default",
        )
        self.assertEqual(preview["selection"]["activation"], "supervisor")
        self.assertTrue(preview["selection"]["pinned"])
        self.assertTrue(preview["selection"]["service_options"]["mtp"])
        self.assertEqual(len(preview["preview_fingerprint"]), 64)
        self.assertEqual(preview["steps"][-1]["id"], "application.canary.hindsight")
        codex_canary = next(
            step
            for step in preview["steps"]
            if step["id"] == "application.canary.codex"
        )
        self.assertEqual(
            codex_canary["inputs"]["performance_profile"],
            {
                "id": "phase1-qwen36-optiq-apple-silicon",
                "version": 1,
            },
        )
        self.assertEqual(
            preview["performance_profile"],
            {
                **validated_performance_profile(plan_sha256=SELECTION_SHA256),
                "status": "provisional",
            },
        )
        self.assertEqual(
            self.runtime.calls
            + self.model.calls
            + self.config.calls
            + self.applications.calls
            + self.application_targets.calls
            + self.supervisor.calls
            + self.verifier.calls,
            [],
        )

    def test_direct_setup_port_rejects_non_boolean_control_flags(self) -> None:
        for parameters in ({"offline": "false"}, {"noninteractive": 1}):
            with self.subTest(parameters=parameters):
                with self.assertRaises(ApplicationError) as raised:
                    self.port().preview(parameters)
                self.assertEqual(raised.exception.code, "invalid_parameter")

    def test_no_validated_fit_is_a_completed_observation_without_mutation(self):
        preview = self.port(
            facts=SetupPreflight("darwin", "arm64", GIB, GIB, True)
        ).preview({"profile": "recommended"})

        self.assertEqual(preview["state"], "no_validated_fit")
        self.assertEqual(preview["completion"], "complete")
        self.assertEqual(preview["readiness"], "unverified")
        self.assertFalse(preview["confirmation_required"])
        self.assertIn("memory", preview["limiting_evidence"])
        self.assertTrue(preview["remediation"])
        self.assertEqual(
            self.runtime.calls
            + self.model.calls
            + self.config.calls
            + self.applications.calls
            + self.application_targets.calls
            + self.supervisor.calls
            + self.verifier.calls,
            [],
        )

    def test_capacity_choice_is_discoverable_and_changes_preview_identity(self):
        baseline = self.port().preview({})
        selected = self.port().preview({"capacity": "long-context"})

        self.assertIn("capacity", selected)
        self.assertEqual(selected["capacity"]["profile"], "long-context")
        self.assertEqual(selected["capacity"]["context_window"], 196_608)
        self.assertEqual(selected["capacity"]["max_concurrent"], 4)
        self.assertIn("simultaneous inference requests", selected["capacity"]["note"])
        self.assertNotEqual(
            baseline["preview_fingerprint"], selected["preview_fingerprint"]
        )

    def test_public_intent_survives_preliminary_and_final_resolution(self):
        compact = RecommendedProfile("compact", 16 * GIB, selection())
        capacities = (
            CapacityProfile("balanced", "Balanced", 131_072, 6, 1, 1, "Balanced"),
            CapacityProfile("deep", "Deep", 262_144, 3, 1, 1, "Deep"),
            CapacityProfile("responsive", "Responsive", 65_536, 7, 1, 1, "Responsive"),
        )
        self.resolver = SetupResolver(
            (compact,),
            capacity_profiles=capacities,
            default_capacity_profile="balanced",
            intent_capacity_profiles={
                SetupIntent.BALANCED: "balanced",
                SetupIntent.DEEP: "deep",
                SetupIntent.RESPONSIVE: "responsive",
            },
        )

        for intent, expected_capacity in (
            ("balanced", "balanced"),
            ("deep", "deep"),
            ("responsive", "responsive"),
        ):
            with self.subTest(intent=intent):
                preview = self.port().preview(
                    {
                        "intent": intent,
                        # Exercise the preliminary editable-selection pass too.
                        "service_route": f"{intent}-route",
                    }
                )
                self.assertEqual(preview["intent"], intent)
                self.assertEqual(preview["capacity"]["profile"], expected_capacity)

    def test_confirmed_exact_preview_orchestrates_owners_and_persists_evidence(self):
        port = self.port()
        preview = port.preview({})

        result = port.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )

        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["completion"], "complete")
        self.assertEqual(result["readiness"], "unverified")
        self.assertEqual(
            result["application_target_readiness"],
            {"codex": "unverified", "hindsight": "unverified"},
        )
        runtime = self.runtime.calls[0]
        self.assertIsInstance(runtime, InstallRuntime)
        self.assertEqual(runtime.runtime, "optiq")
        self.assertEqual(runtime.expected_version, "0.3.3")
        self.assertEqual(runtime.expected_lock_digest, "a" * 64)
        self.assertIsInstance(self.model.calls[0], InstallModel)
        self.assertEqual(
            self.applications.calls,
            [
                InstallApplications(
                    step_fingerprint=next(
                        step["fingerprint"]
                        for step in result["steps"]
                        if step["id"] == "application.install"
                    ),
                    application_targets=("codex", "hindsight"),
                    preserve_outdated_codex=False,
                    offline=False,
                )
            ],
        )
        configure_index = next(
            index
            for index, item in enumerate(result["steps"])
            if item["id"] == "application-target.configure"
        )
        self.assertEqual(
            result["steps"][configure_index - 1]["id"], "application.install"
        )
        self.assertEqual(self.model.calls[0].revision, MODEL_REVISION)
        self.assertEqual(self.model.calls[0].alias, "qwen-optiq")
        service = next(
            call for call in self.config.calls if isinstance(call, ConfigureService)
        )
        self.assertEqual(service.service, "coding")
        self.assertEqual(service.runtime, "optiq-0.3.3-tested")
        self.assertEqual(service.model_alias, "qwen-optiq")
        self.assertEqual(service.route, "engineering")
        self.assertEqual(service.activation, "supervisor")
        self.assertTrue(service.pinned)
        self.assertEqual(
            service.options,
            {
                "kv_config": "kv_config.json",
                "mtp": True,
                "runtime": {"draft_tokens": 4},
            },
        )
        self.assertIsInstance(self.supervisor.calls[0], ActivateSupervisor)
        self.assertIsInstance(self.supervisor.calls[-1], StartService)
        self.assertEqual(self.supervisor.calls[-1].resource, "coding")
        self.assertEqual(self.verifier.calls, [])
        self.assertEqual(
            [
                call.application_target
                for call in self.application_targets.calls
                if isinstance(call, TestApplicationTarget)
            ],
            ["codex", "hindsight"],
        )
        self.assertEqual(
            {
                call.service
                for call in self.application_targets.calls
                if isinstance(call, ConfigureApplicationTarget)
            },
            {"coding"},
        )
        self.assertEqual(len(self.evidence.items["setup"]), len(result["steps"]))
        canary_evidence = next(
            item.detail
            for item in self.evidence.items["setup"]
            if item.step_id == "application.canary.hindsight"
        )
        self.assertNotIn("mastic ready", canary_evidence)
        self.assertIn("evidence_sha256", canary_evidence)

    def test_skipped_target_canary_completes_unverified_without_invocation(self):
        port = self.port()
        preview = port.preview({"skip_canaries": ["hindsight"]})

        result = port.execute(
            "setup",
            {
                "skip_canaries": ["hindsight"],
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )

        tests = [
            call
            for call in self.application_targets.calls
            if isinstance(call, TestApplicationTarget)
        ]
        self.assertEqual(
            [call.application_target for call in tests],
            ["codex"],
        )
        self.assertEqual(result["completion"], "complete")
        self.assertEqual(result["readiness"], "unverified")
        self.assertEqual(
            result["application_target_readiness"],
            {"codex": "unverified", "hindsight": "unverified"},
        )
        skipped = next(
            item
            for item in result["evidence"]
            if item["step_id"] == "application.canary.hindsight"
        )
        self.assertEqual(skipped["state"], "skipped")

    def test_correct_slow_canary_is_durably_degraded_after_resume(self) -> None:
        durations = {"codex": 60.001, "hindsight": 180.0}
        self.application_targets.results["application-target.test"] = (
            lambda parameters: {
                "profile": parameters["profile"],
                "response": {
                    "ok": True,
                    "exact_contract": True,
                    "duration_seconds": durations[parameters["application_target"]],
                    "phases": canary_phases(parameters["application_target"]),
                    "evidence_sha256": canary_evidence_sha256(
                        parameters["application_target"]
                    ),
                },
            }
        )
        port = self.port(
            performance_profile=validated_performance_profile(
                plan_sha256=SELECTION_SHA256
            )
        )
        preview = port.preview({})

        first = port.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )

        self.assertEqual(first["completion"], "complete")
        self.assertEqual(first["readiness"], "degraded")
        self.assertEqual(
            first["application_target_readiness"],
            {"codex": "degraded", "hindsight": "ready"},
        )
        codex_evidence = next(
            item
            for item in first["evidence"]
            if item["step_id"] == "application.canary.codex"
        )
        detail = json.loads(codex_evidence["detail"])["result"]
        self.assertEqual(
            detail,
            {
                "profile": "coding",
                "service": "coding",
                "ok": True,
                "exact_contract": True,
                "phases": canary_phases("codex"),
                "evidence_sha256": canary_evidence_sha256("codex"),
                "performance": {
                    "metric": "codex.native_canary.duration_seconds",
                    "value": 60.001,
                    "unit": "seconds",
                    "band": "degraded",
                    "profile_id": "phase1-qwen36-optiq-apple-silicon",
                    "profile_version": 1,
                },
            },
        )

        resumed_preview = port.preview({})
        self.application_targets.calls.clear()
        resumed = port.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": resumed_preview["preview_fingerprint"],
            },
        )

        self.assertEqual(resumed["completion"], "complete")
        self.assertEqual(resumed["readiness"], "degraded")
        self.assertEqual(resumed["application_target_readiness"]["codex"], "degraded")
        self.assertEqual(self.application_targets.calls, [])

    def test_resumed_preview_rejects_malformed_terminal_canary_evidence(self) -> None:
        port = self.port(
            performance_profile=validated_performance_profile(
                plan_sha256=SELECTION_SHA256
            )
        )
        preview = port.preview({})
        first = port.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )
        self.assertEqual(first["readiness"], "ready")

        index, canary = next(
            (index, item)
            for index, item in enumerate(self.evidence.items["setup"])
            if item.step_id == "application.canary.codex"
        )
        detail = json.loads(canary.detail)
        detail["result"].update(
            {"ok": False, "exact_contract": False, "evidence_sha256": "invalid"}
        )
        self.evidence.items["setup"][index] = replace(canary, detail=json.dumps(detail))

        resumed = port.preview({})

        self.assertEqual(resumed["readiness"], "unverified")
        self.assertEqual(resumed["application_target_readiness"]["codex"], "unverified")

        self.application_targets.calls.clear()
        repaired = port.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": resumed["preview_fingerprint"],
            },
        )

        self.assertEqual(repaired["readiness"], "ready")
        self.assertEqual(
            [
                call.application_target
                for call in self.application_targets.calls
                if isinstance(call, TestApplicationTarget)
            ],
            ["codex"],
        )

    def test_resumed_preview_validates_the_complete_canary_evidence_shape(self) -> None:
        port = self.port(
            performance_profile=validated_performance_profile(
                plan_sha256=SELECTION_SHA256
            )
        )
        preview = port.preview({})
        port.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )
        index, canary = next(
            (index, item)
            for index, item in enumerate(self.evidence.items["setup"])
            if item.step_id == "application.canary.codex"
        )

        cases = (
            ("profile", "not-coding"),
            ("phases", ["responses.exact", "codex.exec"]),
            ("unit", "milliseconds"),
            ("evidence_sha256", "0" * 64),
        )
        for field, value in cases:
            with self.subTest(field=field):
                detail = json.loads(canary.detail)
                if field == "unit":
                    detail["result"]["performance"][field] = value
                else:
                    detail["result"][field] = value
                self.evidence.items["setup"][index] = replace(
                    canary, detail=json.dumps(detail)
                )

                resumed = port.preview({})

                self.assertEqual(resumed["readiness"], "unverified")
                self.assertEqual(
                    resumed["application_target_readiness"]["codex"],
                    "unverified",
                )

        self.evidence.items["setup"][index] = canary

    def test_resumed_preview_rejects_malformed_terminal_gateway_evidence(self) -> None:
        exact = replace(
            selection(), application_targets=(), application_target_options={}
        )
        port = self.port()
        preview = port.preview({"selection": exact})
        first = port.execute(
            "setup",
            {
                "selection": exact,
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )
        self.assertEqual(first["readiness"], "ready")

        index, verification = next(
            (index, item)
            for index, item in enumerate(self.evidence.items["setup"])
            if item.step_id == "verify.request"
        )
        self.evidence.items["setup"][index] = replace(
            verification,
            detail=json.dumps({"result": {"ok": False, "response_sha256": "invalid"}}),
        )

        resumed = port.preview({"selection": exact})

        self.assertEqual(resumed["readiness"], "unverified")

        self.verifier.calls.clear()
        repaired = port.execute(
            "setup",
            {
                "selection": exact,
                "confirmed": True,
                "preview_fingerprint": resumed["preview_fingerprint"],
            },
        )

        self.assertEqual(repaired["readiness"], "ready")
        self.assertEqual(len(self.verifier.calls), 1)
        self.assertIsInstance(self.verifier.calls[0], VerifyGatewayRequest)

    def test_resumed_preview_rejects_unauthorized_skipped_gateway_evidence(
        self,
    ) -> None:
        exact = replace(
            selection(), application_targets=(), application_target_options={}
        )
        port = self.port()
        preview = port.preview({"selection": exact})
        port.execute(
            "setup",
            {
                "selection": exact,
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )
        index, verification = next(
            (index, item)
            for index, item in enumerate(self.evidence.items["setup"])
            if item.step_id == "verify.request"
        )
        self.evidence.items["setup"][index] = replace(
            verification,
            state=StepState.SKIPPED,
            detail="",
        )

        resumed = port.preview({"selection": exact})
        verification_step = next(
            step for step in resumed["steps"] if step["id"] == "verify.request"
        )

        self.assertEqual(verification_step["state"], "ready")
        self.assertEqual(resumed["completion"], "partial")

        self.verifier.calls.clear()
        repaired = port.execute(
            "setup",
            {
                "selection": exact,
                "confirmed": True,
                "preview_fingerprint": resumed["preview_fingerprint"],
            },
        )

        self.assertEqual(repaired["readiness"], "ready")
        self.assertEqual(len(self.verifier.calls), 1)
        self.assertIsInstance(self.verifier.calls[0], VerifyGatewayRequest)

    def test_resumed_preview_rejects_unauthorized_skipped_canary_evidence(
        self,
    ) -> None:
        port = self.port()
        preview = port.preview({})
        port.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )
        index, canary = next(
            (index, item)
            for index, item in enumerate(self.evidence.items["setup"])
            if item.step_id == "application.canary.codex"
        )
        self.evidence.items["setup"][index] = replace(
            canary,
            state=StepState.SKIPPED,
            detail="",
        )

        resumed = port.preview({})
        canary_step = next(
            step
            for step in resumed["steps"]
            if step["id"] == "application.canary.codex"
        )

        self.assertEqual(canary_step["state"], "ready")
        self.assertEqual(resumed["completion"], "partial")

        self.application_targets.calls.clear()
        repaired = port.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": resumed["preview_fingerprint"],
            },
        )

        self.assertEqual(repaired["completion"], "complete")
        self.assertEqual(
            [
                call.application_target
                for call in self.application_targets.calls
                if isinstance(call, TestApplicationTarget)
            ],
            ["codex"],
        )

    def test_skipped_required_canary_precedes_a_degraded_target(self) -> None:
        self.application_targets.results["application-target.test"] = (
            lambda parameters: {
                "profile": parameters["profile"],
                "response": {
                    "ok": True,
                    "exact_contract": True,
                    "duration_seconds": 61.0,
                    "phases": canary_phases(parameters["application_target"]),
                    "evidence_sha256": canary_evidence_sha256(
                        parameters["application_target"]
                    ),
                },
            }
        )
        port = self.port(
            performance_profile=validated_performance_profile(
                plan_sha256=SELECTION_SHA256
            )
        )
        preview = port.preview({"skip_canaries": ["hindsight"]})

        result = port.execute(
            "setup",
            {
                "skip_canaries": ["hindsight"],
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )

        self.assertEqual(result["completion"], "complete")
        self.assertEqual(result["readiness"], "unverified")
        self.assertEqual(
            result["application_target_readiness"],
            {"codex": "degraded", "hindsight": "unverified"},
        )

    def test_performance_profile_does_not_validate_a_different_model(self) -> None:
        alternate_revision = "3" * 40
        alternate = replace(selection(), model_revision=alternate_revision)
        self.model.results["model.install"] = {
            "installation_id": f"qwen-optiq@{alternate_revision}",
            "alias": "coding",
            "revision": alternate_revision,
        }
        port = self.port(
            performance_profile=validated_performance_profile(
                plan_sha256=SELECTION_SHA256
            )
        )
        preview = port.preview({"selection": alternate})

        result = port.execute(
            "setup",
            {
                "selection": alternate,
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )

        self.assertEqual(result["completion"], "complete")
        self.assertEqual(result["readiness"], "unverified")
        self.assertEqual(
            result["application_target_readiness"],
            {"codex": "unverified", "hindsight": "unverified"},
        )

    def test_validated_performance_profile_requires_the_exact_plan(self) -> None:
        port = self.port(
            performance_profile=validated_performance_profile(plan_sha256="0" * 64)
        )
        preview = port.preview({})

        result = port.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )

        self.assertEqual(result["completion"], "complete")
        self.assertEqual(result["readiness"], "unverified")
        self.assertEqual(
            result["application_target_readiness"],
            {"codex": "unverified", "hindsight": "unverified"},
        )

    def test_malformed_performance_profile_fails_at_composition(self) -> None:
        with self.assertRaisesRegex(ValueError, "performance profile id"):
            self.port(performance_profile={})

    def test_validated_profile_binds_capacity_and_macos_range(self) -> None:
        profile = validated_performance_profile(plan_sha256=SELECTION_SHA256)

        exact = self.port(performance_profile=profile)
        exact_preview = exact.preview({})
        exact_result = exact.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": exact_preview["preview_fingerprint"],
            },
        )
        self.assertEqual(exact_result["readiness"], "ready")

        long_context = self.port(performance_profile=profile)
        capacity_preview = long_context.preview({"capacity": "long-context"})
        capacity_result = long_context.execute(
            "setup",
            {
                "capacity": "long-context",
                "confirmed": True,
                "preview_fingerprint": capacity_preview["preview_fingerprint"],
            },
        )
        self.assertEqual(capacity_result["readiness"], "unverified")

        outside_range = self.port(
            facts=SetupPreflight(
                "darwin",
                "arm64",
                96 * GIB,
                500 * GIB,
                True,
                os_version="27.0",
            ),
            performance_profile=profile,
        )
        os_preview = outside_range.preview({})
        os_result = outside_range.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": os_preview["preview_fingerprint"],
            },
        )
        self.assertEqual(os_result["readiness"], "unverified")

    def test_missing_canary_duration_interrupts_without_readiness_claim(self) -> None:
        self.application_targets.results["application-target.test"] = (
            lambda parameters: {
                "profile": parameters["profile"],
                "response": {
                    "ok": True,
                    "exact_contract": True,
                    "phases": canary_phases(parameters["application_target"]),
                    "evidence_sha256": canary_evidence_sha256(
                        parameters["application_target"]
                    ),
                },
            }
        )
        port = self.port()
        preview = port.preview({})

        with self.assertRaises(ApplicationError) as raised:
            port.execute(
                "setup",
                {
                    "confirmed": True,
                    "preview_fingerprint": preview["preview_fingerprint"],
                },
            )

        self.assertEqual(raised.exception.code, "setup_interrupted")
        self.assertIn("finite nonnegative duration", str(raised.exception))

    def test_confirmed_preview_executes_each_selected_target_canary(self) -> None:
        port = self.port()
        preview = port.preview({})

        result = port.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )

        tests = [
            call
            for call in self.application_targets.calls
            if isinstance(call, TestApplicationTarget)
        ]
        self.assertEqual(
            [(call.application_target, call.profile) for call in tests],
            [
                ("codex", "coding"),
                ("hindsight", "retain"),
            ],
        )
        self.assertEqual(self.verifier.calls, [])
        self.assertEqual(
            list(result["results"])[-2:],
            ["application.canary.codex", "application.canary.hindsight"],
        )

    def test_failed_target_canary_is_attributed_and_resumes_at_that_target(
        self,
    ) -> None:
        def fail_hindsight(parameters):
            if parameters["application_target"] == "hindsight":
                return {
                    "profile": parameters["profile"],
                    "response": {"ok": False, "exact_contract": False},
                }
            return {
                "profile": parameters["profile"],
                "response": {
                    "ok": True,
                    "exact_contract": True,
                    "duration_seconds": 12.0,
                    "phases": canary_phases(parameters["application_target"]),
                    "evidence_sha256": canary_evidence_sha256(
                        parameters["application_target"]
                    ),
                },
            }

        self.application_targets.results["application-target.test"] = fail_hindsight
        port = self.port()
        preview = port.preview({})

        with self.assertRaises(ApplicationError) as raised:
            port.execute(
                "setup",
                {
                    "confirmed": True,
                    "preview_fingerprint": preview["preview_fingerprint"],
                },
            )

        self.assertEqual(raised.exception.code, "setup_interrupted")
        self.assertIn("application.canary.hindsight", str(raised.exception))
        details = dict(raised.exception.details)
        self.assertEqual(details["state"], "interrupted")
        self.assertFalse(details["complete"])
        self.assertEqual(details["completion"], "partial")
        self.assertEqual(details["readiness"], "pending")
        self.assertEqual(
            details["application_target_readiness"],
            {"codex": "unverified", "hindsight": "pending"},
        )
        self.assertEqual(details["failed_step"], "application.canary.hindsight")
        self.assertEqual(
            details["remaining_steps"],
            ("application.canary.hindsight",),
        )
        self.assertEqual(
            details["observations"]["application_target_readiness"],
            {"codex": "unverified", "hindsight": "pending"},
        )
        self.assertIn("preflight", details["observations"])
        self.assertIn(
            "application.canary.codex", details["observations"]["completed_steps"]
        )
        self.assertEqual(
            [item.step_id for item in self.evidence.items["setup"]][-1],
            "application.canary.hindsight",
        )
        self.assertEqual(self.evidence.items["setup"][-1].state, StepState.FAILED)

        self.application_targets.calls.clear()
        self.application_targets.results["application-target.test"] = (
            lambda parameters: {
                "profile": parameters["profile"],
                "response": {
                    "ok": True,
                    "exact_contract": True,
                    "duration_seconds": 12.0,
                    "phases": canary_phases(parameters["application_target"]),
                    "evidence_sha256": canary_evidence_sha256(
                        parameters["application_target"]
                    ),
                },
            }
        )
        resumed = port.preview({})
        self.assertEqual(resumed["completion"], "partial")
        self.assertEqual(resumed["readiness"], "pending")
        self.assertEqual(
            resumed["application_target_readiness"],
            {"codex": "unverified", "hindsight": "pending"},
        )
        result = port.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": resumed["preview_fingerprint"],
            },
        )

        self.assertEqual(result["state"], "complete")
        self.assertEqual(
            [
                (call.application_target, call.profile)
                for call in self.application_targets.calls
                if isinstance(call, TestApplicationTarget)
            ],
            [("hindsight", "retain")],
        )

    def test_editing_service_identity_or_options_changes_preview_identity(self):
        port = self.port()
        baseline = port.preview({})
        route = port.preview({"service_route": "assistant"})
        options = port.preview(
            {"service_options": {"kv_config": "kv_config.json", "mtp": False}}
        )

        self.assertNotEqual(
            baseline["preview_fingerprint"], route["preview_fingerprint"]
        )
        self.assertNotEqual(
            baseline["preview_fingerprint"], options["preview_fingerprint"]
        )

    def test_confirmation_survives_harmless_free_disk_observation_drift(self):
        observations = iter(
            (
                self.facts,
                replace(
                    self.facts,
                    disk_free_bytes=self.facts.disk_free_bytes - 4096,
                ),
            )
        )
        port = self.port(preflight=lambda _offline: next(observations))
        preview = port.preview({})
        preflight_step = next(
            step for step in preview["steps"] if step["id"] == "preflight"
        )

        self.assertEqual(
            preflight_step["inputs"],
            {
                "platform": "darwin",
                "machine": "arm64",
                "os_version": "26.5",
                "memory_bytes": self.facts.memory_bytes,
                "disk_free_bytes": self.facts.disk_free_bytes,
            },
        )
        self.assertEqual(
            preview["preflight"]["disk_free_bytes"],
            self.facts.disk_free_bytes,
        )
        self.assertEqual(
            preview["host_requirements"],
            {
                "platform": "darwin",
                "machine": "arm64",
                "macos_major": 26,
                "minimum_memory_bytes": 64 * GIB,
                "minimum_disk_bytes": 0,
            },
        )

        result = port.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )

        self.assertEqual(result["state"], "complete")
        self.assertEqual(
            result["preflight"]["disk_free_bytes"],
            self.facts.disk_free_bytes - 4096,
        )
        self.assertTrue(self.runtime.calls)

    def test_confirmation_survives_macos_patch_observation_drift(self):
        observations = iter(
            (
                self.facts,
                replace(self.facts, os_version="26.6"),
            )
        )
        port = self.port(preflight=lambda _offline: next(observations))
        preview = port.preview({})

        result = port.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )

        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["preflight"]["os_version"], "26.6")
        self.assertTrue(self.runtime.calls)

    def test_confirmation_rejects_macos_major_observation_drift(self):
        observations = iter(
            (
                self.facts,
                replace(self.facts, os_version="27.0"),
            )
        )
        port = self.port(preflight=lambda _offline: next(observations))
        preview = port.preview({})

        with self.assertRaises(ApplicationError) as raised:
            port.execute(
                "setup",
                {
                    "confirmed": True,
                    "preview_fingerprint": preview["preview_fingerprint"],
                },
            )

        self.assertEqual(raised.exception.code, "preview_changed")
        self.assertEqual(self.runtime.calls, [])

    def test_edited_recommended_confirmation_survives_harmless_disk_drift(self):
        observations = iter(
            (
                self.facts,
                replace(
                    self.facts,
                    disk_free_bytes=self.facts.disk_free_bytes - 4096,
                ),
            )
        )
        parameters = {"service_route": "assistant"}
        port = self.port(preflight=lambda _offline: next(observations))
        preview = port.preview(parameters)

        result = port.execute(
            "setup",
            {
                **parameters,
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )

        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["selection"]["service_route"], "assistant")
        self.assertTrue(self.runtime.calls)

    def test_exact_confirmation_survives_harmless_free_disk_observation_drift(self):
        observations = iter(
            (
                self.facts,
                replace(
                    self.facts,
                    disk_free_bytes=self.facts.disk_free_bytes - 4096,
                ),
            )
        )
        exact_selection = selection()
        port = self.port(preflight=lambda _offline: next(observations))
        parameters = {
            "profile": "exact",
            "selection": exact_selection,
        }
        preview = port.preview(parameters)

        result = port.execute(
            "setup",
            {
                **parameters,
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )

        self.assertEqual(result["state"], "complete")
        self.assertEqual(
            result["preflight"]["disk_free_bytes"],
            self.facts.disk_free_bytes - 4096,
        )
        self.assertTrue(self.runtime.calls)

    def test_confirmation_rechecks_recommended_profile_disk_requirement(self):
        minimum_disk_bytes = 100 * GIB
        resolver = SetupResolver(
            (
                RecommendedProfile(
                    "workstation",
                    64 * GIB,
                    selection(),
                    minimum_disk_bytes=minimum_disk_bytes,
                ),
            )
        )
        observations = iter(
            (
                self.facts,
                replace(
                    self.facts,
                    disk_free_bytes=minimum_disk_bytes - 1,
                ),
            )
        )
        port = self.port(
            resolver=resolver,
            preflight=lambda _offline: next(observations),
        )
        preview = port.preview({})

        result = port.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )

        self.assertEqual(result["state"], "no_validated_fit")
        self.assertEqual(result["mutation_count"], 0)
        self.assertEqual(self.runtime.calls, [])

    def test_edited_recommended_confirmation_rejects_profile_boundary_drift(self):
        high_minimum_disk_bytes = 100 * GIB
        shared_selection = selection()
        resolver = SetupResolver(
            (
                RecommendedProfile(
                    "low",
                    64 * GIB,
                    shared_selection,
                    minimum_disk_bytes=10 * GIB,
                ),
                RecommendedProfile(
                    "high",
                    64 * GIB,
                    shared_selection,
                    minimum_disk_bytes=high_minimum_disk_bytes,
                ),
            )
        )
        observations = iter(
            (
                self.facts,
                replace(
                    self.facts,
                    disk_free_bytes=high_minimum_disk_bytes - 1,
                ),
            )
        )
        parameters = {"service_route": "assistant"}
        port = self.port(
            resolver=resolver,
            preflight=lambda _offline: next(observations),
        )
        preview = port.preview(parameters)

        with self.assertRaises(ApplicationError) as raised:
            port.execute(
                "setup",
                {
                    **parameters,
                    "confirmed": True,
                    "preview_fingerprint": preview["preview_fingerprint"],
                },
            )

        self.assertEqual(raised.exception.code, "preview_changed")
        self.assertEqual(self.runtime.calls, [])

    def test_explicit_revision_scoped_trust_is_applied_but_never_inferred(self):
        trusted = selection(trust=("remote_code",))
        port = self.port()
        preview = port.preview({"selection": trusted})
        port.execute(
            "setup",
            {
                "selection": trusted,
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )

        trust = next(call for call in self.config.calls if isinstance(call, TrustModel))
        self.assertEqual(trust.accepted_risks, ("remote_code",))
        self.assertEqual(trust.revision, MODEL_REVISION)

    def test_missing_or_changed_preview_fingerprint_never_mutates(self):
        port = self.port()
        review = port.execute("setup", {"confirmed": True})
        self.assertEqual(review["state"], "review_required")

        with self.assertRaises(ApplicationError) as raised:
            port.execute(
                "setup",
                {"confirmed": True, "preview_fingerprint": "0" * 64},
            )

        self.assertEqual(raised.exception.code, "preview_changed")
        self.assertEqual(self.runtime.calls, [])

    def test_exact_setup_requires_every_exact_identity_field(self) -> None:
        port = self.port()

        with self.assertRaisesRegex(ApplicationError, "exact setup requires"):
            port.preview({"profile": "exact", "model_repository": "acme/model"})

        preview = port.preview(
            {
                "profile": "exact",
                "runtime_name": "optiq",
                "runtime_version": "0.3.3",
                "runtime_lock_digest": "sha256:" + "a" * 64,
                "model_repository": "acme/model",
                "model_revision": "3" * 40,
                "trust_grants": (),
                "service_name": "assistant",
                "gateway_endpoint": "http://127.0.0.1:8766/v1",
            }
        )
        self.assertEqual(preview["profile"], "custom")
        self.assertEqual(preview["selection"]["service_name"], "assistant")

    def test_resume_reuses_durable_runtime_evidence_after_interruption(self):
        failing_model = FakeOwner(fail="model.install")
        port = self.port(model=failing_model)
        preview = port.preview({})
        with self.assertRaises(ApplicationError) as raised:
            port.execute(
                "setup",
                {
                    "confirmed": True,
                    "preview_fingerprint": preview["preview_fingerprint"],
                },
            )
        self.assertEqual(raised.exception.code, "setup_interrupted")
        self.assertEqual(
            [item.step_id for item in self.evidence.items["setup"]],
            [
                "preflight",
                "gateway.configure",
                "supervisor.activate",
                "runtime.install",
                "model.install",
            ],
        )
        self.assertEqual(self.evidence.items["setup"][-1].state, StepState.FAILED)

        resumed = self.port()
        resumed_preview = resumed.preview({})
        resumed.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": resumed_preview["preview_fingerprint"],
            },
        )

        self.assertEqual(len(self.runtime.calls), 1)
        self.assertIsInstance(self.model.calls[0], InstallModel)

    def test_resume_restores_dependency_material_for_the_exact_step_version(self):
        runtime_results = {
            "0.3.3": {
                "installation_id": "optiq-0.3.3-tested",
                "runtime": "optiq",
                "version": "0.3.3",
                "provenance": "tested",
                "bundle_id": "optiq-0.3.3-py3.13-macos-arm64",
                "lock_sha256": "a" * 64,
            },
            "0.3.4": {
                "installation_id": "optiq-0.3.4-tested",
                "runtime": "optiq",
                "version": "0.3.4",
                "provenance": "tested",
                "bundle_id": "optiq-0.3.4-py3.13-macos-arm64",
                "lock_sha256": "c" * 64,
            },
        }
        self.runtime.results["runtime.install"] = lambda parameters: runtime_results[
            parameters["expected_version"]
        ]
        failing_model = FakeOwner(fail="model.install")
        alternate = replace(
            selection(),
            runtime_version="0.3.4",
            runtime_lock_digest="sha256:" + "c" * 64,
        )

        for parameters in ({}, {"selection": alternate}):
            port = self.port(model=failing_model)
            preview = port.preview(parameters)
            with self.assertRaises(ApplicationError):
                port.execute(
                    "setup",
                    {
                        **parameters,
                        "confirmed": True,
                        "preview_fingerprint": preview["preview_fingerprint"],
                    },
                )

        resumed = self.port()
        preview = resumed.preview({})
        resumed.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )

        configured = next(
            operation
            for operation in self.config.calls
            if isinstance(operation, ConfigureService)
        )
        self.assertEqual(configured.runtime, "optiq-0.3.3-tested")

        resumed_alternate = self.port()
        alternate_preview = resumed_alternate.preview({"selection": alternate})
        resumed_alternate.execute(
            "setup",
            {
                "selection": alternate,
                "confirmed": True,
                "preview_fingerprint": alternate_preview["preview_fingerprint"],
            },
        )
        configured_alternate = [
            operation
            for operation in self.config.calls
            if isinstance(operation, ConfigureService)
        ][-1]
        self.assertEqual(configured_alternate.runtime, "optiq-0.3.4-tested")
        self.assertEqual(len(self.runtime.calls), 2)

    def test_resume_reexecutes_only_invalid_dependency_material(self) -> None:
        plans = FakePlanStore()
        port = self.port(plan_store=plans)
        preview = port.preview({})
        completed = port.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )
        self.assertEqual(completed["completion"], "complete")

        cases = (
            ("runtime-malformed", "runtime.install", "{", self.runtime),
            (
                "runtime-wrong-exact-shape",
                "runtime.install",
                json.dumps(
                    {
                        "result": {
                            "installation_id": "other@9.9.9",
                            "runtime": "other",
                            "version": "9.9.9",
                            "provenance": "tested",
                            "bundle_id": "other-bundle",
                            "lock_sha256": "b" * 64,
                        }
                    }
                ),
                self.runtime,
            ),
            (
                "model-malformed",
                "model.install",
                json.dumps({"result": {"revision": "wrong"}}),
                self.model,
            ),
            (
                "model-wrong-exact-shape",
                "model.install",
                json.dumps(
                    {
                        "result": {
                            "installation_id": "qwen-optiq@wrong",
                            "revision": "2" * 40,
                        }
                    }
                ),
                self.model,
            ),
        )
        for case, step_id, detail, owner in cases:
            with self.subTest(case=case):
                index, evidence = next(
                    (index, item)
                    for index, item in reversed(
                        tuple(enumerate(self.evidence.items["setup"]))
                    )
                    if item.step_id == step_id and item.state is StepState.COMPLETE
                )
                self.evidence.items["setup"][index] = replace(evidence, detail=detail)
                owner.calls.clear()
                self.application_targets.calls.clear()
                self.verifier.calls.clear()

                resumed = port.preview({})
                producer = next(
                    step for step in resumed["steps"] if step["id"] == step_id
                )
                self.assertEqual(producer["state"], "ready")
                self.assertEqual(resumed["completion"], "partial")
                durable = DurableSetupOutcomeProvider(plans, self.evidence).outcome()
                self.assertEqual(durable["completion"], "partial")

                repaired = port.execute(
                    "setup",
                    {
                        "confirmed": True,
                        "preview_fingerprint": resumed["preview_fingerprint"],
                    },
                )

                self.assertEqual(repaired["completion"], "complete")
                self.assertEqual(len(owner.calls), 1)
                self.assertIsInstance(
                    owner.calls[0],
                    InstallRuntime if step_id == "runtime.install" else InstallModel,
                )
                self.assertEqual(
                    [
                        call
                        for call in self.application_targets.calls
                        if isinstance(call, TestApplicationTarget)
                    ],
                    [],
                )
                self.assertEqual(self.verifier.calls, [])

    def test_offline_missing_artifacts_block_before_any_owner_runs(self):
        port = self.port()
        preview = port.preview({"offline": True})
        runtime = next(
            step for step in preview["steps"] if step["id"] == "runtime.install"
        )
        self.assertEqual(runtime["state"], "blocked")

        with self.assertRaises(ApplicationError) as raised:
            port.execute(
                "setup",
                {
                    "offline": True,
                    "confirmed": True,
                    "preview_fingerprint": preview["preview_fingerprint"],
                },
            )

        self.assertEqual(raised.exception.code, "offline_blocked")
        self.assertEqual(self.runtime.calls, [])

    def test_removal_preview_retains_shared_cache_and_unrelated_settings(self):
        self.inventory = replace(
            self.inventory,
            owned_applications=("hindsight",),
            retained_applications=("codex",),
        )
        port = self.port()
        preview = port.preview_removal()

        self.assertEqual(preview["state"], "review_required")
        self.assertEqual(
            preview["retained_paths"], list(self.inventory.shared_cache_paths)
        )
        self.assertEqual(
            preview["retained_settings"],
            [*self.inventory.unrelated_settings, "codex", "hindsight"],
        )
        self.assertEqual(
            self.supervisor.calls
            + self.applications.calls
            + self.application_targets.calls
            + self.config.calls,
            [],
        )

        result = port.remove(
            {
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            }
        )

        self.assertEqual(result["state"], "complete")
        self.assertEqual(
            [type(call) for call in self.supervisor.calls],
            [DrainService, StopService, UnregisterSupervisor],
        )
        self.assertEqual(
            [type(call) for call in self.application_targets.calls],
            [RemoveApplicationTarget, RemoveApplicationTarget],
        )
        self.assertEqual(
            [call.application_target for call in self.application_targets.calls],
            ["codex", "hindsight"],
        )
        self.assertEqual(self.applications.calls, [])
        state_remove = next(
            call for call in self.config.calls if isinstance(call, RemoveState)
        )
        self.assertEqual(state_remove.paths, self.inventory.product_owned_paths)
        self.assertNotIn(self.inventory.shared_cache_paths[0], state_remove.paths)

    def test_setup_and_removal_use_their_distinct_coordination_boundaries(self):
        events = []

        @contextmanager
        def setup_transition():
            events.append("setup-enter")
            try:
                yield
            finally:
                events.append("setup-exit")

        @contextmanager
        def removal_transition():
            events.append("removal-enter")
            try:
                yield
            finally:
                events.append("removal-exit")

        port = self.port(
            transition=setup_transition,
            removal_transition=removal_transition,
        )
        setup_preview = port.preview({})
        port.execute(
            "setup",
            {
                "confirmed": True,
                "preview_fingerprint": setup_preview["preview_fingerprint"],
            },
        )
        removal_preview = port.preview_removal()
        port.remove(
            {
                "confirmed": True,
                "preview_fingerprint": removal_preview["preview_fingerprint"],
            }
        )

        self.assertEqual(
            events,
            ["setup-enter", "setup-exit", "removal-enter", "removal-exit"],
        )

    def test_operational_evidence_adapter_round_trips_content_free_evidence(self):
        state = FakeOperationalState()
        evidence = OperationalSetupEvidenceStore(state)
        first = self.resolver.resolve(self.facts).steps[0]

        evidence.record(
            "setup", SetupEvidence.complete(first, json.dumps({"ok": True}))
        )

        restored = evidence.load("setup")
        self.assertEqual(restored[0].step_id, "preflight")
        self.assertEqual(restored[0].fingerprint, first.fingerprint)
        self.assertEqual(state.rows[0]["kind"], "setup_evidence")
        self.assertNotIn("prompt", json.dumps(state.rows[0]))

    def test_operational_evidence_adapter_records_failure_then_success(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = OperationalSetupEvidenceStore(
                OperationalStateStore(Path(directory) / "state.sqlite3")
            )
            failed = SetupEvidence(
                "model.install", "exact-plan", StepState.FAILED, "interrupted"
            )
            completed = SetupEvidence(
                "model.install", "exact-plan", StepState.COMPLETE, "installed"
            )

            evidence.record("setup", failed)
            evidence.record("setup", completed)

            self.assertEqual(evidence.load("setup"), (failed, completed))


if __name__ == "__main__":
    unittest.main()
