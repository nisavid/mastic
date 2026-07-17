import unittest

import tomlkit

from mastic.application.config_schema import ConfigSchemaError, validate_config


VALID = """
schema_version = 1

[gateway]
host = "127.0.0.1"
port = 8766

[runtimes."optiq@0.2.18"]
definition = "optiq"
version = "0.2.18"
provenance = "tested"
root = "/Users/example/.local/share/mastic/runtimes/optiq@0.2.18"
launcher = ["/Users/example/.local/share/mastic/runtimes/optiq@0.2.18/bin/optiq", "serve"]
capabilities = ["model", "host", "port", "kv_config", "mtp"]
bundle_id = "optiq-0.2.18-py313-macos-arm64"

[models.qwen-exact]
repository = "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit"
revision = "70a3aa32c7feef511182bf16aa332f37e8d82014"

[aliases.qwen-optiq]
installation = "qwen-exact"

[services.coding]
model_alias = "qwen-optiq"
runtime = "optiq@0.2.18"
route = "coding"
activation = "manual"
pinned = true

[services.coding.options]
kv_config = "kv_config.json"
mtp = true

[application_targets.codex]
kind = "codex"
service = "coding"
context_window = 32768
provider = "mlx-local"

[application_targets.codex.sampling.coding]
temperature = 0.6
top_p = 0.95
top_k = 20
min_p = 0.0
presence_penalty = 0.0
repetition_penalty = 1.0
enable_thinking = true
upstream_profile = "precise-coding-thinking"
source_url = "https://huggingface.co/Qwen/Qwen3.6-35B-A3B/blob/995ad96eacd98c81ed38be0c5b274b04031597b0/README.md#best-practices"
source_revision = "995ad96eacd98c81ed38be0c5b274b04031597b0"
"""


