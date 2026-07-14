import tempfile
import unittest
from pathlib import Path

from mlxctl.adapters import AdapterRegistry, Endpoint
from mlxctl.config import load_config


class AdapterRegistryTests(unittest.TestCase):
    def test_prepares_exact_mlx_lm_argv_and_environment(self) -> None:
        config = self._load(
            """
schema_version = 1
[models.tiny]
reference = "mlx-community/tiny"
[servers.chat]
type = "mlx_lm"
model = "tiny"
host = "127.0.0.2"
port = 8080
environment = { TOKENIZERS_PARALLELISM = "false" }
[servers.chat.options]
prompt_cache_size = 4
pipeline = true
top_p = 0.9
"""
        )

        prepared = AdapterRegistry().prepare(
            config.servers["chat"],
            config.models["tiny"],
            Endpoint("127.0.0.9", 49152),
        )

        self.assertEqual(
            prepared.argv,
            (
                "mlx_lm.server",
                "--model",
                "mlx-community/tiny",
                "--host",
                "127.0.0.9",
                "--port",
                "49152",
                "--prompt-cache-size",
                "4",
                "--pipeline",
                "--top-p",
                "0.9",
            ),
        )
        self.assertEqual(
            dict(prepared.environment), {"TOKENIZERS_PARALLELISM": "false"}
        )

    def test_prepares_exact_optiq_argv_with_typed_options(self) -> None:
        config = self._load(
            """
schema_version = 1
[models.code]
reference = "/models/code"
[servers.code]
type = "optiq"
model = "code"
port = 9090
[servers.code.options]
prompt_concurrency = 2
top_k = 40
kv_bits = 4
kv_group_size = 64
quantized_kv_start = 128
kv_config = "/models/kv.json"
adapter = ["/models/a", "/models/b"]
anthropic = false
allow_model_switch = false
idle_timeout = 30.5
max_context = 8192
"""
        )

        prepared = AdapterRegistry().prepare(
            config.servers["code"],
            config.models["code"],
            Endpoint("127.0.0.10", 49153),
        )

        self.assertEqual(
            prepared.argv,
            (
                "optiq",
                "serve",
                "--model",
                "/models/code",
                "--host",
                "127.0.0.10",
                "--port",
                "49153",
                "--prompt-concurrency",
                "2",
                "--top-k",
                "40",
                "--kv-bits",
                "4",
                "--kv-group-size",
                "64",
                "--quantized-kv-start",
                "128",
                "--kv-config",
                "/models/kv.json",
                "--adapter",
                "/models/a",
                "--adapter",
                "/models/b",
                "--no-anthropic",
                "--single-model",
                "--idle-timeout",
                "30.5",
                "--max-context",
                "8192",
            ),
        )

    def test_rejects_a_non_loopback_endpoint(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "endpoint host '0.0.0.0' is not loopback"
        ):
            Endpoint("0.0.0.0", 49152)

    def _load(self, source: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(source, encoding="utf-8")
            return load_config(path)


if __name__ == "__main__":
    unittest.main()
