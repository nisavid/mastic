import unittest

from mastic.domain.resources import (
    ActivationPolicy,
    CachedRevision,
    InferenceService,
    ModelAlias,
    ModelInstallation,
    ModelRevision,
    ResourceName,
    RuntimeFamily,
    RuntimeInstallation,
    ServiceRun,
    ServiceRunState,
)


class ResourceIdentityTests(unittest.TestCase):
    def test_model_intent_cache_and_alias_are_distinct(self) -> None:
        revision = ModelRevision("mlx-community/Qwen", "a" * 40)
        cached = CachedRevision(revision, complete=True, size_bytes=42)
        installation = ModelInstallation("qwen-exact", revision)
        alias = ModelAlias(ResourceName("coding-model"), installation.name)

        self.assertEqual(cached.revision, installation.revision)
        self.assertNotEqual(cached, installation)
        self.assertEqual(alias.installation_name, "qwen-exact")

    def test_service_desired_state_is_separate_from_run(self) -> None:
        service = InferenceService(
            name=ResourceName("coding"),
            model_alias=ResourceName("coding-model"),
            runtime_installation="optiq@0.2.18",
            route=ResourceName("coding"),
            activation=ActivationPolicy.MANUAL,
            pinned=True,
            options={"mtp": True},
        )
        run = ServiceRun(
            run_id="run-1",
            service_name=service.name,
            state=ServiceRunState.READY,
            upstream_port=49152,
        )

        self.assertTrue(service.pinned)
        self.assertEqual(run.service_name, service.name)
        self.assertEqual(run.upstream_port, 49152)

    def test_runtime_installation_has_exact_family_version_and_provenance(self) -> None:
        runtime = RuntimeInstallation(
            installation_id="mlx-lm@0.31.3",
            family=RuntimeFamily.MLX_LM,
            version="0.31.3",
            provenance="mastic-tested",
            capabilities=frozenset({"chat_completions", "max_context"}),
        )

        self.assertEqual(runtime.family, RuntimeFamily.MLX_LM)
        self.assertEqual(runtime.version, "0.31.3")
        self.assertEqual(runtime.provenance, "mastic-tested")
        self.assertIn("max_context", runtime.capabilities)

    def test_resource_name_fields_are_canonicalized_and_validated(self) -> None:
        alias = ModelAlias("coding-model", "qwen-exact")  # type: ignore[arg-type]
        service = InferenceService(
            name="coding",  # type: ignore[arg-type]
            model_alias="coding-model",  # type: ignore[arg-type]
            runtime_installation="mlx-lm@0.31.3",
            route="coding",  # type: ignore[arg-type]
        )
        run = ServiceRun(
            run_id="run-1",
            service_name="coding",  # type: ignore[arg-type]
            state=ServiceRunState.READY,
        )

        self.assertIsInstance(alias.name, ResourceName)
        self.assertIsInstance(service.name, ResourceName)
        self.assertIsInstance(service.model_alias, ResourceName)
        self.assertIsInstance(service.route, ResourceName)
        self.assertIsInstance(run.service_name, ResourceName)

        with self.assertRaisesRegex(ValueError, "resource name"):
            ModelAlias("../escape", "qwen-exact")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "resource name"):
            InferenceService(
                name=ResourceName("coding"),
                model_alias="has space",  # type: ignore[arg-type]
                runtime_installation="mlx-lm@0.31.3",
                route=ResourceName("coding"),
            )
        with self.assertRaisesRegex(ValueError, "resource name"):
            ServiceRun(
                run_id="run-1",
                service_name="/absolute",  # type: ignore[arg-type]
                state=ServiceRunState.READY,
            )

    def test_invalid_resource_names_and_mutable_revisions_are_rejected(self) -> None:
        for invalid in (
            "",
            ".",
            "..",
            "has space",
            "../escape",
            "/absolute",
            "back\\slash",
            "ümlaut",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ResourceName(invalid)
        with self.assertRaisesRegex(ValueError, "immutable commit SHA"):
            ModelRevision("org/model", "main")

    def test_model_revisions_require_exact_sha_lengths(self) -> None:
        for length in (40, 64):
            with self.subTest(length=length):
                self.assertEqual(
                    ModelRevision("org/model", "a" * length).revision,
                    "a" * length,
                )
        for length in (39, 41, 63, 65):
            with (
                self.subTest(length=length),
                self.assertRaisesRegex(ValueError, "immutable commit SHA"),
            ):
                ModelRevision("org/model", "a" * length)

    def test_runtime_installation_ids_reject_non_strings_consistently(self) -> None:
        with self.assertRaisesRegex(ValueError, "installation ID"):
            RuntimeInstallation(
                installation_id=None,  # type: ignore[arg-type]
                family=RuntimeFamily.OPTIQ,
                version="0.3.3",
                provenance="tested",
            )
        with self.assertRaisesRegex(ValueError, "installation ID"):
            InferenceService(
                name=ResourceName("coding"),
                model_alias=ResourceName("coding-model"),
                runtime_installation=None,  # type: ignore[arg-type]
                route=ResourceName("coding"),
            )

        for invalid in (".", "..", "a/b", "a\\b"):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ValueError, "installation ID"),
            ):
                RuntimeInstallation(
                    installation_id=invalid,
                    family=RuntimeFamily.OPTIQ,
                    version="0.3.3",
                    provenance="tested",
                )

    def test_service_run_rejects_non_integer_ports(self) -> None:
        for invalid in (True, 1.5, "49152"):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ValueError, "upstream port"),
            ):
                ServiceRun(
                    run_id="run-1",
                    service_name=ResourceName("coding"),
                    state=ServiceRunState.READY,
                    upstream_port=invalid,  # type: ignore[arg-type]
                )


if __name__ == "__main__":
    unittest.main()
