import tempfile
import unittest
from pathlib import Path

from mlxctl.config import ConfigError, load_config


class LoadConfigTests(unittest.TestCase):
    def test_loads_versioned_models_daemon_and_named_servers(self) -> None:
        config = self._load(
            """
schema_version = 1

[models.tiny]
reference = "mlx-community/tiny"

[servers.chat]
type = "mlx_lm"
model = "tiny"
port = 8080
environment = { TOKENIZERS_PARALLELISM = "false" }

[servers.chat.options]
prompt_cache_size = 4
pipeline = true
"""
        )

        self.assertEqual(config.schema_version, 1)
        self.assertEqual(config.daemon.readiness_timeout_seconds, 120)
        self.assertEqual(config.daemon.stop_timeout_seconds, 10)
        self.assertEqual(config.daemon.metrics_interval_seconds, 5)
        self.assertEqual(config.metrics.retention_days, 30)
        self.assertEqual(config.models["tiny"].reference, "mlx-community/tiny")
        server = config.servers["chat"]
        self.assertEqual(server.type, "mlx_lm")
        self.assertEqual(server.model, "tiny")
        self.assertEqual(server.host, "127.0.0.1")
        self.assertEqual(server.port, 8080)
        self.assertEqual(server.environment["TOKENIZERS_PARALLELISM"], "false")
        self.assertEqual(server.options["prompt_cache_size"], 4)
        self.assertIs(server.options["pipeline"], True)

    def test_rejects_unknown_keys_at_every_schema_level(self) -> None:
        cases = {
            "root key 'mystery'": """
schema_version = 1
mystery = true
""",
            "daemon key 'mystery'": """
schema_version = 1
[daemon]
mystery = true
""",
            "metrics key 'mystery'": """
schema_version = 1
[metrics]
mystery = true
""",
            "model 'tiny' key 'mystery'": """
schema_version = 1
[models.tiny]
reference = "repo/tiny"
mystery = true
""",
            "server 'chat' key 'arguments'": """
schema_version = 1
[models.tiny]
reference = "repo/tiny"
[servers.chat]
type = "mlx_lm"
model = "tiny"
port = 8080
arguments = ["--unsafe"]
""",
            "server 'chat' option 'kv_bits'": """
schema_version = 1
[models.tiny]
reference = "repo/tiny"
[servers.chat]
type = "mlx_lm"
model = "tiny"
port = 8080
[servers.chat.options]
kv_bits = 4
""",
        }

        for message, source in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ConfigError, message):
                    self._load(source)

    def test_loads_metrics_retention(self) -> None:
        config = self._load(
            """
schema_version = 1
[metrics]
retention_days = 90
"""
        )

        self.assertEqual(config.metrics.retention_days, 90)

    def test_freezes_list_valued_server_options(self) -> None:
        config = self._load(
            """
schema_version = 1
[models.code]
reference = "repo/code"
[servers.code]
type = "optiq"
model = "code"
port = 8080
[servers.code.options]
adapter = ["/models/a"]
"""
        )

        adapters = config.servers["code"].options["adapter"]

        self.assertEqual(adapters, ("/models/a",))
        with self.assertRaises(AttributeError):
            adapters.append("/models/b")  # type: ignore[attr-defined]

    def test_rejects_non_positive_metrics_retention(self) -> None:
        with self.assertRaisesRegex(
            ConfigError, "metrics retention_days must be a positive integer"
        ):
            self._load(
                """
schema_version = 1
[metrics]
retention_days = 0
"""
            )

    def test_rejects_invalid_schema_values_and_references(self) -> None:
        cases = {
            "schema_version must be 1": "schema_version = 2\n",
            "schema_version is required": "[models]\n",
            "model 'tiny' requires string 'reference'": """
schema_version = 1
[models.tiny]
""",
            "server 'chat' requires integer 'port'": """
schema_version = 1
[models.tiny]
reference = "repo/tiny"
[servers.chat]
type = "mlx_lm"
model = "tiny"
""",
            "server 'chat' type 'other' is not supported": """
schema_version = 1
[models.tiny]
reference = "repo/tiny"
[servers.chat]
type = "other"
model = "tiny"
port = 8080
""",
            "server 'chat' model alias 'missing' is not defined": """
schema_version = 1
[models.tiny]
reference = "repo/tiny"
[servers.chat]
type = "mlx_lm"
model = "missing"
port = 8080
""",
            "server 'chat' host '0.0.0.0' is not loopback": """
schema_version = 1
[models.tiny]
reference = "repo/tiny"
[servers.chat]
type = "mlx_lm"
model = "tiny"
host = "0.0.0.0"
port = 8080
""",
            "server 'chat' port must be in 1..65535": """
schema_version = 1
[models.tiny]
reference = "repo/tiny"
[servers.chat]
type = "mlx_lm"
model = "tiny"
port = 70000
""",
            "daemon readiness_timeout_seconds must be a positive number": """
schema_version = 1
[daemon]
readiness_timeout_seconds = 0
""",
            "server 'chat' environment value 'WORKERS' must be a string": """
schema_version = 1
[models.tiny]
reference = "repo/tiny"
[servers.chat]
type = "mlx_lm"
model = "tiny"
port = 8080
environment = { WORKERS = 2 }
""",
            "server 'chat' option 'prompt_cache_size' must be an integer": """
schema_version = 1
[models.tiny]
reference = "repo/tiny"
[servers.chat]
type = "mlx_lm"
model = "tiny"
port = 8080
[servers.chat.options]
prompt_cache_size = "four"
""",
        }

        for message, source in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ConfigError, message):
                    self._load(source)

    def test_rejects_duplicate_listen_addresses(self) -> None:
        with self.assertRaisesRegex(
            ConfigError,
            "servers 'first' and 'second' share listen address 127.0.0.1:8080",
        ):
            self._load(
                """
schema_version = 1
[models.tiny]
reference = "repo/tiny"
[servers.first]
type = "mlx_lm"
model = "tiny"
port = 8080
[servers.second]
type = "optiq"
model = "tiny"
port = 8080
"""
            )

    def test_rejects_model_and_server_aliases_that_are_unsafe_for_paths(self) -> None:
        cases = {
            "model alias '../tiny' must match": """
schema_version = 1
[models."../tiny"]
reference = "repo/tiny"
""",
            "server alias '/chat' must match": """
schema_version = 1
[models.tiny]
reference = "repo/tiny"
[servers."/chat"]
type = "mlx_lm"
model = "tiny"
port = 8080
""",
            "model alias '.hidden' must match": """
schema_version = 1
[models.".hidden"]
reference = "repo/tiny"
""",
        }

        for message, source in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ConfigError, message):
                    self._load(source)

    def _load(self, source: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(source, encoding="utf-8")
            return load_config(path)


if __name__ == "__main__":
    unittest.main()
