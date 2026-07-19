import json
import math
import shutil
import stat
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import tomlkit

import mastic.infrastructure.hindsight_application_target as hindsight_target
from mastic.application.config_schema import ApplicationTargetSettings
from mastic.application.dispatch import ApplicationError
from mastic.infrastructure.application_target_integrations import (
    ApplicationTargetConfiguration,
    ApplicationTargetIntegrationConflict,
    ApplicationTargetOwnershipRecoveryRequired,
    CodexModelMetadata,
    CodexTargetOptions,
    HindsightTargetOptions,
    CodexApplicationTargetIntegration,
    HindsightApplicationTargetIntegration,
    LocalApplicationTargetIntegrationFactory,
    SamplingProfile,
)
from mastic.infrastructure.operation_ports import (
    ApplicationTargetOperationPort as ProductionApplicationTargetOperationPort,
)


class ApplicationTargetOperationPort(ProductionApplicationTargetOperationPort):
    """Test composition with explicit in-memory lifecycle collaborators."""

    def __init__(
        self,
        adapter,
        configuration,
        *,
        request,
        settings=lambda _name: None,
        record=lambda _name, _value: None,
        transition=lambda _name: nullcontext(),
    ) -> None:
        class RequestCanary:
            def run(
                self,
                target,
                configuration,
                _settings,
                *,
                profile,
            ):
                return request(
                    target,
                    configuration.gateway_endpoint,
                    configuration.service_name,
                    {"profile": profile},
                )

        super().__init__(
            adapter,
            configuration,
            canary=RequestCanary(),
            settings=settings,
            record=record,
            transition=transition,
        )


