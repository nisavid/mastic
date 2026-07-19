import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mastic.application.config_schema import ApplicationTargetSettings
from mastic.application.dispatch import ApplicationError
from mastic.infrastructure.application_target_canaries import (
    NativeApplicationTargetCanary,
    _bounded_file_command,
    application_canary_evidence_sha256,
)
from mastic.infrastructure.application_target_integrations import (
    ApplicationTargetConfiguration,
)


class NativeApplicationTargetCanaryTests(unittest.TestCase):
    def test_canary_rejects_profiles_it_does_not_natively_exercise(self) -> None:
        canary = NativeApplicationTargetCanary(Path("/unused"))

        with self.assertRaises(ApplicationError) as caught:
            canary.run(
                "hindsight",
                ApplicationTargetConfiguration("http://127.0.0.1:8766/v1", "memory"),
                _settings("hindsight", profile="project"),
                profile="consolidation",
            )

        self.assertEqual(caught.exception.code, "application_target_canary_unsupported")

    def test_codex_exec_uses_owned_config_and_exact_bounded_output(self) -> None:
        commands = []

        def run(command, **kwargs):
            commands.append((list(command), kwargs))
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text("mastic gateway contract ok\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            canary = NativeApplicationTargetCanary(
                home,
                resolve_executable=lambda name: Path(f"/tools/{name}"),
                run_command=run,
                monotonic=iter((10.0, 11.25)).__next__,
            )

            with patch.dict(
                os.environ,
                {"MASTIC_UNRELATED_SECRET": "do-not-pass", "PYTHONPATH": "/bad"},
            ):
                result = canary.run(
                    "codex",
                    ApplicationTargetConfiguration(
                        "http://127.0.0.1:8766/v1",
                        "public-route",
                        service_identity="coding-internal",
                    ),
                    _settings("codex"),
                    profile="coding",
                )

        command, kwargs = commands[0]
        self.assertEqual(
            command[:5],
            [
                "/bin/zsh",
                "-f",
                "-c",
                'ulimit -f 128; exec "$@"',
                "mastic-canary",
            ],
        )
        self.assertEqual(command[5:7], ["/tools/codex", "exec"])
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-rules", command)
        self.assertNotIn("--ask-for-approval", command)
        self.assertIn('approval_policy="never"', command)
        self.assertNotIn("--ignore-user-config", command)
        self.assertNotIn("--model", command)
        self.assertEqual(kwargs["env"]["CODEX_HOME"], str(home / ".codex"))
        self.assertNotEqual(kwargs["env"]["HOME"], str(home))
        self.assertNotIn("MASTIC_UNRELATED_SECRET", kwargs["env"])
        self.assertNotIn("PYTHONPATH", kwargs["env"])
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("capture_output", kwargs)
        self.assertEqual(result["phases"], ["codex.exec", "responses.exact"])
        self.assertTrue(result["exact_contract"])
        self.assertEqual(result["duration_seconds"], 1.25)
        self.assertEqual(
            result["evidence_sha256"],
            application_canary_evidence_sha256(
                target="codex",
                profile="coding",
                service="coding-internal",
                phases=("codex.exec", "responses.exact"),
                exact_contract=True,
            ),
        )
        self.assertNotIn("response", result)

    def test_hindsight_uses_managed_env_with_disposable_home_and_state(self) -> None:
        commands = []
        spawned = []

        class Process:
            pid = 999_999_999

            def __init__(self):
                self.polls = 0

            def poll(self):
                self.polls += 1
                return None if self.polls == 1 else 0

            def wait(self, timeout):
                return 0

        process = Process()

        def spawn(command, **kwargs):
            spawned.append((list(command), kwargs))
            return process

        def run(command, **kwargs):
            commands.append((list(command), kwargs))
            if "health" in command:
                value = "{}"
            elif "bank" in command and "create" in command:
                bank_index = command.index("bank")
                value = '{"bank_id":"' + command[bank_index + 2] + '"}'
            elif "memory" in command and "retain" in command:
                value = '{"success":true}'
            elif "memory" in command and "reflect" in command:
                value = '{"text":"mastic gateway contract ok"}'
            else:
                self.fail(f"unexpected command: {command}")
            if kwargs.get("stdout") is not subprocess.DEVNULL:
                kwargs["stdout"].write(value.encode())
                kwargs["stdout"].flush()
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            profile = home / ".hindsight" / "profiles" / "project.env"
            profile.parent.mkdir(parents=True)
            profile.write_text("HINDSIGHT_API_LLM_PROVIDER=openai\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "HINDSIGHT_AMBIENT_SECRET": "do-not-pass",
                    "MASTIC_UNRELATED_SECRET": "do-not-pass",
                    "PYTHONPATH": "/bad",
                },
            ):
                result = NativeApplicationTargetCanary(
                    home,
                    resolve_executable=lambda name: Path(f"/tools/{name}"),
                    run_command=run,
                    spawn_process=spawn,
                ).run(
                    "hindsight",
                    ApplicationTargetConfiguration(
                        "http://127.0.0.1:8766/v1", "memory"
                    ),
                    _settings("hindsight", profile="project"),
                    profile="retain",
                )

        spawn_command, spawn_kwargs = spawned[0]
        self.assertEqual(
            spawn_command[:6],
            [
                "/tools/uv",
                "run",
                "--no-project",
                "--no-config",
                "--env-file",
                str(profile),
            ],
        )
        self.assertNotIn("HINDSIGHT_AMBIENT_SECRET", spawn_kwargs["env"])
        self.assertNotIn("MASTIC_UNRELATED_SECRET", spawn_kwargs["env"])
        self.assertNotIn("PYTHONPATH", spawn_kwargs["env"])
        self.assertEqual(
            spawn_kwargs["env"]["HINDSIGHT_API_DATABASE_URL"],
            "pg0://mastic-canary",
        )
        self.assertNotEqual(spawn_kwargs["env"]["HOME"], str(home))
        self.assertEqual(
            result["phases"],
            ["hindsight.start", "bank.create", "memory.retain", "memory.reflect"],
        )
        self.assertTrue(result["exact_contract"])
        health_kwargs = commands[0][1]
        self.assertIs(health_kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(health_kwargs["stderr"], subprocess.DEVNULL)
        for _, command_kwargs in commands[1:]:
            self.assertNotIn("capture_output", command_kwargs)
            self.assertIs(command_kwargs["stderr"], subprocess.DEVNULL)

    def test_hindsight_cleans_up_exited_server_before_retrying_selected_port(
        self,
    ) -> None:
        class Process:
            def __init__(self, pid, returncode):
                self.pid = pid
                self.returncode = returncode

            def poll(self):
                return self.returncode

            def wait(self, timeout):
                return self.returncode or 0

        processes = [Process(111_111_111, 1), Process(222_222_222, None)]
        spawned = []
        terminated = []

        def spawn(command, **kwargs):
            spawned.append(list(command))
            return processes[len(spawned) - 1]

        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            profile = home / ".hindsight" / "profiles" / "project.env"
            profile.parent.mkdir(parents=True)
            profile.write_text("HINDSIGHT_API_LLM_PROVIDER=openai\n", encoding="utf-8")
            canary = NativeApplicationTargetCanary(
                home,
                resolve_executable=lambda name: Path(f"/tools/{name}"),
                run_command=lambda command, **kwargs: subprocess.CompletedProcess(
                    command, 0, "", ""
                ),
                spawn_process=spawn,
            )
            with (
                patch(
                    "mastic.infrastructure.application_target_canaries._loopback_port",
                    side_effect=(41001, 41002),
                ),
                patch.object(
                    canary,
                    "_hindsight_command",
                    side_effect=(
                        {"bank_id": "mastic-canary-fixed"},
                        {"success": True},
                        {"text": "mastic gateway contract ok"},
                    ),
                ) as command,
                patch(
                    "os.killpg",
                    side_effect=lambda pid, sent_signal: terminated.append(
                        (pid, sent_signal)
                    ),
                ),
                patch("uuid.uuid4", return_value=type("UUID", (), {"hex": "fixed"})()),
            ):
                result = canary.run(
                    "hindsight",
                    ApplicationTargetConfiguration(
                        "http://127.0.0.1:8766/v1", "memory"
                    ),
                    _settings("hindsight", profile="project"),
                    profile="retain",
                )

        self.assertEqual(len(spawned), 2)
        self.assertIn("41001", spawned[0])
        self.assertIn("41002", spawned[1])
        self.assertEqual(command.call_count, 3)
        self.assertEqual(
            terminated,
            [(111_111_111, signal.SIGTERM), (222_222_222, signal.SIGTERM)],
        )
        self.assertTrue(result["exact_contract"])

    def test_codex_rejects_an_oversized_result_file(self) -> None:
        def run(command, **kwargs):
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_bytes(b"x" * (64 * 1024 + 1))
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as raw:
            canary = NativeApplicationTargetCanary(
                Path(raw),
                resolve_executable=lambda name: Path(f"/tools/{name}"),
                run_command=run,
            )

            with self.assertRaises(ApplicationError) as caught:
                canary.run(
                    "codex",
                    ApplicationTargetConfiguration(
                        "http://127.0.0.1:8766/v1", "coding"
                    ),
                    _settings("codex"),
                    profile="coding",
                )

        self.assertEqual(caught.exception.code, "application_target_canary_failed")

    def test_canary_process_cannot_write_past_the_capture_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "oversized"
            completed = subprocess.run(
                _bounded_file_command(
                    [
                        "/bin/zsh",
                        "-f",
                        "-c",
                        'dd if=/dev/zero of="$1" bs=1024 count=128 2>/dev/null',
                        "writer",
                        str(output),
                    ]
                ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertLessEqual(output.stat().st_size, 64 * 1024)

    def test_hindsight_cleanup_accepts_an_already_exited_process_group(self) -> None:
        class Process:
            pid = 999_999_999

            def __init__(self):
                self.waited = False

            def poll(self):
                return None

            def wait(self, timeout):
                self.waited = True
                return 0

        canary = NativeApplicationTargetCanary(Path("/unused"))
        process = Process()

        with patch("os.killpg", side_effect=ProcessLookupError):
            canary._stop_process(process)

        self.assertTrue(process.waited)

    def test_hindsight_cleanup_signals_the_group_even_after_leader_exit(self) -> None:
        class Process:
            pid = 999_999_999

            def poll(self):
                return 0

            def wait(self, timeout):
                return 0

        canary = NativeApplicationTargetCanary(Path("/unused"))

        with patch("os.killpg") as kill_group:
            canary._stop_process(Process())

        kill_group.assert_called_once_with(999_999_999, 15)


def _settings(target: str, *, profile: str | None = None) -> ApplicationTargetSettings:
    return ApplicationTargetSettings(
        name=target,
        kind=target,
        service="coding",
        profile=profile,
        context_window=32768,
        provider="mastic-local",
        max_concurrent=1 if target == "hindsight" else None,
        sampling={},
    )
