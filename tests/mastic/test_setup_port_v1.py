import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

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
from mastic.infrastructure.state_store import OperationalStateStore
from mastic.infrastructure.setup_port import (
    OperationalSetupEvidenceStore,
    SetupOperationPort,
)


GIB = 1024**3
MODEL_REPOSITORY = "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit"
MODEL_REVISION = "70a3aa32c7feef511182bf16aa332f37e8d82014"


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


class FakeEvidenceStore:
    def __init__(self):
        self.items = {"setup": [], "removal": []}

    def load(self, scope):
        return tuple(self.items[scope])

    def record(self, scope, evidence):
        self.items[scope].append(evidence)


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


class SetupOperationPortTests(unittest.TestCase):
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
                        "codex": {"version": "0.144.1", "provenance": "adopted"},
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
                        "phases": ["application.native"],
                        "evidence_sha256": "b" * 64,
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

    def port(self, *, model=None, facts=None, performance_profile=None):
        return SetupOperationPort(
            self.resolver,
            preflight=lambda offline: (
                facts
                or SetupPreflight(
                    self.facts.platform,
                    self.facts.machine,
                    self.facts.memory_bytes,
                    self.facts.disk_free_bytes,
                    self.facts.online and not offline,
                    os_version=self.facts.os_version,
                )
            ),
            runtime=self.runtime,
            model=model or self.model,
            config=self.config,
            applications=self.applications,
            application_targets=self.application_targets,
            supervisor=self.supervisor,
            verifier=self.verifier,
            evidence=self.evidence,
            removal_inventory=lambda: self.inventory,
            performance_profile=performance_profile,
        )

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
                "id": "phase1-qwen36-optiq-apple-silicon",
                "version": 1,
                "status": "provisional",
                "host": {
                    "platform": "darwin",
                    "machine": "arm64",
                    "minimum_memory_bytes": 48 * GIB,
                    "macos_major_versions": [15, 26],
                },
                "plan": {
                    "selection_sha256": "8bedb6280a52b8433da54a485e43b537714980511ae312cd81b8a82769402b56",
                    "application_versions": {
                        "codex": "0.144.1",
                        "hindsight": "0.8.4",
                    },
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
            },
        )
        self.assertEqual(
            self.runtime.calls
            + self.model.calls
            + self.config.calls
            + self.applications.calls
            + self.application_targets.calls
            + self.supervisor.calls,
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
            + self.supervisor.calls,
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
        self.assertEqual(
            self.runtime.calls[0],
            (
                "runtime.install",
                {
                    "runtime": "optiq",
                    "channel": "tested",
                    "expected_version": "0.3.3",
                    "expected_lock_digest": "a" * 64,
                    "confirmed": True,
                },
            ),
        )
        self.assertEqual(self.model.calls[0][0], "model.install")
        self.assertEqual(
            self.applications.calls,
            [
                (
                    "application.install",
                    {
                        "application_targets": ("codex", "hindsight"),
                        "offline": False,
                        "confirmed": True,
                    },
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
        self.assertEqual(self.model.calls[0][1]["revision"], MODEL_REVISION)
        self.assertEqual(self.model.calls[0][1]["alias"], "qwen-optiq")
        service = next(
            call for call in self.config.calls if call[0] == "service.create"
        )
        self.assertEqual(service[1]["resource"], "coding")
        self.assertEqual(service[1]["runtime"], "optiq-0.3.3-tested")
        self.assertEqual(service[1]["model_alias"], "qwen-optiq")
        self.assertEqual(service[1]["route"], "engineering")
        self.assertEqual(service[1]["activation"], "supervisor")
        self.assertTrue(service[1]["pinned"])
        self.assertEqual(
            service[1]["options"],
            {
                "kv_config": "kv_config.json",
                "mtp": True,
                "runtime": {"draft_tokens": 4},
            },
        )
        self.assertEqual(self.supervisor.calls[0][0], "supervisor.start")
        self.assertEqual(
            self.supervisor.calls[-1], ("service.start", {"resource": "coding"})
        )
        self.assertEqual(self.verifier.calls, [])
        self.assertEqual(
            [
                call[1]["application_target"]
                for call in self.application_targets.calls[-2:]
            ],
            ["codex", "hindsight"],
        )
        self.assertEqual(
            {
                call[1]["service"]
                for call in self.application_targets.calls
                if call[0] == "application-target.configure"
            },
            {"coding"},
        )
        self.assertEqual(len(self.evidence.items["setup"]), 11)
        verification_evidence = self.evidence.items["setup"][-1].detail
        self.assertNotIn("mastic ready", verification_evidence)
        self.assertIn("evidence_sha256", verification_evidence)

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
            if call[0] == "application-target.test"
        ]
        self.assertEqual(
            [call[1]["application_target"] for call in tests],
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
                    "phases": ["application.native"],
                    "evidence_sha256": "b" * 64,
                },
            }
        )
        port = self.port(
            performance_profile=validated_performance_profile(
                plan_sha256="7316e2d9b7271228199254ed30b0d89f243d4ad821502fbbc074c5a9654f5f60"
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
                "ok": True,
                "exact_contract": True,
                "phases": ["application.native"],
                "evidence_sha256": "b" * 64,
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

    def test_skipped_required_canary_precedes_a_degraded_target(self) -> None:
        self.application_targets.results["application-target.test"] = (
            lambda parameters: {
                "profile": parameters["profile"],
                "response": {
                    "ok": True,
                    "exact_contract": True,
                    "duration_seconds": 61.0,
                    "phases": ["application.native"],
                    "evidence_sha256": "b" * 64,
                },
            }
        )
        port = self.port(
            performance_profile=validated_performance_profile(
                plan_sha256="7316e2d9b7271228199254ed30b0d89f243d4ad821502fbbc074c5a9654f5f60"
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
        port = self.port()
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
        profile = validated_performance_profile(
            plan_sha256="7316e2d9b7271228199254ed30b0d89f243d4ad821502fbbc074c5a9654f5f60"
        )

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
                    "phases": ["application.native"],
                    "evidence_sha256": "b" * 64,
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
        self.application_targets.results["application-target.test"] = (
            lambda parameters: {
                "profile": parameters["profile"],
                "response": {
                    "ok": True,
                    "exact_contract": True,
                    "duration_seconds": 12.0,
                    "phases": ["application.native"],
                    "evidence_sha256": "b" * 64,
                    "contract": parameters["application_target"],
                },
            }
        )
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
            if call[0] == "application-target.test"
        ]
        self.assertEqual(
            tests,
            [
                (
                    "application-target.test",
                    {"application_target": "codex", "profile": "coding"},
                ),
                (
                    "application-target.test",
                    {"application_target": "hindsight", "profile": "retain"},
                ),
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
                    "evidence_sha256": "b" * 64,
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
                    "evidence_sha256": "b" * 64,
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
            self.application_targets.calls,
            [
                (
                    "application-target.test",
                    {"application_target": "hindsight", "profile": "retain"},
                )
            ],
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

    def test_explicit_revision_scoped_trust_is_applied_but_never_inferred(self):
        trusted = selection(trust=("remote_code",))
        preview = self.port().preview({"selection": trusted})
        self.port().execute(
            "setup",
            {
                "selection": trusted,
                "confirmed": True,
                "preview_fingerprint": preview["preview_fingerprint"],
            },
        )

        trust = next(call for call in self.config.calls if call[0] == "model.trust")
        self.assertEqual(trust[1]["accepted_risks"], ("remote_code",))
        self.assertEqual(trust[1]["revision"], MODEL_REVISION)

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
        self.assertEqual(self.model.calls[0][0], "model.install")

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
            parameters
            for operation, parameters in self.config.calls
            if operation == "service.create"
        )
        self.assertEqual(configured["runtime"], "optiq-0.3.3-tested")
        self.assertEqual(len(self.runtime.calls), 2)

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
            [*self.inventory.unrelated_settings, "codex"],
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
            [call[0] for call in self.supervisor.calls],
            ["service.drain", "service.stop", "supervisor.unregister"],
        )
        self.assertEqual(
            [call[0] for call in self.application_targets.calls],
            ["application-target.remove", "application-target.remove"],
        )
        self.assertEqual(
            self.applications.calls,
            [
                (
                    "application.remove",
                    {"applications": ("hindsight",), "confirmed": True},
                )
            ],
        )
        state_remove = next(
            call for call in self.config.calls if call[0] == "state.remove"
        )
        self.assertEqual(state_remove[1]["paths"], self.inventory.product_owned_paths)
        self.assertNotIn(self.inventory.shared_cache_paths[0], state_remove[1]["paths"])

    def test_operational_evidence_adapter_round_trips_content_free_evidence(self):
        state = FakeOperationalState()
        evidence = OperationalSetupEvidenceStore(state)
        port = self.port()
        resolved = port.preview({})
        first = self.resolver.resolve(self.facts).steps[0]

        from mastic.application.setup import SetupEvidence

        evidence.record(
            "setup", SetupEvidence.complete(first, json.dumps({"ok": True}))
        )

        restored = evidence.load("setup")
        self.assertEqual(restored[0].step_id, "preflight")
        self.assertEqual(restored[0].fingerprint, first.fingerprint)
        self.assertEqual(state.rows[0]["kind"], "setup_evidence")
        self.assertNotIn("prompt", json.dumps(state.rows[0]))
        self.assertEqual(len(resolved["preview_fingerprint"]), 64)

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
