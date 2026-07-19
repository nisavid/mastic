from __future__ import annotations

import os
import plistlib
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mastic.infrastructure.launchd import (
    CommandResult,
    LaunchdAdapter,
    LaunchdConfigurationError,
    SubprocessCommandRunner,
)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.results: list[CommandResult] = []

    def run(self, argv):
        self.calls.append(tuple(argv))
        if self.results:
            return self.results.pop(0)
        return CommandResult(0, "", "")


class LaunchdAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.plist = (
            self.root / "Library" / "LaunchAgents" / "com.nisavid.masticd.plist"
        )
        self.runner = FakeRunner()
        self.adapter = LaunchdAdapter(
            label="com.nisavid.masticd",
            program_arguments=("/Users/example/.local/bin/masticd", "serve"),
            plist_path=self.plist,
            runner=self.runner,
            uid=os.getuid(),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @patch("mastic.infrastructure.launchd.subprocess.run")
    def test_system_launchctl_commands_have_a_finite_timeout(self, run) -> None:
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")

        result = SubprocessCommandRunner().run(("launchctl", "print", "gui/501/x"))

        self.assertEqual(result.returncode, 0)
        self.assertEqual(run.call_args.kwargs["timeout"], 30.0)

    def test_preview_is_an_inactive_per_user_launch_agent(self) -> None:
        preview = plistlib.loads(self.adapter.preview())

        self.assertEqual(preview["Label"], "com.nisavid.masticd")
        self.assertEqual(
            preview["ProgramArguments"],
            ["/Users/example/.local/bin/masticd", "serve"],
        )
        self.assertFalse(preview["RunAtLoad"])
        self.assertFalse(preview["KeepAlive"])
        self.assertEqual(preview["ProcessType"], "Background")
        self.assertNotIn("Program", preview)
        self.assertNotIn("ShellPath", preview)

    def test_register_writes_private_owned_plist_and_does_not_start_service(self):
        status = self.adapter.register()

        self.assertTrue(status.registered)
        self.assertFalse(status.running)
        self.assertEqual(
            self.runner.calls,
            [("launchctl", "bootstrap", f"gui/{os.getuid()}", str(self.plist))],
        )
        self.assertEqual(stat.S_IMODE(self.plist.stat().st_mode), 0o600)
        self.assertEqual(self.plist.stat().st_uid, os.getuid())
        self.assertEqual(plistlib.loads(self.plist.read_bytes())["RunAtLoad"], False)

    def test_kickstart_bootout_and_status_use_exact_safe_targets(self) -> None:
        self.adapter.kickstart()
        self.adapter.bootout()
        self.runner.results.append(CommandResult(0, "state = running\npid = 123\n", ""))
        status = self.adapter.status()

        target = f"gui/{os.getuid()}/com.nisavid.masticd"
        self.assertEqual(
            self.runner.calls,
            [
                ("launchctl", "kickstart", target),
                ("launchctl", "bootout", target),
                ("launchctl", "print", target),
            ],
        )
        self.assertTrue(status.registered)
        self.assertTrue(status.running)
        self.assertEqual(status.pid, 123)

    def test_unregistered_status_is_observed_without_mutation(self) -> None:
        self.runner.results.append(CommandResult(113, "", "Could not find service"))

        status = self.adapter.status()

        self.assertFalse(status.registered)
        self.assertFalse(status.running)
        self.assertEqual(len(self.runner.calls), 1)

    def test_rejects_unsafe_label_argv_and_plist_targets(self) -> None:
        cases = (
            {"label": "bad/label"},
            {"label": "masticd"},
            {"program_arguments": ("masticd",)},
            {"program_arguments": ("/bin/masticd\x00oops",)},
            {"plist_path": self.root / "wrong-name.plist"},
            {"plist_path": Path("com.nisavid.masticd.plist")},
        )
        defaults = {
            "label": "com.nisavid.masticd",
            "program_arguments": ("/usr/local/bin/masticd",),
            "plist_path": self.plist,
            "runner": self.runner,
            "uid": os.getuid(),
        }
        for overrides in cases:
            with (
                self.subTest(overrides=overrides),
                self.assertRaises(LaunchdConfigurationError),
            ):
                LaunchdAdapter(**{**defaults, **overrides})

    def test_refuses_to_replace_a_symlink_or_foreign_owned_file(self) -> None:
        self.plist.parent.mkdir(parents=True)
        target = self.root / "elsewhere"
        target.write_text("do not replace", encoding="utf-8")
        self.plist.symlink_to(target)
        with self.assertRaisesRegex(LaunchdConfigurationError, "symbolic link"):
            self.adapter.install()
        self.assertEqual(target.read_text(encoding="utf-8"), "do not replace")

    def test_refuses_a_symlinked_launch_agents_directory(self) -> None:
        real_directory = self.root / "real-agents"
        real_directory.mkdir()
        self.plist.parent.parent.mkdir(parents=True)
        self.plist.parent.symlink_to(real_directory)

        with self.assertRaisesRegex(LaunchdConfigurationError, "symbolic link"):
            self.adapter.install()

    def test_refuses_cross_user_installation_before_writing(self) -> None:
        adapter = LaunchdAdapter(
            label="com.nisavid.masticd",
            program_arguments=("/usr/local/bin/masticd",),
            plist_path=self.plist,
            runner=self.runner,
            uid=os.getuid() + 1,
        )

        with self.assertRaisesRegex(LaunchdConfigurationError, "target user"):
            adapter.register()

        self.assertFalse(self.plist.parent.exists())
        self.assertEqual(self.runner.calls, [])


if __name__ == "__main__":
    unittest.main()