class ConfigSchemaV1Tests(unittest.TestCase):
    def test_rejects_legacy_client_configuration_table(self) -> None:
        legacy = VALID.replace("[application_targets.", "[clients.")

        with self.assertRaisesRegex(
            ConfigSchemaError, "unknown (?:fields|keys): clients"
        ):
            validate_config(tomlkit.parse(legacy))

    def test_loads_distinct_runtime_model_alias_service_gateway_and_application_target_state(
        self,
    ) -> None:
        config = validate_config(tomlkit.parse(VALID))

        self.assertEqual(config.gateway.port, 8766)
        self.assertEqual(config.runtimes["optiq@0.2.18"].definition, "optiq")
        self.assertIn("mtp", config.runtimes["optiq@0.2.18"].capabilities)
        self.assertEqual(config.models["qwen-exact"].revision.revision[:8], "70a3aa32")
        self.assertEqual(config.models["qwen-exact"].provenance, "cached")
        self.assertIsNone(config.models["qwen-exact"].path)
        self.assertEqual(config.aliases["qwen-optiq"].installation_name, "qwen-exact")
        self.assertTrue(config.services["coding"].pinned)
        self.assertEqual(config.services["coding"].route, "coding")
        self.assertEqual(config.application_targets["codex"].service, "coding")
        self.assertEqual(config.application_targets["codex"].context_window, 32768)
        self.assertEqual(config.application_targets["codex"].provider, "mlx-local")
        self.assertEqual(
            config.application_targets["codex"].sampling["coding"].top_p, 0.95
        )
        self.assertEqual(
            config.application_targets["codex"].sampling["coding"].top_k, 20
        )
        self.assertEqual(
            config.application_targets["codex"].sampling["coding"].min_p, 0.0
        )
        self.assertEqual(
            config.application_targets["codex"].sampling["coding"].presence_penalty, 0.0
        )
        self.assertEqual(
            config.application_targets["codex"].sampling["coding"].repetition_penalty,
            1.0,
        )
        self.assertTrue(
            config.application_targets["codex"].sampling["coding"].enable_thinking
        )
        self.assertEqual(
            config.application_targets["codex"].sampling["coding"].upstream_profile,
            "precise-coding-thinking",
        )
        self.assertEqual(
            config.application_targets["codex"].sampling["coding"].source_revision,
            "995ad96eacd98c81ed38be0c5b274b04031597b0",
        )

    def test_rejects_unknown_keys_raw_argv_and_environment_escape_hatches(self) -> None:
        for insertion in (
            "mystery = true\n",
            'arguments = ["--unsafe"]\n',
            'environment = { TOKEN = "secret" }\n',
        ):
            source = VALID.replace("pinned = true\n", f"pinned = true\n{insertion}")
            with (
                self.subTest(insertion=insertion),
                self.assertRaises(ConfigSchemaError),
            ):
                validate_config(tomlkit.parse(source))

    def test_rejects_non_loopback_gateway_and_duplicate_routes(self) -> None:
        with self.assertRaisesRegex(ConfigSchemaError, "loopback"):
            validate_config(tomlkit.parse(VALID.replace("127.0.0.1", "0.0.0.0")))
        duplicate = (
            VALID
            + """
[services.memory]
model_alias = "qwen-optiq"
runtime = "optiq@0.2.18"
route = "coding"
"""
        )
        with self.assertRaisesRegex(ConfigSchemaError, "Gateway route"):
            validate_config(tomlkit.parse(duplicate))

    def test_rejects_missing_references_and_mutable_model_revision(self) -> None:
        with self.assertRaisesRegex(ConfigSchemaError, "immutable commit SHA"):
            validate_config(
                tomlkit.parse(
                    VALID.replace("70a3aa32c7feef511182bf16aa332f37e8d82014", "main")
                )
            )
        with self.assertRaisesRegex(ConfigSchemaError, "unknown Model Alias"):
            validate_config(
                tomlkit.parse(
                    VALID.replace(
                        'model_alias = "qwen-optiq"', 'model_alias = "missing"'
                    )
                )
            )

    def test_adopted_model_requires_an_absolute_external_path(self) -> None:
        adopted = VALID.replace(
            'revision = "70a3aa32c7feef511182bf16aa332f37e8d82014"',
            'revision = "70a3aa32c7feef511182bf16aa332f37e8d82014"\n'
            'provenance = "adopted"\npath = "/Volumes/models/qwen"',
        )
        model = validate_config(tomlkit.parse(adopted)).models["qwen-exact"]
        self.assertEqual(model.provenance, "adopted")
        self.assertEqual(model.path, "/Volumes/models/qwen")
        with self.assertRaisesRegex(ConfigSchemaError, "absolute"):
            validate_config(
                tomlkit.parse(adopted.replace("/Volumes/models/qwen", "qwen"))
            )

    def test_rejects_unsupported_application_target_kind_and_invalid_sampling(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ConfigSchemaError, "unsupported Application Configuration Target kind"
        ):
            validate_config(
                tomlkit.parse(VALID.replace('kind = "codex"', 'kind = "other"'))
            )
        with self.assertRaisesRegex(ConfigSchemaError, "sampling"):
            validate_config(
                tomlkit.parse(
                    VALID.replace("temperature = 0.6", 'temperature = "cold"')
                )
            )

        for original, invalid in (
            ("top_k = 20", "top_k = -1"),
            ("min_p = 0.0", "min_p = 1.1"),
            ("presence_penalty = 0.0", "presence_penalty = 2.1"),
            ("repetition_penalty = 1.0", "repetition_penalty = 0.0"),
            ("enable_thinking = true", 'enable_thinking = "yes"'),
            ("enable_thinking = true", "preserve_thinking = 1"),
            (
                'upstream_profile = "precise-coding-thinking"',
                'upstream_profile = "../bad"',
            ),
            (
                'source_url = "https://huggingface.co/Qwen/Qwen3.6-35B-A3B/blob/995ad96eacd98c81ed38be0c5b274b04031597b0/README.md#best-practices"',
                'source_url = "http://example.test/model-card"',
            ),
            (
                'source_revision = "995ad96eacd98c81ed38be0c5b274b04031597b0"',
                'source_revision = "main"',
            ),
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ConfigSchemaError, "sampling"),
            ):
                validate_config(tomlkit.parse(VALID.replace(original, invalid)))

    def test_hindsight_profile_and_sampling_are_explicit_desired_state(self) -> None:
        source = (
            VALID.replace(
                "[application_targets.codex]",
                "[application_targets.hindsight]",
            )
            .replace(
                'kind = "codex"',
                'kind = "hindsight"\nprofile = "agent-memory"\nmax_concurrent = 1',
            )
            .replace(
                "[application_targets.codex.sampling.coding]",
                "[application_targets.hindsight.sampling.verification]",
            )
        )

        profile_body = VALID.split("[application_targets.codex.sampling.coding]\n", 1)[
            1
        ]
        source += "\n[application_targets.hindsight.sampling.retain]\n" + profile_body
        source += "\n[application_targets.hindsight.sampling.reflect]\n" + profile_body
        source += (
            "\n[application_targets.hindsight.sampling.consolidation]\n" + profile_body
        )

        application_target = validate_config(tomlkit.parse(source)).application_targets[
            "hindsight"
        ]

        self.assertEqual(application_target.profile, "agent-memory")
        self.assertEqual(application_target.sampling["retain"].temperature, 0.6)
        self.assertEqual(application_target.max_concurrent, 1)

    def test_rejects_unsafe_hindsight_profile_and_ambiguous_flat_sampling(self) -> None:
        hindsight = VALID.replace(
            "[application_targets.codex]", "[application_targets.hindsight]"
        ).replace(
            'kind = "codex"',
            'kind = "hindsight"\nprofile = "../default"\nmax_concurrent = 1',
        )
        with self.assertRaisesRegex(ConfigSchemaError, "profile"):
            validate_config(tomlkit.parse(hindsight))

        flat = VALID.replace(
            "[application_targets.codex.sampling.coding]",
            "[application_targets.codex.sampling]",
        )
        with self.assertRaisesRegex(ConfigSchemaError, "sampling profile"):
            validate_config(tomlkit.parse(flat))

    def test_rejects_partial_workload_sets_and_unrepresentable_codex_values(
        self,
    ) -> None:
        partial_hindsight = VALID.replace(
            "[application_targets.codex]", "[application_targets.hindsight]"
        ).replace(
            'kind = "codex"',
            'kind = "hindsight"\nprofile = "default"\nmax_concurrent = 1',
        )
        with self.assertRaisesRegex(ConfigSchemaError, "requires sampling profiles"):
            validate_config(tomlkit.parse(partial_hindsight))

        for original, invalid in (
            ("min_p = 0.0", "min_p = 0.1"),
            ("presence_penalty = 0.0", "presence_penalty = 1.5"),
            ("repetition_penalty = 1.0", "repetition_penalty = 1.1"),
            ("top_k = 20", "top_k = 20\nmax_tokens = 100"),
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ConfigSchemaError, "Responses"),
            ):
                validate_config(tomlkit.parse(VALID.replace(original, invalid)))

    def test_rejects_non_finite_sampling_values(self) -> None:
        for value in ("nan", "+inf", "-inf"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ConfigSchemaError, "finite"),
            ):
                validate_config(
                    tomlkit.parse(
                        VALID.replace("temperature = 0.6", f"temperature = {value}")
                    )
                )


if __name__ == "__main__":
    unittest.main()
