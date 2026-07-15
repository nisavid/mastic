import unittest

from mlxctl.application.setup import (
    ExactSetupSelection,
    PlanExecutionError,
    RecommendedProfile,
    RemovalInventory,
    SetupEvidence,
    SetupPlanner,
    SetupPreflight,
    SetupRequest,
    StepState,
)


GIB = 1024**3


def _selection(*, service: str, revision: str) -> ExactSetupSelection:
    return ExactSetupSelection(
        runtime_name="optiq",
        runtime_version="0.2.18",
        runtime_lock_digest="sha256:" + "a" * 64,
        model_repository="mlx-community/example-OptiQ-4bit",
        model_revision=revision,
        trust_grants=(),
        service_name=service,
        gateway_endpoint="http://127.0.0.1:8766/v1",
        clients=("codex", "hindsight"),
        sampling_profiles={
            "coding": {"temperature": 0.0, "top_p": 0.95},
            "memory-reflect": {"temperature": 0.9, "top_p": 0.95},
        },
        service_options={"kv_config": "kv_config.json", "mtp": True},
    )


class SetupV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.compact = RecommendedProfile(
            "compact", 16 * GIB, _selection(service="compact", revision="1" * 40)
        )
        self.workstation = RecommendedProfile(
            "workstation",
            64 * GIB,
            _selection(service="coding", revision="2" * 40),
        )
        self.planner = SetupPlanner((self.compact, self.workstation))

    def test_guided_plan_preselects_a_machine_aware_editable_exact_profile(
        self,
    ) -> None:
        plan = self.planner.plan(
            SetupPreflight(
                platform="darwin",
                machine="arm64",
                memory_bytes=96 * GIB,
                disk_free_bytes=300 * GIB,
                online=True,
            )
        )

        preview = self.planner.preview(plan)

        self.assertEqual(plan.profile_name, "workstation")
        self.assertTrue(preview.editable)
        self.assertEqual(preview.runtime, "optiq==0.2.18")
        self.assertEqual(preview.model_revision, "2" * 40)
        self.assertEqual(preview.service_name, "coding")
        self.assertEqual(preview.model_alias, "coding")
        self.assertEqual(preview.service_route, "coding")
        self.assertEqual(preview.activation, "manual")
        self.assertFalse(preview.pinned)
        self.assertEqual(preview.service_options["kv_config"], "kv_config.json")
        self.assertEqual(preview.gateway_endpoint, "http://127.0.0.1:8766/v1")
        self.assertEqual(preview.clients, ("codex", "hindsight"))
        self.assertEqual(preview.sampling_profiles["coding"]["temperature"], 0.0)

    def test_guided_setup_never_falls_back_to_an_oversized_profile(self) -> None:
        undersized = SetupPreflight(
            "darwin",
            "arm64",
            memory_bytes=8 * GIB,
            disk_free_bytes=8 * GIB,
            online=True,
        )

        with self.assertRaisesRegex(ValueError, "no recommended setup profile fits"):
            self.planner.plan(undersized)

        expert = self.planner.plan(
            undersized,
            SetupRequest(selection=self.compact.selection),
        )
        self.assertEqual(expert.profile_name, "custom")

    def test_service_identity_and_options_are_exact_immutable_plan_inputs(self) -> None:
        facts = SetupPreflight("darwin", "arm64", 64 * GIB, 200 * GIB, True)
        options = {
            "kv_config": "kv_config.json",
            "mtp": True,
            "runtime": {"draft_tokens": 4},
            "stop": ["</s>", 17],
        }
        exact = ExactSetupSelection(
            runtime_name="optiq",
            runtime_version="0.3.3",
            runtime_lock_digest="sha256:" + "a" * 64,
            model_repository="mlx-community/example",
            model_revision="3" * 40,
            trust_grants=(),
            service_name="internal-worker",
            model_alias="qwen-optiq",
            service_route="coding",
            activation="supervisor",
            pinned=True,
            service_options=options,
            gateway_endpoint="http://127.0.0.1:8766/v1",
        )

        plan = self.planner.plan(facts, SetupRequest(selection=exact))
        service = next(step for step in plan.steps if step.id == "service.configure")
        gateway = next(step for step in plan.steps if step.id == "gateway.configure")
        verify = next(step for step in plan.steps if step.id == "verify.request")

        options["mtp"] = False
        self.assertTrue(exact.service_options["mtp"])
        self.assertEqual(exact.service_options["runtime"]["draft_tokens"], 4)
        with self.assertRaises(TypeError):
            exact.service_options["mtp"] = False  # type: ignore[index]
        self.assertEqual(service.inputs["model_alias"], "qwen-optiq")
        self.assertEqual(service.inputs["route"], "coding")
        self.assertEqual(service.inputs["activation"], "supervisor")
        self.assertTrue(service.inputs["pinned"])
        self.assertEqual(gateway.inputs["route"], "coding")
        self.assertEqual(verify.inputs["model"], "coding")

    def test_service_names_activation_and_json_options_are_validated(self) -> None:
        facts = SetupPreflight("darwin", "arm64", 64 * GIB, 200 * GIB, True)
        invalid_name = ExactSetupSelection(
            runtime_name="optiq",
            runtime_version="0.3.3",
            runtime_lock_digest="sha256:" + "a" * 64,
            model_repository="mlx-community/example",
            model_revision="3" * 40,
            trust_grants=(),
            service_name="not safe",
            gateway_endpoint="http://127.0.0.1:8766/v1",
        )
        with self.assertRaisesRegex(ValueError, "resource name"):
            self.planner.plan(facts, SetupRequest(selection=invalid_name))

        with self.assertRaisesRegex(ValueError, "service_options"):
            ExactSetupSelection(
                runtime_name="optiq",
                runtime_version="0.3.3",
                runtime_lock_digest="sha256:" + "a" * 64,
                model_repository="mlx-community/example",
                model_revision="3" * 40,
                trust_grants=(),
                service_name="coding",
                gateway_endpoint="http://127.0.0.1:8766/v1",
                service_options={"bad": {1, 2}},
            )

    def test_exact_noninteractive_setup_requires_explicit_trust_and_confirmation(
        self,
    ) -> None:
        facts = SetupPreflight("darwin", "arm64", 64 * GIB, 200 * GIB, True)
        incomplete = ExactSetupSelection(
            runtime_name="optiq",
            runtime_version="0.2.18",
            runtime_lock_digest="sha256:" + "a" * 64,
            model_repository="mlx-community/example",
            model_revision="3" * 40,
            trust_grants=None,
            service_name="coding",
            gateway_endpoint="http://127.0.0.1:8766/v1",
        )

        with self.assertRaisesRegex(ValueError, "trust_grants"):
            self.planner.plan(
                facts,
                SetupRequest(selection=incomplete, noninteractive=True, confirmed=True),
            )
        with self.assertRaisesRegex(ValueError, "confirmed"):
            self.planner.plan(
                facts,
                SetupRequest(
                    selection=self.workstation.selection,
                    noninteractive=True,
                    confirmed=False,
                ),
            )

        not_locked = ExactSetupSelection(
            runtime_name="optiq",
            runtime_version="0.2.18",
            runtime_lock_digest="sha256:short",
            model_repository="mlx-community/example",
            model_revision="3" * 40,
            trust_grants=(),
            service_name="coding",
            gateway_endpoint="http://127.0.0.1:8766/v1",
        )
        with self.assertRaisesRegex(ValueError, "runtime_lock_digest"):
            self.planner.plan(
                facts,
                SetupRequest(selection=not_locked, noninteractive=True, confirmed=True),
            )

        hostname_endpoint = ExactSetupSelection(
            runtime_name="optiq",
            runtime_version="0.3.3",
            runtime_lock_digest="sha256:" + "a" * 64,
            model_repository="mlx-community/example",
            model_revision="3" * 40,
            trust_grants=(),
            service_name="coding",
            gateway_endpoint="http://localhost:8766/v1",
        )
        with self.assertRaisesRegex(ValueError, "literal HTTP loopback"):
            self.planner.plan(
                facts,
                SetupRequest(
                    selection=hostname_endpoint,
                    noninteractive=True,
                    confirmed=True,
                ),
            )

    def test_resume_skips_a_completed_download_and_ends_with_a_real_request(
        self,
    ) -> None:
        facts = SetupPreflight("darwin", "arm64", 64 * GIB, 200 * GIB, True)
        initial = self.planner.plan(facts)
        model_step = next(step for step in initial.steps if step.id == "model.install")
        evidence = SetupEvidence.complete(model_step)
        resumed = self.planner.plan(facts, evidence=(evidence,))
        executed: list[str] = []

        result = self.planner.apply(
            resumed,
            lambda step: executed.append(step.id) or SetupEvidence.complete(step),
            evidence=(evidence,),
        )

        self.assertNotIn("model.install", executed)
        self.assertEqual(executed[-1], "verify.request")
        self.assertEqual(result.evidence[-1].step_id, "verify.request")
        self.assertTrue(result.complete)

    def test_apply_records_only_completed_steps_before_a_failure(self) -> None:
        facts = SetupPreflight("darwin", "arm64", 64 * GIB, 200 * GIB, True)
        plan = self.planner.plan(facts)
        recorded: list[SetupEvidence] = []

        def execute(step):
            if step.id == "model.install":
                raise RuntimeError("download interrupted")
            return SetupEvidence.complete(step)

        with self.assertRaises(PlanExecutionError) as failure:
            self.planner.apply(plan, execute, record=recorded.append)

        self.assertEqual(failure.exception.step_id, "model.install")
        self.assertEqual(
            [item.step_id for item in recorded], ["preflight", "runtime.install"]
        )

    def test_offline_plan_exposes_evidence_and_blocks_missing_network_artifacts(
        self,
    ) -> None:
        plan = self.planner.plan(
            SetupPreflight("darwin", "arm64", 64 * GIB, 200 * GIB, False)
        )

        self.assertTrue(plan.offline)
        runtime = next(step for step in plan.steps if step.id == "runtime.install")
        model = next(step for step in plan.steps if step.id == "model.install")
        self.assertEqual(runtime.state, StepState.BLOCKED)
        self.assertIn("offline", runtime.reason)
        self.assertEqual(model.state, StepState.BLOCKED)
        self.assertIn("No completed evidence", self.planner.preview(plan).offline_note)

    def test_removal_is_reference_aware_and_retains_shared_and_unrelated_state(
        self,
    ) -> None:
        inventory = RemovalInventory(
            running_services=("coding",),
            registered=True,
            client_integrations=("codex", "hindsight"),
            product_owned_paths=("~/.config/mlxctl", "~/.local/state/mlxctl"),
            product_owned_bytes=2 * GIB,
            shared_cache_paths=("~/.cache/huggingface/hub/models--example",),
            shared_cache_bytes=40 * GIB,
            references={"coding": ("optiq@0.2.18", "example@" + "2" * 40)},
            unrelated_settings=("Codex theme", "Hindsight bank ID"),
        )

        plan = self.planner.plan_removal(inventory)

        self.assertEqual(
            tuple(step.id for step in plan.steps),
            (
                "service.drain",
                "service.stop",
                "supervisor.unregister",
                "client.remove",
                "state.remove",
            ),
        )
        self.assertEqual(plan.freed_bytes_estimate, 2 * GIB)
        self.assertEqual(plan.retained_paths, inventory.shared_cache_paths)
        self.assertEqual(plan.retained_settings, inventory.unrelated_settings)
        self.assertIn("coding", plan.references)


if __name__ == "__main__":
    unittest.main()