class ClientIntegrationV1Tests(unittest.TestCase):
    def test_hindsight_adopts_valid_nonsecret_drift_without_rewriting_profile(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = HindsightApplicationTargetIntegration(
                root / "profile.env", root / "owner.json", root / "backup"
            )
            adapter.apply(self.hindsight_configuration)
            original_manifest = adapter.manifest_path.read_bytes()
            adopted_profiles = dict(self.hindsight_configuration.sampling_profiles)
            adopted_profiles["reflect"] = replace(
                adopted_profiles["reflect"], temperature=0.4
            )
            adopted_configuration = replace(
                self.hindsight_configuration,
                sampling_profiles=adopted_profiles,
                target=HindsightTargetOptions(provider="openai", max_concurrent=2),
            )
            adapter.apply(adopted_configuration)
            adapter.manifest_path.write_bytes(original_manifest)
            external_before = adapter.config_path.read_bytes()

            observed = adapter.observe_drift(self.hindsight_configuration)
            result = adapter.adopt_drift(adopted_configuration)

            self.assertEqual(observed["max_concurrent"], 2)
            self.assertEqual(
                observed["sampling_profiles"]["reflect"]["temperature"], 0.4
            )
            self.assertTrue(result.changed)
            self.assertEqual(adapter.config_path.read_bytes(), external_before)
            self.assertEqual(adapter.inspect()["state"], "healthy")

    def test_hindsight_adoption_blocks_secret_drift_without_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credential = root / "gateway.token"
            token = "a" * 64
            credential.write_text(token, encoding="utf-8")
            credential.chmod(0o600)
            configuration = replace(
                self.hindsight_configuration, credential_path=credential
            )
            adapter = HindsightApplicationTargetIntegration(
                root / "profile.env", root / "owner.json", root / "backup"
            )
            adapter.apply(configuration)
            profile = adapter.config_path.read_text(encoding="utf-8").replace(
                f"HINDSIGHT_API_LLM_API_KEY={token}",
                "HINDSIGHT_API_LLM_API_KEY=external-secret",
            )
            adapter.config_path.write_text(profile, encoding="utf-8")

            with self.assertRaises(ApplicationTargetIntegrationConflict) as blocked:
                adapter.adopt_drift(configuration)

            self.assertNotIn("external-secret", str(blocked.exception))
            self.assertNotIn("external-secret", repr(adapter.inspect()))

    def test_codex_adopts_valid_drift_and_relinquishes_without_external_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = CodexApplicationTargetIntegration(
                root / "config.toml", root / "owner.json", root / "backup"
            )
            adapter.apply(self.codex_configuration)
            original_manifest = adapter.manifest_path.read_bytes()
            adopted_configuration = replace(
                self.codex_configuration,
                service_name="review",
                context_window=16384,
            )
            adapter.apply(adopted_configuration)
            adapter.manifest_path.write_bytes(original_manifest)
            external_before = adapter.config_path.read_bytes()

            observed = adapter.observe_drift(self.codex_configuration)
            adopted = adapter.adopt_drift(adopted_configuration)
            relinquished = adapter.relinquish()

            self.assertEqual(
                observed,
                {"service": "review", "context_window": 16384, "provider": "mlx-local"},
            )
            self.assertTrue(adopted.changed)
            self.assertTrue(relinquished.changed)
            self.assertEqual(adapter.config_path.read_bytes(), external_before)
            self.assertFalse(adapter.manifest_path.exists())
            self.assertFalse(adapter.backup_path.exists())

    def setUp(self) -> None:
        self.codex_configuration = ApplicationTargetConfiguration(
            gateway_endpoint="http://127.0.0.1:8766/v1",
            service_name="coding",
            context_window=32768,
            sampling_profiles={
                "coding": SamplingProfile(
                    temperature=0.6,
                    top_p=0.95,
                    top_k=20,
                    min_p=0.0,
                    presence_penalty=0.0,
                    repetition_penalty=1.0,
                    enable_thinking=True,
                ),
            },
            target=CodexTargetOptions(provider_id="mlx-local"),
        )
        self.hindsight_configuration = ApplicationTargetConfiguration(
            gateway_endpoint="http://127.0.0.1:8766/v1",
            service_name="coding",
            context_window=32768,
            sampling_profiles={
                "verification": SamplingProfile(
                    temperature=0.7,
                    top_p=0.8,
                    top_k=20,
                    min_p=0.0,
                    presence_penalty=1.5,
                    repetition_penalty=1.0,
                    enable_thinking=False,
                ),
                "retain": SamplingProfile(
                    temperature=0.7,
                    top_p=0.8,
                    top_k=20,
                    min_p=0.0,
                    presence_penalty=1.5,
                    repetition_penalty=1.0,
                    enable_thinking=False,
                ),
                "reflect": SamplingProfile(
                    temperature=1.0,
                    top_p=0.95,
                    top_k=20,
                    min_p=0.0,
                    presence_penalty=1.5,
                    repetition_penalty=1.0,
                    enable_thinking=True,
                ),
                "consolidation": SamplingProfile(
                    temperature=0.7,
                    top_p=0.8,
                    top_k=20,
                    min_p=0.0,
                    presence_penalty=1.5,
                    repetition_penalty=1.0,
                    enable_thinking=False,
                ),
            },
            target=HindsightTargetOptions(provider="openai", max_concurrent=1),
        )

    def test_hindsight_provider_is_the_exact_phase_one_provider(self) -> None:
        for provider in (
            "anthropic",
            "openai\nINJECTED=yes",
            "openai\rbad",
            "openai=bad",
        ):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                HindsightTargetOptions(provider=provider)

    def test_application_target_context_is_a_positive_integer(self) -> None:
        for value in (False, True, 0, -1, "32768", 3.5):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "context_window must be positive"),
            ):
                ApplicationTargetConfiguration(
                    gateway_endpoint="http://127.0.0.1:8766/v1",
                    service_name="coding",
                    context_window=value,  # type: ignore[arg-type]
                )

    def test_application_target_endpoint_is_the_public_v1_root(self) -> None:
        for endpoint in (
            "http://127.0.0.1:8766",
            "http://127.0.0.1:8766/",
            "http://127.0.0.1:8766/private/v1",
            "http://127.0.0.1:8766/v1/responses",
        ):
            with (
                self.subTest(endpoint=endpoint),
                self.assertRaisesRegex(ValueError, "public /v1 root"),
            ):
                ApplicationTargetConfiguration(
                    gateway_endpoint=endpoint,
                    service_name="coding",
                )

    def _codex_with_model(
        self, model: CodexModelMetadata, **changes: object
    ) -> ApplicationTargetConfiguration:
        assert isinstance(self.codex_configuration.target, CodexTargetOptions)
        return replace(
            self.codex_configuration,
            target=replace(self.codex_configuration.target, model=model),
            **changes,
        )

    @staticmethod
    def _bundled_codex_catalog() -> dict[str, object]:
        return {
            "models": [
                {
                    "slug": "bundled-coding",
                    "display_name": "Bundled coding",
                    "description": "Bundled model",
                    "base_instructions": "You are Codex, the bundled coding agent.",
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": ["low", "medium", "high"],
                    "supports_reasoning_summaries": True,
                    "supports_parallel_tool_calls": True,
                    "supports_image_detail_original": True,
                    "supports_search_tool": True,
                    "use_responses_lite": True,
                    "input_modalities": ["text", "image"],
                    "context_window": 200_000,
                    "max_context_window": 200_000,
                    "visibility": "list",
                }
            ]
        }

    def test_codex_catalog_is_owned_version_shaped_and_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            manifest = root / "owner.json"
            backup = root / "config.backup"
            catalog = root / "model-catalog.json"
            catalog_backup = root / "model-catalog.backup"
            configuration = self._codex_with_model(
                CodexModelMetadata(
                    slug="qwen36-optiq",
                    display_name="Qwen3.6 35B A3B OptiQ 4-bit",
                    description="Local Qwen3.6 mixture-of-experts coding model.",
                ),
                context_window=131_072,
                service_name="qwen36-optiq",
            )
            adapter = CodexApplicationTargetIntegration(
                config,
                manifest,
                backup,
                catalog_path=catalog,
                catalog_backup_path=catalog_backup,
                bundled_catalog=self._bundled_codex_catalog,
                catalog_validator=lambda _path: None,
            )

            preview = adapter.preview(configuration)
            first = adapter.apply(configuration)
            second = adapter.apply(configuration)

            document = tomlkit.parse(config.read_text(encoding="utf-8"))
            rendered = json.loads(catalog.read_text(encoding="utf-8"))
            model = rendered["models"][0]
            ownership = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertIn(("model_catalog_json",), {item.path for item in preview})
            self.assertTrue(first.changed)
            self.assertFalse(second.changed)
            self.assertEqual(document["model_catalog_json"], str(catalog))
            self.assertEqual(model["slug"], "qwen36-optiq")
            self.assertEqual(model["context_window"], 131_072)
            self.assertEqual(model["max_context_window"], 131_072)
            self.assertEqual(
                model["base_instructions"],
                "You are Codex, the bundled coding agent.",
            )
            self.assertEqual(model["supported_reasoning_levels"], [])
            self.assertIsNone(model["default_reasoning_level"])
            self.assertEqual(model["input_modalities"], ["text"])
            self.assertFalse(model["supports_parallel_tool_calls"])
            self.assertFalse(model["supports_search_tool"])
            self.assertFalse(model["use_responses_lite"])
            self.assertNotIn("apply_patch_tool_type", model)
            self.assertNotIn("web_search_tool_type", model)
            self.assertEqual(model["additional_speed_tiers"], [])
            self.assertEqual(model["service_tiers"], [])
            self.assertEqual(ownership["catalog"]["slug"], "qwen36-optiq")
            self.assertEqual(ownership["catalog"]["context_window"], 131_072)

            removed = adapter.remove()
            self.assertTrue(removed.changed)
            self.assertFalse(catalog.exists())
            self.assertFalse(manifest.exists())

    def test_codex_rollback_point_restores_every_managed_file_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = (
                root / "config.toml",
                root / "owner.json",
                root / "config.backup",
                root / "model-catalog.json",
                root / "model-catalog.backup",
            )
            adapter = CodexApplicationTargetIntegration(
                *paths[:3],
                catalog_path=paths[3],
                catalog_backup_path=paths[4],
                bundled_catalog=self._bundled_codex_catalog,
                catalog_validator=lambda _path: None,
            )
            adapter.apply(self.codex_configuration)
            before = {
                path: path.read_bytes() if path.exists() else None for path in paths
            }
            rollback = adapter.rollback_point()

            adapter.remove()
            rollback()

            self.assertEqual(
                {path: path.read_bytes() if path.exists() else None for path in paths},
                before,
            )

    def test_codex_catalog_inspect_reports_and_repair_fixes_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "catalog.json"
            configuration = self._codex_with_model(
                CodexModelMetadata(
                    slug="coding",
                    display_name="Local coding model",
                    description="Local model",
                ),
                context_window=196_608,
            )
            adapter = CodexApplicationTargetIntegration(
                root / "config.toml",
                root / "owner.json",
                root / "config.backup",
                catalog_path=catalog,
                catalog_backup_path=root / "catalog.backup",
                bundled_catalog=self._bundled_codex_catalog,
                catalog_validator=lambda _path: None,
            )
            adapter.apply(configuration)
            catalog.write_text('{"models": []}\n', encoding="utf-8")

            drifted = adapter.inspect()
            repaired = adapter.apply(configuration)
            healthy = adapter.inspect()

            self.assertEqual(drifted["state"], "drifted")
            self.assertIn(
                "mastic application-target configure codex", drifted["next_actions"]
            )
            self.assertTrue(repaired.changed)
            self.assertEqual(healthy["state"], "healthy")

    def test_legacy_codex_ownership_requires_catalog_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = CodexApplicationTargetIntegration(
                root / "config.toml",
                root / "owner.json",
                root / "config.backup",
                bundled_catalog=self._bundled_codex_catalog,
                catalog_validator=lambda _path: None,
            )
            adapter.apply(self.codex_configuration)

            report = adapter.inspect()

            self.assertEqual(report["state"], "missing")
            self.assertIn(
                "mastic application-target configure codex", report["next_actions"]
            )

    def test_codex_inspect_detects_real_config_catalog_pointer_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = self._codex_with_model(
                CodexModelMetadata("coding", "Coding", "Local model"),
                context_window=131_072,
            )
            adapter = CodexApplicationTargetIntegration(
                root / "config.toml",
                root / "owner.json",
                root / "config.backup",
                catalog_path=root / "catalog.json",
                catalog_backup_path=root / "catalog.backup",
                bundled_catalog=self._bundled_codex_catalog,
                catalog_validator=lambda _path: None,
            )
            adapter.apply(configuration)
            document = tomlkit.parse((root / "config.toml").read_text())
            document["model_catalog_json"] = "/tmp/other-catalog.json"
            (root / "config.toml").write_text(document.as_string())

            report = adapter.inspect()

            self.assertEqual(report["state"], "drifted")
            self.assertIn("model_catalog_json", report["detail"])

    def test_codex_inspect_reports_malformed_or_wrong_path_ownership_bounded(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = CodexApplicationTargetIntegration(
                root / "config.toml", root / "owner.json", root / "backup"
            )
            for payload in (
                "not json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "integration": "codex",
                        "config_path": str(root / "other.toml"),
                        "backup_path": str(adapter.backup_path),
                        "fields": [],
                    }
                ),
            ):
                with self.subTest(payload=payload[:8]):
                    adapter.manifest_path.write_text(payload, encoding="utf-8")

                    report = adapter.inspect()

                    self.assertEqual(report["state"], "malformed")
                    self.assertNotIn(str(root / "other.toml"), repr(report))
                    self.assertEqual(
                        report["next_actions"],
                        [
                            "move invalid or conflicting ownership manifests out of the mastic application-target ownership directory",
                            "mastic application-target inspect codex",
                        ],
                    )

    def test_codex_inspect_reports_malformed_config_toml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = CodexApplicationTargetIntegration(
                root / "config.toml",
                root / "owner.json",
                root / "backup",
                catalog_path=root / "catalog.json",
                catalog_backup_path=root / "catalog.backup",
                bundled_catalog=self._bundled_codex_catalog,
                catalog_validator=lambda _path: None,
            )
            adapter.apply(
                self._codex_with_model(
                    CodexModelMetadata("coding", "Coding", "Local model")
                )
            )
            adapter.config_path.write_text(
                "[broken\nsecret = 'do not expose'", encoding="utf-8"
            )

            report = adapter.inspect()

            self.assertEqual(report["state"], "malformed")
            self.assertEqual(report["detail"], "Codex config TOML is malformed.")
            self.assertNotIn("do not expose", repr(report))

    def test_legacy_codex_migration_restores_preexisting_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "catalog.json"
            original = b'{"models":[{"slug":"user"}]}\n'
            adapter = CodexApplicationTargetIntegration(
                root / "config.toml",
                root / "owner.json",
                root / "config.backup",
                catalog_path=catalog,
                catalog_backup_path=root / "catalog.backup",
                bundled_catalog=self._bundled_codex_catalog,
                catalog_validator=lambda _path: None,
            )
            adapter.apply(self.codex_configuration)
            catalog.write_bytes(original)
            document = tomlkit.parse((root / "config.toml").read_text())
            document["model_catalog_json"] = str(catalog)
            (root / "config.toml").write_text(document.as_string())
            adapter.apply(
                self._codex_with_model(
                    CodexModelMetadata("coding", "Coding", "Local model"),
                    context_window=131_072,
                )
            )

            adapter.remove()

            self.assertEqual(catalog.read_bytes(), original)
            restored = tomlkit.parse((root / "config.toml").read_text())
            self.assertEqual(restored["model_catalog_json"], str(catalog))

    def test_codex_catalog_validation_failure_restores_both_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            catalog = root / "catalog.json"
            config.write_text('unrelated = "keep"\n', encoding="utf-8")
            catalog.write_text('{"models":[{"slug":"user"}]}\n', encoding="utf-8")

            def reject(_path: Path) -> None:
                raise RuntimeError("Codex rejected catalog")

            adapter = CodexApplicationTargetIntegration(
                config,
                root / "owner.json",
                root / "config.backup",
                catalog_path=catalog,
                catalog_backup_path=root / "catalog.backup",
                bundled_catalog=self._bundled_codex_catalog,
                catalog_validator=reject,
            )
            configuration = self._codex_with_model(
                CodexModelMetadata(
                    slug="coding",
                    display_name="Local coding model",
                    description="Local model",
                ),
                context_window=131_072,
            )

            with self.assertRaisesRegex(RuntimeError, "rejected"):
                adapter.apply(configuration)

            self.assertEqual(config.read_text(), 'unrelated = "keep"\n')
            self.assertEqual(catalog.read_text(), '{"models":[{"slug":"user"}]}\n')
            self.assertFalse((root / "owner.json").exists())

    def test_codex_records_pending_ownership_before_replacing_managed_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "owner.json"
            observed: list[tuple[str, str]] = []

            def replace(path: Path, payload: bytes) -> None:
                ownership = json.loads(manifest.read_text(encoding="utf-8"))
                observed.append((path.name, ownership["state"]))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)

            adapter = CodexApplicationTargetIntegration(
                root / "config.toml",
                manifest,
                root / "config.backup",
                replace=replace,
                catalog_path=root / "catalog.json",
                catalog_backup_path=root / "catalog.backup",
                bundled_catalog=self._bundled_codex_catalog,
                catalog_validator=lambda _path: None,
            )

            adapter.apply(
                self._codex_with_model(
                    CodexModelMetadata("coding", "Coding", "Local model")
                )
            )

            self.assertTrue(observed)
            self.assertEqual({state for _path, state in observed}, {"pending"})
            self.assertEqual(
                json.loads(manifest.read_text(encoding="utf-8"))["state"],
                "applied",
            )

    def test_codex_catalog_remove_failure_rolls_back_all_owned_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = (
                root / "config.toml",
                root / "owner.json",
                root / "config.backup",
                root / "catalog.json",
                root / "catalog.backup",
            )
            configuration = self._codex_with_model(
                CodexModelMetadata("coding", "Coding", "Local model"),
                context_window=131_072,
            )
            paths[3].write_text('{"models":[{"slug":"user"}]}\n')
            adapter = CodexApplicationTargetIntegration(
                *paths[:3],
                catalog_path=paths[3],
                catalog_backup_path=paths[4],
                bundled_catalog=self._bundled_codex_catalog,
                catalog_validator=lambda _path: None,
            )
            adapter.apply(configuration)
            before = {path: path.read_bytes() for path in paths if path.exists()}

            def fail_catalog(path: Path, payload: bytes) -> None:
                if path == paths[3]:
                    raise OSError("catalog replace failed")
                path.write_bytes(payload)

            failing = CodexApplicationTargetIntegration(
                *paths[:3],
                catalog_path=paths[3],
                catalog_backup_path=paths[4],
                bundled_catalog=self._bundled_codex_catalog,
                catalog_validator=lambda _path: None,
                replace=fail_catalog,
            )
            with self.assertRaisesRegex(OSError, "catalog replace failed"):
                failing.remove()

            self.assertEqual(
                {path: path.read_bytes() for path in paths if path.exists()}, before
            )

    def test_codex_catalog_restore_failure_rolls_back_all_owned_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = (
                root / "config.toml",
                root / "owner.json",
                root / "config.backup",
                root / "catalog.json",
                root / "catalog.backup",
            )
            paths[0].write_text('unrelated = "keep"\n')
            paths[3].write_text('{"models":[{"slug":"user"}]}\n')
            configuration = self._codex_with_model(
                CodexModelMetadata("coding", "Coding", "Local model"),
                context_window=131_072,
            )
            adapter = CodexApplicationTargetIntegration(
                *paths[:3],
                catalog_path=paths[3],
                catalog_backup_path=paths[4],
                bundled_catalog=self._bundled_codex_catalog,
                catalog_validator=lambda _path: None,
            )
            adapter.apply(configuration)
            before = {path: path.read_bytes() for path in paths if path.exists()}

            def fail_catalog(path: Path, payload: bytes) -> None:
                if path == paths[3]:
                    raise OSError("catalog restore failed")
                path.write_bytes(payload)

            failing = CodexApplicationTargetIntegration(
                *paths[:3],
                catalog_path=paths[3],
                catalog_backup_path=paths[4],
                bundled_catalog=self._bundled_codex_catalog,
                catalog_validator=lambda _path: None,
                replace=fail_catalog,
            )
            with self.assertRaisesRegex(OSError, "catalog restore failed"):
                failing.restore()

            self.assertEqual(
                {path: path.read_bytes() for path in paths if path.exists()}, before
            )

    @unittest.skipUnless(shutil.which("codex"), "Codex is not installed")
    def test_installed_codex_resolves_catalog_without_fallback_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = CodexApplicationTargetIntegration(
                root / "config.toml",
                root / "owner.json",
                root / "config.backup",
                catalog_path=root / "catalog.json",
                catalog_backup_path=root / "catalog.backup",
            )
            result = adapter.apply(
                self._codex_with_model(
                    CodexModelMetadata(
                        slug="qwen36-optiq",
                        display_name="Qwen3.6 35B A3B OptiQ 4-bit",
                        description="Local coding model",
                    ),
                    context_window=131_072,
                )
            )

            self.assertTrue(result.changed)
            self.assertEqual(adapter.inspect()["state"], "healthy")

    def test_codex_preview_apply_and_remove_preserve_unrelated_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "codex" / "config.toml"
            manifest = root / "mastic" / "codex-ownership.json"
            backup = root / "mastic" / "codex-config.backup"
            config.parent.mkdir()
            config.write_text(
                '# keep this comment\nmodel = "cloud"\nmodel_provider = "existing"\n'
                '[model_providers.existing]\nname = "Existing"\nbase_url = "https://example.invalid/v1"\n'
                '[tui]\ntheme = "catppuccin-mocha"\n',
                encoding="utf-8",
            )
            adapter = CodexApplicationTargetIntegration(config, manifest, backup)

            preview = adapter.preview(self.codex_configuration)
            applied = adapter.apply(self.codex_configuration)
            second = adapter.apply(self.codex_configuration)

            document = tomlkit.parse(config.read_text(encoding="utf-8"))
            self.assertIn(("model",), {change.path for change in preview})
            self.assertTrue(applied.changed)
            self.assertFalse(second.changed)
            self.assertEqual(document["model"], "coding")
            self.assertEqual(document["oss_provider"], "mlx-local")
            self.assertEqual(
                document["model_providers"]["mlx-local"]["base_url"],
                "http://127.0.0.1:8766/application-targets/codex/profiles/coding/v1",
            )
            self.assertNotIn("profiles", document)
            self.assertEqual(document["tui"]["theme"], "catppuccin-mocha")
            self.assertEqual(
                document["model_providers"]["existing"]["name"], "Existing"
            )
            self.assertIn("# keep this comment", config.read_text(encoding="utf-8"))
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
            owned = json.loads(manifest.read_text(encoding="utf-8"))["fields"]
            self.assertNotIn("tui.theme", {".".join(field["path"]) for field in owned})

            removed = adapter.remove()
            restored = tomlkit.parse(config.read_text(encoding="utf-8"))

            self.assertTrue(removed.changed)
            self.assertEqual(restored["model"], "cloud")
            self.assertEqual(restored["model_provider"], "existing")
            self.assertNotIn("oss_provider", restored)
            self.assertNotIn("mlx-local", restored["model_providers"])
            self.assertEqual(restored["tui"]["theme"], "catppuccin-mocha")

    def test_codex_apply_migrates_the_owned_legacy_provider_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "codex" / "config.toml"
            adapter = CodexApplicationTargetIntegration(
                config,
                root / "mastic" / "codex-ownership.json",
                root / "mastic" / "codex-config.backup",
            )
            legacy = replace(
                self.codex_configuration,
                target=CodexTargetOptions(provider_id="mastic-local"),
            )

            adapter.apply(legacy)
            adapter.apply(self.codex_configuration)

            document = tomlkit.parse(config.read_text(encoding="utf-8"))
            self.assertEqual(document["model_provider"], "mlx-local")
            self.assertEqual(document["oss_provider"], "mlx-local")
            self.assertIn("mlx-local", document["model_providers"])
            self.assertNotIn("mastic-local", document["model_providers"])

    def test_managed_application_targets_fail_closed_on_missing_or_unrepresentable_profiles(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex = CodexApplicationTargetIntegration(
                root / "codex.toml", root / "codex-owner.json", root / "codex-backup"
            )
            missing_coding = replace(
                self.codex_configuration,
                sampling_profiles={
                    "codng": SamplingProfile(temperature=0.6, top_p=0.95)
                },
            )
            with self.assertRaisesRegex(ValueError, "requires sampling profiles"):
                codex.preview(missing_coding)

            unsupported = replace(
                self.codex_configuration,
                sampling_profiles={
                    "coding": SamplingProfile(
                        temperature=0.6,
                        top_p=0.95,
                        presence_penalty=1.5,
                    )
                },
            )
            with self.assertRaisesRegex(ValueError, "Responses"):
                codex.preview(unsupported)

            unexpected = replace(
                self.codex_configuration,
                sampling_profiles={
                    **self.codex_configuration.sampling_profiles,
                    "surprise": SamplingProfile(temperature=0.5),
                },
            )
            with self.assertRaisesRegex(ValueError, "requires sampling profiles"):
                codex.preview(unexpected)

            hindsight = HindsightApplicationTargetIntegration(
                root / "hindsight.env",
                root / "hindsight-owner.json",
                root / "hindsight-backup",
            )
            with self.assertRaisesRegex(ValueError, "requires sampling profiles"):
                hindsight.preview(
                    replace(
                        self.hindsight_configuration,
                        sampling_profiles={"retain": SamplingProfile(temperature=0.7)},
                    )
                )

    def test_sampling_profiles_reject_non_finite_values_and_preserve_provenance(
        self,
    ) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "finite"),
            ):
                SamplingProfile(temperature=value)

        profile = SamplingProfile(
            temperature=0.6,
            upstream_profile="precise-coding-thinking",
            source_url="https://example.test/model-card",
            source_revision="9" * 40,
        )
        self.assertEqual(profile.values(), {"temperature": 0.6})
        self.assertEqual(
            profile.definition()["upstream_profile"], "precise-coding-thinking"
        )

    def test_gateway_credential_is_exactly_configured_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credential = root / "gateway.token"
            token = "private-token-value-that-must-never-leak"
            credential.write_text(token + "\n", encoding="ascii")
            credential.chmod(0o600)
            codex_configuration = replace(
                self.codex_configuration, credential_path=credential
            )
            hindsight_configuration = replace(
                self.hindsight_configuration, credential_path=credential
            )

            codex_path = root / "codex.toml"
            codex = CodexApplicationTargetIntegration(
                codex_path, root / "codex-owner.json", root / "codex-backup"
            )
            codex.apply(codex_configuration)
            document = tomlkit.parse(codex_path.read_text(encoding="utf-8"))
            auth = document["model_providers"]["mlx-local"]["auth"]
            self.assertEqual(auth["command"], "/bin/cat")
            self.assertEqual(auth["args"], [str(credential)])
            self.assertEqual(auth["refresh_interval_ms"], 0)
            self.assertNotIn(token, codex_path.read_text(encoding="utf-8"))

            hindsight_path = root / "hindsight.env"
            hindsight_path.write_text(
                "HINDSIGHT_API_LLM_API_KEY=old-private-token\n",
                encoding="utf-8",
            )
            hindsight = HindsightApplicationTargetIntegration(
                hindsight_path,
                root / "hindsight-owner.json",
                root / "hindsight-backup",
            )
            preview = hindsight.preview(hindsight_configuration)
            applied = hindsight.apply(hindsight_configuration)

            rendered = hindsight_path.read_text(encoding="utf-8")
            manifest = hindsight.manifest_path.read_text(encoding="utf-8")
            self.assertIn(f"HINDSIGHT_API_LLM_API_KEY={token}", rendered)
            self.assertEqual(stat.S_IMODE(hindsight_path.stat().st_mode), 0o600)
            self.assertNotIn(token, repr(preview))
            self.assertNotIn(token, repr(applied))
            self.assertNotIn(token, manifest)
            self.assertNotIn("old-private-token", manifest)
            self.assertTrue(
                all(
                    change.after == "<redacted>"
                    for change in applied.changes
                    if change.path == ("HINDSIGHT_API_LLM_API_KEY",)
                )
            )

            hindsight.remove()
            self.assertIn(
                "HINDSIGHT_API_LLM_API_KEY=old-private-token",
                hindsight_path.read_text(encoding="utf-8"),
            )

    def test_codex_precise_removal_does_not_clobber_a_later_user_edit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            adapter = CodexApplicationTargetIntegration(
                config, root / "owner.json", root / "backup"
            )
            adapter.apply(self.codex_configuration)
            document = tomlkit.parse(config.read_text(encoding="utf-8"))
            document["model"] = "my-new-choice"
            config.write_text(document.as_string(), encoding="utf-8")

            result = adapter.remove()

            current = tomlkit.parse(config.read_text(encoding="utf-8"))
            self.assertEqual(current["model"], "my-new-choice")
            self.assertIn(("model",), result.skipped_paths)

    def test_codex_takeover_records_already_equal_fields_for_precise_removal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            adapter = CodexApplicationTargetIntegration(
                config, root / "owner.json", root / "backup"
            )
            adapter.apply(self.codex_configuration)
            adapter.manifest_path.unlink()
            adapter.backup_path.unlink()

            adopted = adapter.apply(self.codex_configuration, takeover=True)
            manifest = json.loads(adapter.manifest_path.read_text(encoding="utf-8"))

            self.assertTrue(adopted.changed)
            self.assertFalse(adopted.changes)
            self.assertTrue(manifest["fields"])
            self.assertTrue(
                all(not item["before_present"] for item in manifest["fields"])
            )
            removed = adapter.remove()
            self.assertTrue(removed.changed)
            document = tomlkit.parse(config.read_text(encoding="utf-8"))
            self.assertNotIn("mlx-local", document.get("model_providers", {}))

    def test_codex_reconfiguration_keeps_the_original_restore_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            original = '# original\nmodel = "cloud"\n'
            config.write_text(original, encoding="utf-8")
            adapter = CodexApplicationTargetIntegration(
                config, root / "owner.json", root / "backup"
            )
            adapter.apply(self.codex_configuration)
            changed = ApplicationTargetConfiguration(
                gateway_endpoint=self.codex_configuration.gateway_endpoint,
                service_name="general",
                context_window=16384,
                sampling_profiles={
                    "coding": SamplingProfile(temperature=0.2, top_p=0.9)
                },
            )

            adapter.apply(changed)
            adapter.restore()

            self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_codex_restore_is_exact_and_refuses_to_overwrite_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text('model = "before"\n', encoding="utf-8")
            adapter = CodexApplicationTargetIntegration(
                config, root / "owner.json", root / "backup"
            )
            adapter.apply(self.codex_configuration)
            configured = config.read_text(encoding="utf-8")

            adapter.restore()
            self.assertEqual(config.read_text(encoding="utf-8"), 'model = "before"\n')

            adapter.apply(self.codex_configuration)
            config.write_text(configured + "# user edit\n", encoding="utf-8")
            with self.assertRaises(ApplicationTargetIntegrationConflict):
                adapter.restore()

    def test_invalid_codex_input_and_replace_failure_leave_current_config_intact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text("not valid = [\n", encoding="utf-8")
            adapter = CodexApplicationTargetIntegration(
                config, root / "owner.json", root / "backup"
            )
            before = config.read_bytes()

            with self.assertRaises(Exception):
                adapter.apply(self.codex_configuration)
            self.assertEqual(config.read_bytes(), before)

            config.write_text('model = "before"\n', encoding="utf-8")

            def fail_replace(path: Path, payload: bytes) -> None:
                raise OSError("simulated replace failure")

            failing = CodexApplicationTargetIntegration(
                config, root / "owner-2.json", root / "backup-2", replace=fail_replace
            )
            with self.assertRaisesRegex(OSError, "simulated"):
                failing.apply(self.codex_configuration)
            self.assertEqual(config.read_text(encoding="utf-8"), 'model = "before"\n')
            self.assertFalse((root / "owner-2.json").exists())
            self.assertFalse((root / "backup-2").exists())

    def test_hindsight_round_trips_comments_profiles_test_and_precise_removal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "profile.env"
            config.write_text(
                "# memory profile\nHINDSIGHT_BANK_ID=existing-bank\nHINDSIGHT_API_LLM_MODEL=cloud\n",
                encoding="utf-8",
            )
            adapter = HindsightApplicationTargetIntegration(
                config, root / "owner.json", root / "backup"
            )

            changes = adapter.preview(self.hindsight_configuration)
            applied = adapter.apply(self.hindsight_configuration)
            calls: list[tuple[str, str, dict[str, object]]] = []
            response = adapter.test(
                self.hindsight_configuration,
                lambda endpoint, model, sampling: (
                    calls.append((endpoint, model, dict(sampling))) or {"text": "ready"}
                ),
                profile="reflect",
            )

            text = config.read_text(encoding="utf-8")
            self.assertTrue(changes)
            self.assertTrue(applied.changed)
            self.assertIn("# memory profile", text)
            self.assertIn("HINDSIGHT_BANK_ID=existing-bank", text)
            self.assertIn("HINDSIGHT_API_LLM_MODEL=coding", text)
            self.assertIn(
                "HINDSIGHT_API_LLM_BASE_URL=http://127.0.0.1:8766/application-targets/hindsight/profiles/verification/v1",
                text,
            )
            self.assertIn("HINDSIGHT_API_LLM_TEMPERATURE_REFLECT=1.0", text)
            self.assertIn(
                "HINDSIGHT_API_RETAIN_LLM_BASE_URL=http://127.0.0.1:8766/application-targets/hindsight/profiles/retain/v1",
                text,
            )
            self.assertIn(
                "HINDSIGHT_API_REFLECT_LLM_BASE_URL=http://127.0.0.1:8766/application-targets/hindsight/profiles/reflect/v1",
                text,
            )
            self.assertIn(
                "HINDSIGHT_API_CONSOLIDATION_LLM_BASE_URL=http://127.0.0.1:8766/application-targets/hindsight/profiles/consolidation/v1",
                text,
            )
            self.assertEqual(
                calls,
                [
                    (
                        "http://127.0.0.1:8766/application-targets/hindsight/profiles/reflect/v1",
                        "coding",
                        {},
                    )
                ],
            )
            self.assertEqual(response, {"text": "ready"})

            adapter.remove()
            restored = config.read_text(encoding="utf-8")
            self.assertIn("HINDSIGHT_BANK_ID=existing-bank", restored)
            self.assertIn("HINDSIGHT_API_LLM_MODEL=cloud", restored)
            self.assertNotIn("HINDSIGHT_API_LLM_BASE_URL", restored)

    def test_hindsight_inspect_reports_healthy_owned_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = HindsightApplicationTargetIntegration(
                root / "profile.env", root / "owner.json", root / "backup"
            )
            adapter.apply(self.hindsight_configuration)

            report = adapter.inspect()

            self.assertEqual(report["state"], "healthy")
            self.assertIn("matches mastic ownership", report["detail"])
            self.assertEqual(report["config_path"], str(adapter.config_path))
            self.assertEqual(report["next_actions"], [])

    def test_hindsight_inspect_reports_changed_owned_field_as_drifted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = HindsightApplicationTargetIntegration(
                root / "profile.env", root / "owner.json", root / "backup"
            )
            adapter.apply(self.hindsight_configuration)
            adapter.config_path.write_text(
                adapter.config_path.read_text(encoding="utf-8").replace(
                    "HINDSIGHT_API_LLM_MODEL=coding",
                    "HINDSIGHT_API_LLM_MODEL=user-choice",
                ),
                encoding="utf-8",
            )

            report = adapter.inspect()

            self.assertEqual(report["state"], "drifted")
            self.assertIn("HINDSIGHT_API_LLM_MODEL", report["detail"])
            self.assertEqual(
                report["next_actions"],
                ["mastic application-target configure hindsight --help"],
            )

    def test_hindsight_inspect_reports_profile_without_manifest_as_unmanaged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = HindsightApplicationTargetIntegration(
                root / "profile.env", root / "owner.json", root / "backup"
            )
            adapter.config_path.write_text(
                "HINDSIGHT_API_LLM_MODEL=user-choice\n", encoding="utf-8"
            )

            report = adapter.inspect()

            self.assertEqual(report["state"], "unmanaged")
            self.assertNotIn("user-choice", repr(report))
            self.assertTrue(report["next_actions"])

    def test_hindsight_inspect_reports_missing_owned_support(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = HindsightApplicationTargetIntegration(
                root / "profile.env", root / "owner.json", root / "backup"
            )
            adapter.apply(self.hindsight_configuration)
            adapter.backup_path.unlink()

            report = adapter.inspect()

            self.assertEqual(report["state"], "missing")
            self.assertIn("backup", report["detail"].lower())
            self.assertTrue(report["next_actions"])

    def test_hindsight_inspect_reports_changed_ownership_backup_as_drifted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = HindsightApplicationTargetIntegration(
                root / "profile.env", root / "owner.json", root / "backup"
            )
            adapter.apply(self.hindsight_configuration)
            adapter.backup_path.write_text("changed support data\n", encoding="utf-8")

            report = adapter.inspect()

            self.assertEqual(report["state"], "drifted")
            self.assertIn("backup", report["detail"].lower())
            self.assertTrue(report["next_actions"])

    def test_hindsight_inspect_reports_invalid_support_path_as_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = HindsightApplicationTargetIntegration(
                root / "profile.env", root / "owner.json", root / "backup"
            )
            adapter.apply(self.hindsight_configuration)
            adapter.backup_path.unlink()
            adapter.backup_path.mkdir()

            report = adapter.inspect()

            self.assertEqual(report["state"], "malformed")
            self.assertIn("support", report["detail"].lower())
            self.assertTrue(report["next_actions"])

    def test_hindsight_inspect_reports_missing_owned_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = HindsightApplicationTargetIntegration(
                root / "profile.env", root / "owner.json", root / "backup"
            )
            adapter.apply(self.hindsight_configuration)
            adapter.config_path.unlink()

            report = adapter.inspect()

            self.assertEqual(report["state"], "missing")
            self.assertIn("profile", report["detail"].lower())
            self.assertTrue(report["next_actions"])

    def test_hindsight_inspect_reports_malformed_ownership_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = HindsightApplicationTargetIntegration(
                root / "profile.env", root / "owner.json", root / "backup"
            )
            adapter.config_path.write_text(
                "HINDSIGHT_API_LLM_API_KEY=must-not-leak\n", encoding="utf-8"
            )
            adapter.manifest_path.write_text("not json", encoding="utf-8")

            report = adapter.inspect()

            self.assertEqual(report["state"], "malformed")
            self.assertNotIn("must-not-leak", repr(report))
            self.assertTrue(report["next_actions"])

    def test_hindsight_inspect_reports_malformed_owned_profile_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = HindsightApplicationTargetIntegration(
                root / "profile.env", root / "owner.json", root / "backup"
            )
            adapter.apply(self.hindsight_configuration)
            adapter.config_path.write_text(
                "HINDSIGHT_API_LLM_MODEL=must-not-leak\n"
                "HINDSIGHT_API_LLM_MODEL=duplicate\n",
                encoding="utf-8",
            )

            report = adapter.inspect()

            self.assertEqual(report["state"], "malformed")
            self.assertNotIn("must-not-leak", repr(report))
            self.assertNotIn("duplicate", repr(report))
            self.assertTrue(report["next_actions"])

    def test_hindsight_inspect_detects_rotated_secret_without_exposing_values(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credential = root / "gateway.token"
            original = "original-secret-value-must-never-leak"
            rotated = "rotated-secret-value-must-never-leak"
            credential.write_text(original + "\n", encoding="ascii")
            credential.chmod(0o600)
            adapter = HindsightApplicationTargetIntegration(
                root / "profile.env", root / "owner.json", root / "backup"
            )
            adapter.apply(
                replace(self.hindsight_configuration, credential_path=credential)
            )
            adapter.config_path.write_text(
                adapter.config_path.read_text(encoding="utf-8").replace(
                    original, rotated
                ),
                encoding="utf-8",
            )

            report = adapter.inspect()

            self.assertEqual(report["state"], "drifted")
            self.assertIn("credential", report["detail"].lower())
            self.assertNotIn(original, repr(report))
            self.assertNotIn(rotated, repr(report))
            self.assertTrue(report["next_actions"])

    def test_hindsight_apply_restores_all_files_after_a_post_replace_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "profile.env"
            manifest = root / "owner.json"
            backup = root / "backup"
            original = b"HINDSIGHT_API_LLM_MODEL=cloud\n"
            config.write_bytes(original)

            def replace_then_fail(path: Path, payload: bytes) -> None:
                path.write_bytes(payload)
                raise OSError("post-replace failure")

            adapter = HindsightApplicationTargetIntegration(
                config,
                manifest,
                backup,
                replace=replace_then_fail,
            )

            with self.assertRaisesRegex(OSError, "post-replace failure"):
                adapter.apply(self.hindsight_configuration)

            self.assertEqual(config.read_bytes(), original)
            self.assertFalse(manifest.exists())
            self.assertFalse(backup.exists())

    def test_hindsight_rollback_point_restores_every_managed_file_exactly(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = (root / "profile.env", root / "owner.json", root / "backup")
            paths[0].write_text("HINDSIGHT_API_LLM_MODEL=cloud\n", encoding="utf-8")
            adapter = HindsightApplicationTargetIntegration(*paths)
            adapter.apply(self.hindsight_configuration)
            before = {path: path.read_bytes() for path in paths}
            rollback = adapter.rollback_point()

            adapter.remove()
            rollback()

            self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_hindsight_remove_restores_all_files_after_a_post_replace_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = (root / "profile.env", root / "owner.json", root / "backup")
            paths[0].write_text("HINDSIGHT_API_LLM_MODEL=cloud\n", encoding="utf-8")
            HindsightApplicationTargetIntegration(*paths).apply(
                self.hindsight_configuration
            )
            before = {path: path.read_bytes() for path in paths}

            def replace_then_fail(path: Path, payload: bytes) -> None:
                path.write_bytes(payload)
                raise OSError("post-replace failure")

            failing = HindsightApplicationTargetIntegration(
                *paths,
                replace=replace_then_fail,
            )

            with self.assertRaisesRegex(OSError, "post-replace failure"):
                failing.remove()

            self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_hindsight_restore_restores_all_files_after_a_post_replace_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = (root / "profile.env", root / "owner.json", root / "backup")
            paths[0].write_text("HINDSIGHT_API_LLM_MODEL=cloud\n", encoding="utf-8")
            HindsightApplicationTargetIntegration(*paths).apply(
                self.hindsight_configuration
            )
            before = {path: path.read_bytes() for path in paths}

            def replace_then_fail(path: Path, payload: bytes) -> None:
                path.write_bytes(payload)
                raise OSError("post-replace failure")

            failing = HindsightApplicationTargetIntegration(
                *paths,
                replace=replace_then_fail,
            )

            with self.assertRaisesRegex(OSError, "post-replace failure"):
                failing.restore()

            self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_hindsight_remove_restores_all_files_after_manifest_replace_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = (root / "profile.env", root / "owner.json", root / "backup")
            paths[0].write_text("HINDSIGHT_API_LLM_MODEL=cloud\n", encoding="utf-8")
            adapter = HindsightApplicationTargetIntegration(*paths)
            adapter.apply(self.hindsight_configuration)
            paths[0].write_text(
                paths[0]
                .read_text(encoding="utf-8")
                .replace(
                    "HINDSIGHT_API_LLM_MODEL=coding",
                    "HINDSIGHT_API_LLM_MODEL=user-choice",
                ),
                encoding="utf-8",
            )
            before = {path: path.read_bytes() for path in paths}
            write_private = hindsight_target._write_private

            def write_then_fail(path: Path, payload: bytes) -> None:
                write_private(path, payload)
                if path == paths[1]:
                    raise OSError("manifest replace failure")

            with (
                patch.object(
                    hindsight_target,
                    "_write_private",
                    side_effect=write_then_fail,
                ),
                self.assertRaisesRegex(OSError, "manifest replace failure"),
            ):
                adapter.remove()

            self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_hindsight_restore_restores_all_files_after_manifest_unlink_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = (root / "profile.env", root / "owner.json", root / "backup")
            paths[0].write_text("HINDSIGHT_API_LLM_MODEL=cloud\n", encoding="utf-8")
            adapter = HindsightApplicationTargetIntegration(*paths)
            adapter.apply(self.hindsight_configuration)
            before = {path: path.read_bytes() for path in paths}
            unlink = Path.unlink

            def unlink_then_fail(path: Path, missing_ok: bool = False) -> None:
                unlink(path, missing_ok=missing_ok)
                if path == paths[1]:
                    raise OSError("manifest unlink failure")

            with (
                patch.object(Path, "unlink", unlink_then_fail),
                self.assertRaisesRegex(OSError, "manifest unlink failure"),
            ):
                adapter.restore()

            self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_application_target_test_route_is_independent_of_profile_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex = CodexApplicationTargetIntegration(
                root / "codex.toml", root / "codex-owner.json", root / "codex-backup"
            )
            hindsight = HindsightApplicationTargetIntegration(
                root / "hindsight.env",
                root / "hindsight-owner.json",
                root / "hindsight-backup",
            )
            endpoints: list[str] = []

            def request(endpoint: str, model: str, sampling: dict[str, object]) -> None:
                endpoints.append(endpoint)

            codex.test(self.codex_configuration, request, profile="coding")
            hindsight.test(self.hindsight_configuration, request, profile="reflect")

            self.assertEqual(
                endpoints,
                [
                    "http://127.0.0.1:8766/application-targets/codex/profiles/coding/v1",
                    "http://127.0.0.1:8766/application-targets/hindsight/profiles/reflect/v1",
                ],
            )

    def test_hindsight_takeover_records_already_equal_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "profile.env"
            adapter = HindsightApplicationTargetIntegration(
                config, root / "owner.json", root / "backup"
            )
            adapter.apply(self.hindsight_configuration)
            adapter.manifest_path.unlink()
            adapter.backup_path.unlink()

            adopted = adapter.apply(self.hindsight_configuration, takeover=True)

            self.assertTrue(adopted.changed)
            self.assertFalse(adopted.changes)
            owned = json.loads(adapter.manifest_path.read_text(encoding="utf-8"))[
                "fields"
            ]
            self.assertTrue(owned)
            self.assertTrue(all(not item["before_present"] for item in owned))

    def test_application_target_endpoint_requires_a_literal_loopback_origin(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "literal HTTP loopback"):
            ApplicationTargetConfiguration(
                gateway_endpoint="http://localhost:8766/v1",
                service_name="coding",
            )

    def test_local_factory_selects_explicit_hindsight_profile_and_owned_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = root / "hindsight" / "profiles"
            ownership = root / "mastic" / "application-targets"
            factory = LocalApplicationTargetIntegrationFactory(
                codex_config_path=root / "codex" / "config.toml",
                hindsight_profiles_dir=profiles,
                ownership_dir=ownership,
            )

            adapter = factory(
                "application-target.configure",
                "hindsight",
                {"profile": "agent-memory"},
                None,
            )

            self.assertEqual(adapter.config_path, profiles / "agent-memory.env")
            self.assertEqual(
                adapter.manifest_path,
                ownership / "hindsight-agent-memory.ownership.json",
            )

            stored = ApplicationTargetSettings(
                name="hindsight",
                kind="hindsight",
                service="memory",
                profile="agent-memory",
                context_window=32768,
                provider="openai",
                max_concurrent=1,
                sampling={},
            )
            test_adapter = factory(
                "application-target.test",
                "hindsight",
                {"profile": "reflect"},
                stored,
            )
            self.assertEqual(test_adapter.config_path, profiles / "agent-memory.env")

    def test_local_factory_recovers_one_manifest_backed_hindsight_profile(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = root / "profiles"
            ownership = root / "ownership"
            factory = LocalApplicationTargetIntegrationFactory(
                codex_config_path=root / "config.toml",
                hindsight_profiles_dir=profiles,
                ownership_dir=ownership,
            )
            configured = factory(
                "application-target.configure",
                "hindsight",
                {"profile": "agent-memory"},
                None,
            )
            configured.apply(self.hindsight_configuration)

            recovered = factory("application-target.inspect", "hindsight", {}, None)
            recorded = []
            port = ApplicationTargetOperationPort(
                factory,
                lambda name, parameters, settings: self.hindsight_configuration,
                request=lambda target, endpoint, model, sampling: {},
                settings=lambda name: None,
                record=lambda name, value: recorded.append((name, value)),
            )
            report = port.execute(
                "application-target.inspect", {"application_target": "hindsight"}
            )
            result = port.execute(
                "application-target.remove", {"application_target": "hindsight"}
            )

            self.assertEqual(recovered.config_path, profiles / "agent-memory.env")
            self.assertEqual(report["state"], "healthy")
            self.assertTrue(result["changed"])
            self.assertFalse(configured.manifest_path.exists())
            self.assertEqual(recorded, [])

    def test_local_factory_rejects_desired_hindsight_profile_mismatching_ownership(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            factory = LocalApplicationTargetIntegrationFactory(
                codex_config_path=root / "config.toml",
                hindsight_profiles_dir=root / "profiles",
                ownership_dir=root / "ownership",
            )
            factory(
                "application-target.configure",
                "hindsight",
                {"profile": "owned-profile"},
                None,
            ).apply(self.hindsight_configuration)
            desired = ApplicationTargetSettings(
                name="hindsight",
                kind="hindsight",
                service="memory",
                profile="desired-profile",
                context_window=32768,
                provider="openai",
                max_concurrent=1,
                sampling={},
            )

            with self.assertRaisesRegex(
                ApplicationTargetOwnershipRecoveryRequired, "does not match"
            ):
                factory("application-target.inspect", "hindsight", {}, desired)

    def test_local_factory_inspects_one_malformed_orphan_hindsight_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = root / "profiles"
            ownership = root / "ownership"
            ownership.mkdir()
            (ownership / "hindsight-agent-memory.ownership.json").write_text(
                "not json", encoding="utf-8"
            )
            factory = LocalApplicationTargetIntegrationFactory(
                codex_config_path=root / "config.toml",
                hindsight_profiles_dir=profiles,
                ownership_dir=ownership,
            )

            adapter = factory("application-target.inspect", "hindsight", {}, None)
            report = adapter.inspect()

            self.assertEqual(adapter.config_path, profiles / "agent-memory.env")
            self.assertEqual(report["state"], "malformed")
            self.assertNotIn("not json", repr(report))

    def test_malformed_inspection_recovery_reaches_unmanaged_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = root / "profiles"
            ownership = root / "ownership"
            ownership.mkdir()
            manifest = ownership / "hindsight-agent-memory.ownership.json"
            secret = "secret-payload-at-/tmp/untrusted-profile.env"
            manifest.write_text(secret, encoding="utf-8")
            factory = LocalApplicationTargetIntegrationFactory(
                codex_config_path=root / "config.toml",
                hindsight_profiles_dir=profiles,
                ownership_dir=ownership,
            )
            stored = ApplicationTargetSettings(
                name="hindsight",
                kind="hindsight",
                service="memory",
                profile="agent-memory",
                context_window=32768,
                provider="openai",
                max_concurrent=1,
                sampling={},
            )
            port = ApplicationTargetOperationPort(
                factory,
                lambda name, parameters, settings: self.hindsight_configuration,
                request=lambda target, endpoint, model, sampling: {},
                settings=lambda name: stored,
            )

            malformed = port.execute(
                "application-target.inspect", {"application_target": "hindsight"}
            )

            self.assertEqual(malformed["state"], "malformed")
            self.assertEqual(
                malformed["next_actions"],
                [
                    "move invalid or conflicting ownership manifests out of the mastic application-target ownership directory",
                    "mastic application-target inspect hindsight",
                ],
            )
            self.assertNotIn(secret, repr(malformed))
            self.assertNotIn("configure", repr(malformed["next_actions"]))

            with self.assertRaises(ApplicationError) as blocked:
                port.execute(
                    "application-target.configure",
                    {
                        "application_target": "hindsight",
                        "profile": "agent-memory",
                    },
                )
            self.assertEqual(
                blocked.exception.code, "application_target_recovery_required"
            )
            self.assertNotIn(secret, repr(blocked.exception))

            self.assertEqual(
                malformed["ownership_manifest_path"],
                str(manifest),
            )
            Path(str(malformed["ownership_manifest_path"])).rename(
                root / "quarantined-ownership-manifest"
            )
            recovered = port.execute(
                "application-target.inspect", {"application_target": "hindsight"}
            )

            self.assertEqual(recovered["state"], "unmanaged")

    def test_codex_mutations_fail_with_typed_recovery_for_malformed_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ownership = root / "ownership"
            ownership.mkdir()
            manifest = ownership / "codex.ownership.json"
            secret = "secret-payload-at-/tmp/untrusted-config.toml"
            manifest.write_text(secret, encoding="utf-8")
            factory = LocalApplicationTargetIntegrationFactory(
                codex_config_path=root / "config.toml",
                hindsight_profiles_dir=root / "profiles",
                ownership_dir=ownership,
            )
            stored = ApplicationTargetSettings(
                name="codex",
                kind="codex",
                service="coding",
                profile=None,
                context_window=32768,
                provider="mastic",
                max_concurrent=1,
                sampling={},
            )
            port = ApplicationTargetOperationPort(
                factory,
                lambda name, parameters, settings: self.codex_configuration,
                request=lambda target, endpoint, model, sampling: {},
                settings=lambda name: stored,
            )

            report = port.execute(
                "application-target.inspect", {"application_target": "codex"}
            )
            self.assertEqual(report["state"], "malformed")
            self.assertEqual(report["ownership_manifest_path"], str(manifest))

            for operation in (
                "application-target.configure",
                "application-target.remove",
            ):
                with self.subTest(operation=operation):
                    with self.assertRaises(ApplicationError) as blocked:
                        port.execute(operation, {"application_target": "codex"})
                    self.assertEqual(
                        blocked.exception.code,
                        "application_target_recovery_required",
                    )
                    self.assertNotIn(secret, repr(blocked.exception))

    def test_local_factory_refuses_to_remove_malformed_orphan_hindsight_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ownership = root / "ownership"
            ownership.mkdir()
            (ownership / "hindsight-agent-memory.ownership.json").write_text(
                "not json", encoding="utf-8"
            )
            factory = LocalApplicationTargetIntegrationFactory(
                codex_config_path=root / "config.toml",
                hindsight_profiles_dir=root / "profiles",
                ownership_dir=ownership,
            )

            with self.assertRaisesRegex(
                ApplicationTargetOwnershipRecoveryRequired, "invalid hindsight"
            ):
                factory("application-target.remove", "hindsight", {}, None)

    def test_hindsight_factory_discovery_ignores_codex_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ownership = root / "ownership"
            ownership.mkdir()
            (ownership / "codex.ownership.json").write_text(
                "not json", encoding="utf-8"
            )
            factory = LocalApplicationTargetIntegrationFactory(
                codex_config_path=root / "config.toml",
                hindsight_profiles_dir=root / "profiles",
                ownership_dir=ownership,
            )

            adapter = factory(
                "application-target.configure",
                "hindsight",
                {"profile": "agent-memory"},
                None,
            )

            self.assertEqual(adapter.config_path, root / "profiles/agent-memory.env")

    def test_codex_factory_discovery_ignores_malformed_hindsight_ownership(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ownership = root / "ownership"
            ownership.mkdir()
            (ownership / "hindsight-Bad!.ownership.json").write_text(
                "not json", encoding="utf-8"
            )
            factory = LocalApplicationTargetIntegrationFactory(
                codex_config_path=root / "config.toml",
                hindsight_profiles_dir=root / "profiles",
                ownership_dir=ownership,
            )

            for operation in (
                "application-target.inspect",
                "application-target.configure",
                "application-target.remove",
            ):
                with self.subTest(operation=operation):
                    adapter = factory(operation, "codex", {}, None)
                    self.assertIsInstance(adapter, CodexApplicationTargetIntegration)

    def test_local_factory_refuses_ambiguous_manifest_backed_hindsight_profiles(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            factory = LocalApplicationTargetIntegrationFactory(
                codex_config_path=root / "config.toml",
                hindsight_profiles_dir=root / "profiles",
                ownership_dir=root / "ownership",
            )
            configured = factory(
                "application-target.configure",
                "hindsight",
                {"profile": "agent-memory"},
                None,
            )
            configured.apply(self.hindsight_configuration)
            manifest = json.loads(configured.manifest_path.read_text(encoding="utf-8"))
            manifest["config_path"] = str(root / "profiles/research-memory.env")
            manifest["backup_path"] = str(
                root / "ownership/hindsight-research-memory.config.backup"
            )
            (root / "ownership/hindsight-research-memory.ownership.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            with self.assertRaisesRegex(
                ApplicationTargetOwnershipRecoveryRequired,
                "exactly one recognizable",
            ):
                factory("application-target.remove", "hindsight", {}, None)

    def test_local_factory_rejects_missing_traversal_and_symlink_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = root / "profiles"
            ownership = root / "ownership"
            factory = LocalApplicationTargetIntegrationFactory(
                codex_config_path=root / "config.toml",
                hindsight_profiles_dir=profiles,
                ownership_dir=ownership,
            )
            for profile in (None, "../default", ".hidden", "name/other"):
                with (
                    self.subTest(profile=profile),
                    self.assertRaisesRegex(ValueError, "profile"),
                ):
                    factory(
                        "application-target.configure",
                        "hindsight",
                        ({"profile": profile} if profile is not None else {}),
                        None,
                    )

            profiles.mkdir()
            target = root / "outside.env"
            target.write_text("SECRET=yes\n", encoding="utf-8")
            (profiles / "agent-memory.env").symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlink"):
                factory(
                    "application-target.configure",
                    "hindsight",
                    {"profile": "agent-memory"},
                    None,
                )


if __name__ == "__main__":
    unittest.main()
