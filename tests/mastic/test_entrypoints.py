import subprocess
import shutil
import unittest


class EntrypointTests(unittest.TestCase):
    def test_installed_cli_script_has_help(self) -> None:
        executable = shutil.which("mastic")
        self.assertIsNotNone(executable)
        result = subprocess.run(
            [executable, "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: mastic", result.stdout)

    def test_installed_daemon_script_has_help(self) -> None:
        executable = shutil.which("masticd")
        self.assertIsNotNone(executable)
        result = subprocess.run(
            [executable, "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: masticd", result.stdout)

    def test_status_help_describes_the_status_surface_without_a_server_argument(
        self,
    ) -> None:
        executable = shutil.which("mastic")
        self.assertIsNotNone(executable)
        result = subprocess.run(
            [executable, "status", "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Supervisor, Gateway, Inference Services", result.stdout)
        self.assertNotIn("SERVER", result.stdout)


if __name__ == "__main__":
    unittest.main()
