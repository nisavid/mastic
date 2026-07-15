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

[clients.codex.sampling]
temperature = 0.0
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
        self.assertEqual(config.aliases["qwen-optiq"].installation_name, "qwen-exact")
        self.assertTrue(config.services["coding"].pinned)
        self.assertEqual(config.services["coding"].route, "coding")
        self.assertEqual(config.clients["codex"].service, "coding")

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


if __name__ == "__main__":
    unittest.main()
