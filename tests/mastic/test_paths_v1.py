import tempfile
import unittest
from pathlib import Path

from mastic.infrastructure.paths_v1 import resolve_paths


class PathsV1Tests(unittest.TestCase):
    def test_defaults_are_product_named_and_keep_desired_operational_and_logs_separate(
        self,
    ) -> None:
        paths = resolve_paths(home=Path("/Users/ivan"), environment={})

        self.assertEqual(
            paths.config_file, Path("/Users/ivan/.config/mastic/config.toml")
        )
        self.assertEqual(
            paths.state_db, Path("/Users/ivan/.local/state/mastic/state.sqlite3")
        )
        self.assertEqual(
            paths.control_socket, Path("/Users/ivan/.local/state/mastic/masticd.sock")
        )
        self.assertEqual(
            paths.runtime_dir, Path("/Users/ivan/.local/share/mastic/runtimes")
        )
        self.assertEqual(paths.log_dir, Path("/Users/ivan/Library/Logs/mastic"))

    def test_xdg_and_explicit_overrides_are_deterministic(self) -> None:
        environment = {
            "XDG_CONFIG_HOME": "/cfg",
            "XDG_STATE_HOME": "/state",
            "XDG_DATA_HOME": "/data",
            "MASTIC_LOG_DIR": "/logs",
        }

        paths = resolve_paths(home=Path("/home/user"), environment=environment)

        self.assertEqual(paths.config_dir, Path("/cfg/mastic"))
        self.assertEqual(paths.state_dir, Path("/state/mastic"))
        self.assertEqual(paths.data_dir, Path("/data/mastic"))
        self.assertEqual(paths.log_dir, Path("/logs"))

    def test_prepare_creates_private_directories_without_creating_configuration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_paths(home=Path(directory), environment={})

            paths.prepare()

            for path in (
                paths.config_dir,
                paths.state_dir,
                paths.data_dir,
                paths.runtime_dir,
                paths.log_dir,
            ):
                self.assertTrue(path.is_dir())
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            self.assertFalse(paths.config_file.exists())


if __name__ == "__main__":
    unittest.main()
