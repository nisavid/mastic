import unittest
from dataclasses import replace

from mastic.application.setup import (
    CapacityProfile,
    ExactSetupSelection,
    MutationExecutionError,
    RecommendedProfile,
    RemovalInventory,
    SetupEvidence,
    SetupResolver,
    SetupPreflight,
    SetupRequest,
    StepState,
    _fingerprint,
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
        application_targets=("codex", "hindsight"),
        application_target_options={"hindsight": {"profile": "default"}},
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
        self.resolver = SetupResolver((self.compact, self.workstation))

    def test_capacity_profile_coherently_caps_service_and_application_targets(
        self,
    ) -> None:
        resolver = SetupResolver(
            (self.compact,),
            capacity_profiles=(
                CapacityProfile(
                    "balanced",
                    "Balanced",
                    context_window=131_072,
                    max_concurrent=6,
                    projected_kv_bytes=5_737_807_872,
                    prompt_cache_bytes=2 * GIB,
                    description="Most parallel agents with long context.",
                ),
                CapacityProfile(
                    "native-context",
                    "Native context",
                    context_window=262_144,
                    max_concurrent=3,
                    projected_kv_bytes=5_737_807_872,
                    prompt_cache_bytes=2 * GIB,
                    description="Longest context with fewer simultaneous requests.",
                ),
            ),
            default_capacity_profile="balanced",
        )
        facts = SetupPreflight("darwin", "arm64", 48 * GIB, 200 * GIB, True)

        balanced = resolver.preview(resolver.resolve(facts))
        native = resolver.preview(
            resolver.resolve(facts, SetupRequest(capacity_profile="native-context"))
        )

        self.assertEqual(balanced.capacity_profile, "balanced")
        self.assertEqual(balanced.context_window, 131_072)
        self.assertEqual(balanced.service_options["max_context"], 131_072)
        self.assertEqual(balanced.service_options["max_concurrent"], 6)
        self.assertEqual(balanced.service_options["prompt_cache_bytes"], 2 * GIB)
        self.assertEqual(balanced.projected_kv_bytes, 5_737_807_872)
        self.assertEqual(native.capacity_profile, "native-context")
        self.assertEqual(native.context_window, 262_144)
        self.assertEqual(native.service_options["max_concurrent"], 3)

    def test_selected_application_target_context_cannot_exceed_service_capacity(
        self,
    ) -> None:
        invalid = _selection(service="coding", revision="3" * 40)
        invalid = replace(
            invalid,
            service_options={
                "kv_config": "kv_config.json",
                "max_context": 131_072,
                "max_concurrent": 6,
            },
            context_window=196_608,
        )

        with self.assertRaisesRegex(ValueError, "context_window.*max_context"):
            invalid.validate_exact()

    def test_exact_selection_is_not_overwritten_by_default_capacity(self) -> None:
        resolver = SetupResolver(
            (self.compact,),
            capacity_profiles=(
                CapacityProfile(
                    "balanced",
                    "Balanced",
                    131_072,
                    6,
                    5_737_807_872,
                    2 * GIB,
                    "Default capacity.",
                ),
            ),
            default_capacity_profile="balanced",
        )
        exact = replace(
            _selection(service="coding", revision="3" * 40),
            service_options={
                "kv_config": "kv_config.json",
                "max_context": 262_144,
                "max_concurrent": 3,
            },
            context_window=262_144,
        )

        resolved = resolver.resolve(
            SetupPreflight("darwin", "arm64", 48 * GIB, 200 * GIB, True),
            SetupRequest(selection=exact, noninteractive=True, confirmed=True),
        )

        self.assertIsNone(resolved.capacity_profile)
        self.assertEqual(resolved.selection.context_window, 262_144)
        self.assertEqual(resolved.selection.service_options["max_concurrent"], 3)

    def test_guided_preview_preselects_a_machine_aware_editable_exact_profile(
        self,
    ) -> None:
        resolved = self.resolver.resolve(
            SetupPreflight(
                platform="darwin",
                machine="arm64",
                memory_bytes=96 * GIB,
                disk_free_bytes=300 * GIB,
                online=True,
            )
        )

        preview = self.resolver.preview(resolved)

        self.assertEqual(resolved.profile_name, "workstation")
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
        self.assertEqual(preview.application_targets, ("codex", "hindsight"))
        self.assertEqual(
            preview.application_target_options["hindsight"]["profile"], "default"
        )
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
            self.resolver.resolve(undersized)

        exact = self.resolver.resolve(
            undersized,
            SetupRequest(selection=self.compact.selection),
        )
        self.assertEqual(exact.profile_name, "custom")

    def test_selected_codex_target_has_its_own_resumable_canary_step(self) -> None:
        exact = replace(
            self.workstation.selection,
            application_targets=("codex",),
            application_target_options={},
        )

        resolved = self.resolver.resolve(
            SetupPreflight("darwin", "arm64", 64 * GIB, 200 * GIB, True),
            SetupRequest(selection=exact, confirmed=True),
        )

        canary = resolved.steps[-1]
        self.assertEqual(canary.id, "gateway.contract.codex")
        self.assertEqual(
            dict(canary.inputs),
            {
                "target": "codex",
                "profile": "coding",
                "service": "coding",
                "route": "coding",
                "endpoint": "http://127.0.0.1:8766/v1",
                "request": "Respond with exactly: mastic gateway contract ok",
            },
        )

    def test_selected_hindsight_target_has_its_own_resumable_canary_step(self) -> None:
        exact = replace(
            self.workstation.selection,
            application_targets=("hindsight",),
            application_target_options={"hindsight": {"profile": "project-memory"}},
        )

        resolved = self.resolver.resolve(
            SetupPreflight("darwin", "arm64", 64 * GIB, 200 * GIB, True),
            SetupRequest(selection=exact, confirmed=True),
        )

        canary = resolved.steps[-1]
        self.assertEqual(canary.id, "gateway.contract.hindsight")
        self.assertEqual(
            dict(canary.inputs),
            {
                "target": "hindsight",
                "configuration_profile": "project-memory",
                "profile": "retain",
                "service": "coding",
                "route": "coding",
                "endpoint": "http://127.0.0.1:8766/v1",
                "request": "Respond with exactly: mastic gateway contract ok",
            },
        )

    def test_service_identity_and_options_are_exact_immutable_preview_inputs(self) -> None:
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

        resolved = self.resolver.resolve(facts, SetupRequest(selection=exact))
        service = next(
            step for step in resolved.steps if step.id == "service.configure"
        )
        gateway = next(
            step for step in resolved.steps if step.id == "gateway.configure"
        )
        verify = next(step for step in resolved.steps if step.id == "verify.request")

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
            self.resolver.resolve(facts, SetupRequest(selection=invalid_name))

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

    def test_unsupported_setup_target_uses_canonical_name(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "unsupported Application Configuration Targets in setup: other",
        ):
            replace(
                self.workstation.selection, application_targets=("other",)
            ).validate_exact()

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
            self.resolver.resolve(
                facts,
                SetupRequest(selection=incomplete, noninteractive=True, confirmed=True),
            )
        with self.assertRaisesRegex(ValueError, "confirmed"):
            self.resolver.resolve(
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
            self.resolver.resolve(
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
            self.resolver.resolve(
                facts,
                SetupRequest(
                    selection=hostname_endpoint,
                    noninteractive=True,
                    confirmed=True,
                ),
            )

    def test_resume_skips_a_completed_download_and_ends_with_target_canaries(
        self,
    ) -> None:
        facts = SetupPreflight("darwin", "arm64", 64 * GIB, 200 * GIB, True)
        initial = self.resolver.resolve(facts)
        model_step = next(step for step in initial.steps if step.id == "model.install")
        evidence = SetupEvidence.complete(model_step)
        resumed = self.resolver.resolve(facts, evidence=(evidence,))
        executed: list[str] = []

        result = self.resolver.apply(
            resumed,
            lambda step: executed.append(step.id) or SetupEvidence.complete(step),
            evidence=(evidence,),
        )

        self.assertNotIn("model.install", executed)
        self.assertEqual(
            executed[-2:], ["gateway.contract.codex", "gateway.contract.hindsight"]
        )
        self.assertEqual(result.evidence[-1].step_id, "gateway.contract.hindsight")
        self.assertTrue(result.complete)

    def test_changed_supervisor_protocol_invalidates_old_activation_evidence(
        self,
    ) -> None:
        facts = SetupPreflight("darwin", "arm64", 64 * GIB, 200 * GIB, True)
        old_fingerprint = _fingerprint(
            "supervisor.activate",
            {"reason": "install runtimes, models, and start the selected service"},
        )
        old_evidence = SetupEvidence(
            "supervisor.activate", old_fingerprint, StepState.COMPLETE
        )

        resumed = self.resolver.resolve(facts, evidence=(old_evidence,))
        activation = next(
            step for step in resumed.steps if step.id == "supervisor.activate"
        )

        self.assertEqual(activation.state, StepState.READY)
        self.assertNotEqual(activation.fingerprint, old_fingerprint)

    def test_apply_records_only_completed_steps_before_a_failure(self) -> None:
        facts = SetupPreflight("darwin", "arm64", 64 * GIB, 200 * GIB, True)
        resolved = self.resolver.resolve(facts)
        recorded: list[SetupEvidence] = []

        def execute(step):
            if step.id == "model.install":
                raise RuntimeError("download interrupted")
            return SetupEvidence.complete(step)

        with self.assertRaises(MutationExecutionError) as failure:
            self.resolver.apply(resolved, execute, record=recorded.append)

        self.assertEqual(failure.exception.step_id, "model.install")
        self.assertEqual(
            [item.step_id for item in recorded],
            [
                "preflight",
                "gateway.configure",
                "supervisor.activate",
                "runtime.install",
            ],
        )

    def test_offline_preview_exposes_evidence_and_blocks_missing_network_artifacts(
        self,
    ) -> None:
        resolved = self.resolver.resolve(
            SetupPreflight("darwin", "arm64", 64 * GIB, 200 * GIB, False)
        )

        self.assertTrue(resolved.offline)
        runtime = next(step for step in resolved.steps if step.id == "runtime.install")
        model = next(step for step in resolved.steps if step.id == "model.install")
        self.assertEqual(runtime.state, StepState.BLOCKED)
        self.assertIn("offline", runtime.reason)
        self.assertEqual(model.state, StepState.BLOCKED)
        self.assertIn(
            "No completed evidence", self.resolver.preview(resolved).offline_note
        )

    def test_removal_is_reference_aware_and_retains_shared_and_unrelated_state(
        self,
    ) -> None:
        inventory = RemovalInventory(
            running_services=("coding",),
            registered=True,
            application_target_integrations=("codex", "hindsight"),
            product_owned_paths=("~/.config/mastic", "~/.local/state/mastic"),
            product_owned_bytes=2 * GIB,
            shared_cache_paths=("~/.cache/huggingface/hub/models--example",),
            shared_cache_bytes=40 * GIB,
            references={"coding": ("optiq@0.2.18", "example@" + "2" * 40)},
            unrelated_settings=("Codex theme", "Hindsight bank ID"),
        )

        resolved = self.resolver.resolve_removal(inventory)

        self.assertEqual(
            tuple(step.id for step in resolved.steps),
            (
                "service.drain",
                "service.stop",
                "supervisor.unregister",
                "application-target.remove",
                "state.remove",
            ),
        )
        self.assertEqual(resolved.freed_bytes_estimate, 2 * GIB)
        self.assertEqual(resolved.retained_paths, inventory.shared_cache_paths)
        self.assertEqual(resolved.retained_settings, inventory.unrelated_settings)
        self.assertIn("coding", resolved.references)


if __name__ == "__main__":
    unittest.main()
