import unittest

import tomlkit

from mlxctl.application.config_schema import ConfigSchemaError, validate_config


VALID = """
schema_version = 1

[gateway]
host = "127.0.0.1"
port = 8766

[runtimes."optiq@0.2.18"]
definition = "optiq"
version = "0.2.18"
provenance = "tested"
root = "/Users/example/.local/share/mlxctl/runtimes/optiq@0.2.18"
launcher = ["/Users/example/.local/share/mlxctl/runtimes/optiq@0.2.18/bin/optiq", "serve"]
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

[clients.codex]
kind = "codex"
service = "coding"
context_window = 32768
provider = "mlxctl-local"

[clients.codex.sampling.coding]
temperature = 0.0
top_p = 0.95
"""


class ConfigSchemaV1Tests(unittest.TestCase):
    def test_loads_distinct_runtime_model_alias_service_gateway_and_client_state(
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
        self.assertEqual(config.clients["codex"].service, "coding")
        self.assertEqual(config.clients["codex"].context_window, 32768)
        self.assertEqual(config.clients["codex"].provider, "mlxctl-local")
        self.assertEqual(config.clients["codex"].sampling["coding"].top_p, 0.95)

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

    def test_rejects_unsupported_client_kind_and_invalid_sampling(self) -> None:
        with self.assertRaisesRegex(ConfigSchemaError, "client kind"):
            validate_config(
                tomlkit.parse(VALID.replace('kind = "codex"', 'kind = "other"'))
            )
        with self.assertRaisesRegex(ConfigSchemaError, "sampling"):
            validate_config(
                tomlkit.parse(
                    VALID.replace("temperature = 0.0", 'temperature = "cold"')
                )
            )

    def test_hindsight_profile_and_sampling_are_explicit_desired_state(self) -> None:
        source = (
            VALID.replace(
                "[clients.codex]",
                "[clients.hindsight]",
            )
            .replace(
                'kind = "codex"',
                'kind = "hindsight"\nprofile = "agent-memory"\nmax_concurrent = 1',
            )
            .replace(
                "[clients.codex.sampling.coding]",
                "[clients.hindsight.sampling.retain]",
            )
        )

        client = validate_config(tomlkit.parse(source)).clients["hindsight"]

        self.assertEqual(client.profile, "agent-memory")
        self.assertEqual(client.sampling["retain"].temperature, 0.0)
        self.assertEqual(client.max_concurrent, 1)

    def test_rejects_unsafe_hindsight_profile_and_ambiguous_flat_sampling(self) -> None:
        hindsight = VALID.replace("[clients.codex]", "[clients.hindsight]").replace(
            'kind = "codex"',
            'kind = "hindsight"\nprofile = "../default"\nmax_concurrent = 1',
        )
        with self.assertRaisesRegex(ConfigSchemaError, "profile"):
            validate_config(tomlkit.parse(hindsight))

        flat = VALID.replace(
            "[clients.codex.sampling.coding]", "[clients.codex.sampling]"
        )
        with self.assertRaisesRegex(ConfigSchemaError, "sampling profile"):
            validate_config(tomlkit.parse(flat))


if __name__ == "__main__":
    unittest.main()
