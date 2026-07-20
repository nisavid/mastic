import tempfile
import threading
import unittest
from pathlib import Path

from mastic.infrastructure.paths_v1 import resolve_paths


class PathsV1Tests(unittest.TestCase):
    def test_rejects_blank_or_relative_path_inputs(self) -> None:
        for key in (
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_DATA_HOME",
            "MASTIC_CONFIG_DIR",
            "MASTIC_STATE_DIR",
            "MASTIC_DATA_DIR",
            "MASTIC_LOG_DIR",
        ):
            for value in ("", "relative/path"):
                with self.subTest(key=key, value=value):
                    with self.assertRaisesRegex(ValueError, "absolute"):
                        resolve_paths(home=Path("/home/user"), environment={key: value})

        with self.assertRaisesRegex(ValueError, "absolute"):
            resolve_paths(home=Path("relative/home"), environment={})

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
            paths.planning_grant_key,
            Path("/Users/ivan/.local/state/mastic/planning-grant.key"),
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

    def test_coordination_namespace_is_stable_across_product_root_overrides(
        self,
    ) -> None:
        home = Path("/home/user")
        first = resolve_paths(
            home=home,
            environment={
                "MASTIC_CONFIG_DIR": "/first/config",
                "MASTIC_STATE_DIR": "/first/state",
                "MASTIC_DATA_DIR": "/first/data",
                "MASTIC_LOG_DIR": "/first/logs",
            },
        )
        second = resolve_paths(
            home=home,
            environment={
                "MASTIC_CONFIG_DIR": "/second/config",
                "MASTIC_STATE_DIR": "/second/state",
                "MASTIC_DATA_DIR": "/second/data",
                "MASTIC_LOG_DIR": "/second/logs",
            },
        )

        self.assertEqual(first.coordination_dir, home / ".local/state/.mastic-locks")
        self.assertEqual(second.coordination_dir, first.coordination_dir)

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

    def test_concurrent_prepare_treats_an_already_created_directory_as_success(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_paths(home=Path(directory), environment={})
            barrier = threading.Barrier(4)
            errors: list[BaseException] = []

            def prepare() -> None:
                barrier.wait()
                try:
                    paths.prepare()
                except BaseException as error:  # pragma: no cover - assertion aid
                    errors.append(error)

            threads = [threading.Thread(target=prepare) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

            self.assertEqual(errors, [])
            self.assertTrue(all(not thread.is_alive() for thread in threads))


if __name__ == "__main__":
    unittest.main()
