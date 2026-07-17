import json
import math
import shutil
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import tomlkit

from mastic.application.config_schema import ClientSettings
from mastic.infrastructure.client_integrations import (
    ClientConfiguration,
    ClientIntegrationConflict,
    CodexModelMetadata,
    CodexClientIntegration,
    HindsightClientIntegration,
    LocalClientIntegrationFactory,
    SamplingProfile,
)


class ClientIntegrationV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.configuration = ClientConfiguration(
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
                "coding": SamplingProfile(
                    temperature=0.6,
                    top_p=0.95,
                    top_k=20,
                    min_p=0.0,
                    presence_penalty=0.0,
                    repetition_penalty=1.0,
                    enable_thinking=True,
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
            codex_provider_id="mlx-local",
            hindsight_provider="openai",
            max_concurrent=1,
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
            configuration = replace(
                self.configuration,
                context_window=131_072,
                service_name="qwen36-optiq",
                codex_model=CodexModelMetadata(
                    slug="qwen36-optiq",
                    display_name="Qwen3.6 35B A3B OptiQ 4-bit",
                    description="Local Qwen3.6 mixture-of-experts coding model.",
                ),
            )
            adapter = CodexClientIntegration(
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

    def test_codex_catalog_inspect_reports_and_repair_fixes_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "catalog.json"
            configuration = replace(
                self.configuration,
                context_window=196_608,
                codex_model=CodexModelMetadata(
                    slug="coding",
                    display_name="Local coding model",
                    description="Local model",
                ),
            )
            adapter = CodexClientIntegration(
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
            self.assertIn("mastic client configure codex", drifted["next_actions"])
            self.assertTrue(repaired.changed)
            self.assertEqual(healthy["state"], "healthy")

    def test_legacy_codex_ownership_requires_catalog_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = CodexClientIntegration(
                root / "config.toml",
                root / "owner.json",
                root / "config.backup",
                bundled_catalog=self._bundled_codex_catalog,
                catalog_validator=lambda _path: None,
            )
            adapter.apply(self.configuration)

            report = adapter.inspect()

            self.assertEqual(report["state"], "missing")
            self.assertIn("mastic client configure codex", report["next_actions"])

    def test_codex_inspect_detects_real_config_catalog_pointer_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = replace(
                self.configuration,
                context_window=131_072,
                codex_model=CodexModelMetadata("coding", "Coding", "Local model"),
            )
            adapter = CodexClientIntegration(
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

    def test_legacy_codex_migration_restores_preexisting_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "catalog.json"
            original = b'{"models":[{"slug":"user"}]}\n'
            adapter = CodexClientIntegration(
                root / "config.toml",
                root / "owner.json",
                root / "config.backup",
                catalog_path=catalog,
                catalog_backup_path=root / "catalog.backup",
                bundled_catalog=self._bundled_codex_catalog,
                catalog_validator=lambda _path: None,
            )
            adapter.apply(self.configuration)
            catalog.write_bytes(original)
            document = tomlkit.parse((root / "config.toml").read_text())
            document["model_catalog_json"] = str(catalog)
            (root / "config.toml").write_text(document.as_string())
            adapter.apply(
                replace(
                    self.configuration,
                    context_window=131_072,
                    codex_model=CodexModelMetadata("coding", "Coding", "Local model"),
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

            adapter = CodexClientIntegration(
                config,
                root / "owner.json",
                root / "config.backup",
                catalog_path=catalog,
                catalog_backup_path=root / "catalog.backup",
                bundled_catalog=self._bundled_codex_catalog,
                catalog_validator=reject,
            )
            configuration = replace(
                self.configuration,
                context_window=131_072,
                codex_model=CodexModelMetadata(
                    slug="coding",
                    display_name="Local coding model",
                    description="Local model",
                ),
            )

            with self.assertRaisesRegex(RuntimeError, "rejected"):
                adapter.apply(configuration)

            self.assertEqual(config.read_text(), 'unrelated = "keep"\n')
            self.assertEqual(catalog.read_text(), '{"models":[{"slug":"user"}]}\n')
            self.assertFalse((root / "owner.json").exists())

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
            configuration = replace(
                self.configuration,
                context_window=131_072,
                codex_model=CodexModelMetadata("coding", "Coding", "Local model"),
            )
            paths[3].write_text('{"models":[{"slug":"user"}]}\n')
            adapter = CodexClientIntegration(
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

            failing = CodexClientIntegration(
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
            configuration = replace(
                self.configuration,
                context_window=131_072,
                codex_model=CodexModelMetadata("coding", "Coding", "Local model"),
            )
            adapter = CodexClientIntegration(
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

            failing = CodexClientIntegration(
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
            adapter = CodexClientIntegration(
                root / "config.toml",
                root / "owner.json",
                root / "config.backup",
                catalog_path=root / "catalog.json",
                catalog_backup_path=root / "catalog.backup",
            )
            result = adapter.apply(
                replace(
                    self.configuration,
                    context_window=131_072,
                    codex_model=CodexModelMetadata(
                        slug="qwen36-optiq",
                        display_name="Qwen3.6 35B A3B OptiQ 4-bit",
                        description="Local coding model",
                    ),
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
            adapter = CodexClientIntegration(config, manifest, backup)

            preview = adapter.preview(self.configuration)
            applied = adapter.apply(self.configuration)
            second = adapter.apply(self.configuration)

            document = tomlkit.parse(config.read_text(encoding="utf-8"))
            self.assertIn(("model",), {change.path for change in preview})
            self.assertTrue(applied.changed)
            self.assertFalse(second.changed)
            self.assertEqual(document["model"], "coding")
            self.assertEqual(document["oss_provider"], "mlx-local")
            self.assertEqual(
                document["model_providers"]["mlx-local"]["base_url"],
                "http://127.0.0.1:8766/clients/codex/profiles/coding/v1",
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
            adapter = CodexClientIntegration(
                config,
                root / "mastic" / "codex-ownership.json",
                root / "mastic" / "codex-config.backup",
            )
            legacy = replace(self.configuration, codex_provider_id="mastic-local")

            adapter.apply(legacy)
            adapter.apply(self.configuration)

            document = tomlkit.parse(config.read_text(encoding="utf-8"))
            self.assertEqual(document["model_provider"], "mlx-local")
            self.assertEqual(document["oss_provider"], "mlx-local")
            self.assertIn("mlx-local", document["model_providers"])
            self.assertNotIn("mastic-local", document["model_providers"])

    def test_managed_clients_fail_closed_on_missing_or_unrepresentable_profiles(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex = CodexClientIntegration(
                root / "codex.toml", root / "codex-owner.json", root / "codex-backup"
            )
            missing_coding = replace(
                self.configuration,
                sampling_profiles={
                    "codng": SamplingProfile(temperature=0.6, top_p=0.95)
                },
            )
            with self.assertRaisesRegex(ValueError, "requires sampling profiles"):
                codex.preview(missing_coding)

            unsupported = replace(
                self.configuration,
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

            hindsight = HindsightClientIntegration(
                root / "hindsight.env",
                root / "hindsight-owner.json",
                root / "hindsight-backup",
            )
            with self.assertRaisesRegex(ValueError, "requires sampling profiles"):
                hindsight.preview(
                    replace(
                        self.configuration,
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
            configuration = replace(self.configuration, credential_path=credential)

            codex_path = root / "codex.toml"
            codex = CodexClientIntegration(
                codex_path, root / "codex-owner.json", root / "codex-backup"
            )
            codex.apply(configuration)
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
            hindsight = HindsightClientIntegration(
                hindsight_path,
                root / "hindsight-owner.json",
                root / "hindsight-backup",
            )
            preview = hindsight.preview(configuration)
            applied = hindsight.apply(configuration)

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
            adapter = CodexClientIntegration(
                config, root / "owner.json", root / "backup"
            )
            adapter.apply(self.configuration)
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
            adapter = CodexClientIntegration(
                config, root / "owner.json", root / "backup"
            )
            adapter.apply(self.configuration)
            adapter.manifest_path.unlink()
            adapter.backup_path.unlink()

            adopted = adapter.apply(self.configuration, takeover=True)
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
            adapter = CodexClientIntegration(
                config, root / "owner.json", root / "backup"
            )
            adapter.apply(self.configuration)
            changed = ClientConfiguration(
                gateway_endpoint=self.configuration.gateway_endpoint,
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
            adapter = CodexClientIntegration(
                config, root / "owner.json", root / "backup"
            )
            adapter.apply(self.configuration)
            configured = config.read_text(encoding="utf-8")

            adapter.restore()
            self.assertEqual(config.read_text(encoding="utf-8"), 'model = "before"\n')

            adapter.apply(self.configuration)
            config.write_text(configured + "# user edit\n", encoding="utf-8")
            with self.assertRaises(ClientIntegrationConflict):
                adapter.restore()

    def test_invalid_codex_input_and_replace_failure_leave_current_config_intact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text("not valid = [\n", encoding="utf-8")
            adapter = CodexClientIntegration(
                config, root / "owner.json", root / "backup"
            )
            before = config.read_bytes()

            with self.assertRaises(Exception):
                adapter.apply(self.configuration)
            self.assertEqual(config.read_bytes(), before)

            config.write_text('model = "before"\n', encoding="utf-8")

            def fail_replace(path: Path, payload: bytes) -> None:
                raise OSError("simulated replace failure")

            failing = CodexClientIntegration(
                config, root / "owner-2.json", root / "backup-2", replace=fail_replace
            )
            with self.assertRaisesRegex(OSError, "simulated"):
                failing.apply(self.configuration)
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
            adapter = HindsightClientIntegration(
                config, root / "owner.json", root / "backup"
            )

            changes = adapter.preview(self.configuration)
            applied = adapter.apply(self.configuration)
            calls: list[tuple[str, str, dict[str, object]]] = []
            response = adapter.test(
                self.configuration,
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
                "HINDSIGHT_API_LLM_BASE_URL=http://127.0.0.1:8766/clients/hindsight/profiles/verification/v1",
                text,
            )
            self.assertIn("HINDSIGHT_API_LLM_TEMPERATURE_REFLECT=1.0", text)
            self.assertIn(
                "HINDSIGHT_API_RETAIN_LLM_BASE_URL=http://127.0.0.1:8766/clients/hindsight/profiles/retain/v1",
                text,
            )
            self.assertIn(
                "HINDSIGHT_API_REFLECT_LLM_BASE_URL=http://127.0.0.1:8766/clients/hindsight/profiles/reflect/v1",
                text,
            )
            self.assertIn(
                "HINDSIGHT_API_CONSOLIDATION_LLM_BASE_URL=http://127.0.0.1:8766/clients/hindsight/profiles/consolidation/v1",
                text,
            )
            self.assertEqual(
                calls,
                [
                    (
                        "http://127.0.0.1:8766/clients/hindsight/profiles/reflect/v1",
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

    def test_hindsight_takeover_records_already_equal_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "profile.env"
            adapter = HindsightClientIntegration(
                config, root / "owner.json", root / "backup"
            )
            adapter.apply(self.configuration)
            adapter.manifest_path.unlink()
            adapter.backup_path.unlink()

            adopted = adapter.apply(self.configuration, takeover=True)

            self.assertTrue(adopted.changed)
            self.assertFalse(adopted.changes)
            owned = json.loads(adapter.manifest_path.read_text(encoding="utf-8"))[
                "fields"
            ]
            self.assertTrue(owned)
            self.assertTrue(all(not item["before_present"] for item in owned))

    def test_client_endpoint_requires_a_literal_loopback_origin(self) -> None:
        with self.assertRaisesRegex(ValueError, "literal HTTP loopback"):
            ClientConfiguration(
                gateway_endpoint="http://localhost:8766/v1",
                service_name="coding",
            )

    def test_local_factory_selects_explicit_hindsight_profile_and_owned_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = root / "hindsight" / "profiles"
            ownership = root / "mastic" / "clients"
            factory = LocalClientIntegrationFactory(
                codex_config_path=root / "codex" / "config.toml",
                hindsight_profiles_dir=profiles,
                ownership_dir=ownership,
            )

            adapter = factory(
                "client.configure",
                "hindsight",
                {"profile": "agent-memory"},
                None,
            )

            self.assertEqual(adapter.config_path, profiles / "agent-memory.env")
            self.assertEqual(
                adapter.manifest_path,
                ownership / "hindsight-agent-memory.ownership.json",
            )

            stored = ClientSettings(
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
                "client.test",
                "hindsight",
                {"profile": "reflect"},
                stored,
            )
            self.assertEqual(test_adapter.config_path, profiles / "agent-memory.env")

    def test_local_factory_rejects_missing_traversal_and_symlink_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = root / "profiles"
            ownership = root / "ownership"
            factory = LocalClientIntegrationFactory(
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
                        "client.configure",
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
                    "client.configure",
                    "hindsight",
                    {"profile": "agent-memory"},
                    None,
                )


if __name__ == "__main__":
    unittest.main()
