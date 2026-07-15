import json
import stat
import tempfile
import unittest
from pathlib import Path

import tomlkit

from mlxctl.application.config_schema import ClientSettings
from mlxctl.infrastructure.client_integrations import (
    ClientConfiguration,
    ClientIntegrationConflict,
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
                "coding": SamplingProfile(temperature=0.0, top_p=0.95),
                "retain": SamplingProfile(temperature=0.1, top_p=0.9),
                "reflect": SamplingProfile(temperature=0.9, top_p=0.95),
            },
            codex_provider_id="mlxctl-local",
            hindsight_provider="openai",
            max_concurrent=1,
        )

    def test_codex_preview_apply_and_remove_preserve_unrelated_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "codex" / "config.toml"
            manifest = root / "mlxctl" / "codex-ownership.json"
            backup = root / "mlxctl" / "codex-config.backup"
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
            self.assertEqual(
                document["model_providers"]["mlxctl-local"]["base_url"],
                self.configuration.gateway_endpoint,
            )
            self.assertEqual(document["profiles"]["coding"]["temperature"], 0.0)
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
            self.assertNotIn("mlxctl-local", restored["model_providers"])
            self.assertEqual(restored["tui"]["theme"], "catppuccin-mocha")

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
                    "general": SamplingProfile(temperature=0.2, top_p=0.9)
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
            self.assertIn("HINDSIGHT_API_LLM_TEMPERATURE_REFLECT=0.9", text)
            self.assertEqual(
                calls,
                [
                    (
                        self.configuration.gateway_endpoint,
                        "coding",
                        {"temperature": 0.9, "top_p": 0.95},
                    )
                ],
            )
            self.assertEqual(response, {"text": "ready"})

            adapter.remove()
            restored = config.read_text(encoding="utf-8")
            self.assertIn("HINDSIGHT_BANK_ID=existing-bank", restored)
            self.assertIn("HINDSIGHT_API_LLM_MODEL=cloud", restored)
            self.assertNotIn("HINDSIGHT_API_LLM_BASE_URL", restored)

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
            ownership = root / "mlxctl" / "clients"
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
