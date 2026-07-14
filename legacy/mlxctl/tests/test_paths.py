import unittest
from pathlib import Path

from mlxctl.paths import resolve_paths


class ResolvePathsTests(unittest.TestCase):
    def test_defaults_match_the_deployment_contract(self) -> None:
        paths = resolve_paths(environ={}, home=Path("/Users/tester"))

        self.assertEqual(paths.config_dir, Path("/Users/tester/.config/mlxd"))
        self.assertEqual(
            paths.config_file, Path("/Users/tester/.config/mlxd/config.toml")
        )
        self.assertEqual(paths.state_dir, Path("/Users/tester/.local/state/mlxd"))
        self.assertEqual(paths.log_dir, Path("/Users/tester/Library/Logs/mlxd"))

    def test_environment_overrides_each_directory(self) -> None:
        paths = resolve_paths(
            environ={
                "MLXD_CONFIG_DIR": "/runtime/config",
                "MLXD_STATE_DIR": "/runtime/state",
                "MLXD_LOG_DIR": "/runtime/logs",
            },
            home=Path("/Users/tester"),
        )

        self.assertEqual(paths.config_dir, Path("/runtime/config"))
        self.assertEqual(paths.config_file, Path("/runtime/config/config.toml"))
        self.assertEqual(paths.state_dir, Path("/runtime/state"))
        self.assertEqual(paths.log_dir, Path("/runtime/logs"))

    def test_tilde_overrides_use_the_injected_home_directory(self) -> None:
        paths = resolve_paths(
            environ={
                "MLXD_CONFIG_DIR": "~/config",
                "MLXD_STATE_DIR": "~/state",
                "MLXD_LOG_DIR": "~/logs",
            },
            home=Path("/Users/tester"),
        )

        self.assertEqual(paths.config_dir, Path("/Users/tester/config"))
        self.assertEqual(paths.state_dir, Path("/Users/tester/state"))
        self.assertEqual(paths.log_dir, Path("/Users/tester/logs"))


if __name__ == "__main__":
    unittest.main()
