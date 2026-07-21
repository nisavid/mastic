import signal
import subprocess
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from mastic.application.external_application_lifecycle import (
    OwnerUpgradeCommandError,
    OwnerUpgradeNotAttemptedError,
    OwnerUpgradeRequest,
    VerifiedArtifact,
    VerifiedArtifactClosure,
)
from mastic.domain.external_applications import InstallationObservation
from mastic.infrastructure.codex_vite_lifecycle import (
    CodexViteOwnerLifecycle,
    OwnerUpgradeExecutionError,
    SubprocessExactCommandRunner,
)
from mastic.infrastructure.owner_command_tracker import OwnerCommandGroupActiveError


NOW = datetime(2026, 7, 20, 21, 30, tzinfo=UTC)
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64
SHA512_A = "sha512:" + "a" * 128
SHA512_B = "sha512:" + "b" * 128


def observation(
    *,
    owner="vite-plus/npm-global",
    owner_installation=SHA_A,
    owner_runtime="node:24.18.0",
    release="0.144.5",
) -> InstallationObservation:
    active = "/Users/test/.vite-plus/bin/codex"
    return InstallationObservation(
        application_identity="external-application:codex",
        installation_identity="application-installation:codex:vite",
        owner_identity=owner,
        owner_installation_identity=owner_installation,
        owner_runtime_identity=owner_runtime,
        release_channel="npm:latest",
        platform="darwin",
        architecture="arm64",
        installed_release=release,
        installed_artifact_digest=SHA_B,
        active_invocation=active,
        reachable_invocations=(active,),
        observed_at=NOW,
    )


def closure() -> VerifiedArtifactClosure:
    stage = Path("/private/tmp/staged-codex")
    return VerifiedArtifactClosure(
        application_identity="external-application:codex",
        exact_release="0.144.6",
        artifacts=(
            VerifiedArtifact(
                role="primary",
                package_identity="@openai/codex",
                exact_release="0.144.6",
                coordinate="npm:@openai/codex@0.144.6",
                archive_digest=SHA512_A,
                installed_payload_digest=SHA_C,
                staged_path=stage / "codex-0.144.6.tgz",
            ),
            VerifiedArtifact(
                role="platform",
                package_identity="@openai/codex-darwin-arm64",
                exact_release="0.144.6-darwin-arm64",
                coordinate="npm:@openai/codex@0.144.6-darwin-arm64",
                archive_digest=SHA512_B,
                installed_payload_digest=SHA_D,
                staged_path=stage / "codex-0.144.6-darwin-arm64.tgz",
            ),
        ),
        staging_directory=stage,
        cache_directory=stage / "npm-cache",
    )


class Discovery:
    def __init__(self, observations):
        self.observations = iter(observations)
        self.calls = []

    def discover(self, *, selected_installation_identity, selected_release_channel):
        self.calls.append((selected_installation_identity, selected_release_channel))
        return next(self.observations)


class Verifier:
    def __init__(
        self, *, staged_error=None, staged_error_on_call=None, installed_error=None
    ):
        self.staged_error = staged_error
        self.staged_error_on_call = staged_error_on_call
        self.installed_error = installed_error
        self.calls = []

    def prepare(self, selected, owner_runtime_identity):
        self.calls.append(("prepare", selected, owner_runtime_identity))

    def verify_staged(self, selected):
        self.calls.append(("staged", selected))
        staged_call = sum(call[0] == "staged" for call in self.calls)
        if self.staged_error and (
            self.staged_error_on_call is None
            or staged_call == self.staged_error_on_call
        ):
            raise self.staged_error

    def verify_installed(self, selected, observed):
        self.calls.append(("installed", selected, observed))
        if self.installed_error:
            raise self.installed_error


class Runner:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.calls = []

    def run(self, argv, *, cwd, environment, timeout_seconds):
        self.calls.append((tuple(argv), cwd, dict(environment), timeout_seconds))
        return self.returncode


def lifecycle(
    owner="vite-plus/npm-global", *, discovery=None, verifier=None, runner=None
):
    observed = observation(owner=owner)
    discovered = discovery or Discovery(
        [observed, replace(observed, installed_release="0.144.6")]
    )
    verified = verifier or Verifier()
    commands = runner or Runner()
    selected = CodexViteOwnerLifecycle(
        vp_home=Path("/Users/test/.vite-plus"),
        discovery=discovered,
        artifact_verifier=verified,
        runner=commands,
        base_environment={
            "HOME": "/Users/test",
            "USER": "test",
            "LANG": "en_US.UTF-8",
            "NODE_OPTIONS": "--require=/private/secret.js",
            "NPM_CONFIG_REGISTRY": "https://untrusted.invalid/",
            "SECRET_TOKEN": "do-not-copy",
        },
    )
    return selected, observed, discovered, verified, commands


def request(selected, observed, artifacts):
    action = selected.preview_action(observed, artifacts)
    return OwnerUpgradeRequest(
        installation_identity=observed.installation_identity,
        release_channel=observed.release_channel,
        expected_owner_installation_identity=observed.owner_installation_identity,
        expected_owner_runtime_identity=observed.owner_runtime_identity,
        expected_state_fingerprint=observed.state_fingerprint,
        preview_fingerprint=SHA_A,
        action=action,
        artifact_closure=artifacts,
    )


class CodexViteOwnerLifecycleTests(unittest.TestCase):
    def test_preview_rejects_unsupported_platform(self):
        selected, observed, *_rest = lifecycle()

        with self.assertRaisesRegex(ValueError, "darwin/arm64"):
            selected.preview_action(
                replace(observed, platform="linux", architecture="x86_64"),
                closure(),
            )

    def test_npm_owner_previews_verified_local_closure_with_same_node(self):
        selected, observed, *_rest = lifecycle()
        artifacts = closure()

        action = selected.preview_action(observed, artifacts)

        self.assertEqual(action.action_kind, "npm-global-install-verified-closure")
        self.assertEqual(
            action.argv[:6],
            ("/Users/test/.vite-plus/bin/vp", "env", "exec", "--node", "24.18.0", "--"),
        )
        self.assertEqual(action.argv[-1], "./codex-0.144.6.tgz")
        self.assertEqual(action.cwd, artifacts.staging_directory)
        environment = dict(action.environment)
        self.assertEqual(
            environment["NPM_CONFIG_CACHE"], str(artifacts.cache_directory)
        )
        self.assertEqual(environment["NPM_CONFIG_PREFER_OFFLINE"], "true")
        self.assertNotIn("NODE_OPTIONS", environment)
        self.assertEqual(
            environment["NPM_CONFIG_REGISTRY"], "https://registry.npmjs.org/"
        )
        self.assertNotIn("SECRET_TOKEN", environment)

    def test_vite_native_owner_previews_exact_local_global_install(self):
        selected, observed, *_rest = lifecycle(owner="vite-plus/global-package")
        action = selected.preview_action(observed, closure())

        self.assertEqual(
            action.action_kind, "vite-plus-global-install-verified-closure"
        )
        self.assertEqual(
            action.argv,
            (
                "/Users/test/.vite-plus/bin/vp",
                "install",
                "--global",
                "--node",
                "24.18.0",
                "--prefer-offline",
                "./codex-0.144.6.tgz",
            ),
        )

    def test_apply_rechecks_expected_current_and_both_payloads(self):
        selected, observed, discovered, verified, commands = lifecycle()
        artifacts = closure()
        upgrade = request(selected, observed, artifacts)

        evidence = selected.apply_exact(upgrade)

        self.assertEqual(evidence.request_fingerprint, upgrade.fingerprint)
        self.assertEqual(evidence.artifact_closure_fingerprint, artifacts.fingerprint)
        self.assertTrue(evidence.verified_post_state_fingerprint.startswith("sha256:"))
        self.assertEqual(len(commands.calls), 1)
        self.assertEqual(len(discovered.calls), 2)
        self.assertEqual(
            [call[0] for call in verified.calls],
            ["prepare", "staged", "staged", "installed"],
        )

    def test_byte_verified_payload_on_changed_runtime_emits_no_execution_evidence(self):
        source = observation()
        changed_runtime = observation(release="0.144.6", owner_runtime="node:25.0.0")
        discovery = Discovery([source, changed_runtime])
        selected, observed, _d, verified, commands = lifecycle(discovery=discovery)

        with self.assertRaisesRegex(
            OwnerUpgradeExecutionError, "installed_target_unverified"
        ):
            selected.apply_exact(request(selected, observed, closure()))

        self.assertEqual(len(commands.calls), 1)
        self.assertEqual(
            [call[0] for call in verified.calls], ["prepare", "staged", "staged"]
        )

    def test_last_safe_expected_current_drift_spawns_no_command(self):
        changed = observation(owner_installation=SHA_D)
        discovery = Discovery([changed])
        selected, observed, _d, _v, commands = lifecycle(discovery=discovery)

        with self.assertRaises(OwnerUpgradeNotAttemptedError):
            selected.apply_exact(request(selected, observed, closure()))

        self.assertEqual(commands.calls, [])

    def test_tampered_action_or_invalid_version_spawns_no_command(self):
        selected, observed, _d, _v, commands = lifecycle()
        artifacts = closure()
        upgrade = request(selected, observed, artifacts)

        with self.assertRaises(OwnerUpgradeNotAttemptedError):
            selected.apply_exact(
                replace(
                    upgrade,
                    action=replace(
                        upgrade.action, argv=upgrade.action.argv + ("--force",)
                    ),
                )
            )
        self.assertEqual(commands.calls, [])
        with self.assertRaisesRegex(ValueError, "exact npm version"):
            selected.preview_action(
                observed, replace(artifacts, exact_release="latest")
            )

    def test_wrapper_only_closure_is_rejected_before_preview_or_command(self):
        selected, observed, _d, _v, commands = lifecycle()
        complete = closure()
        incomplete = replace(
            complete,
            artifacts=(complete.artifact("primary"),),
        )

        with self.assertRaisesRegex(ValueError, "wrapper and platform"):
            selected.preview_action(observed, incomplete)

        action = replace(
            selected.preview_action(observed, complete),
            artifact_closure_fingerprint=incomplete.fingerprint,
        )
        incomplete_request = OwnerUpgradeRequest(
            installation_identity=observed.installation_identity,
            release_channel=observed.release_channel,
            expected_owner_installation_identity=observed.owner_installation_identity,
            expected_owner_runtime_identity=observed.owner_runtime_identity,
            expected_state_fingerprint=observed.state_fingerprint,
            preview_fingerprint=SHA_A,
            action=action,
            artifact_closure=incomplete,
        )
        with self.assertRaises(OwnerUpgradeNotAttemptedError):
            selected.apply_exact(incomplete_request)
        self.assertEqual(commands.calls, [])

    def test_nonzero_or_post_byte_mismatch_is_content_free_failure(self):
        for runner, verifier, reason in (
            (Runner(1), Verifier(), "owner_command_failed"),
            (
                Runner(),
                Verifier(
                    installed_error=OwnerUpgradeExecutionError("payload_mismatch")
                ),
                "payload_mismatch",
            ),
        ):
            with self.subTest(reason=reason):
                selected, observed, *_ = lifecycle(runner=runner, verifier=verifier)
                with self.assertRaises(OwnerUpgradeExecutionError) as raised:
                    selected.apply_exact(request(selected, observed, closure()))
                self.assertEqual(raised.exception.reason_code, reason)
                self.assertNotIn("codex-0.144.6.tgz", repr(raised.exception))

    def test_post_command_staged_drift_is_a_typed_execution_failure(self):
        verifier = Verifier(
            staged_error=OwnerUpgradeCommandError("staged_payload_changed"),
            staged_error_on_call=2,
        )
        selected, observed, discovered, _verified, commands = lifecycle(
            verifier=verifier
        )

        with self.assertRaisesRegex(
            OwnerUpgradeExecutionError,
            "staged_artifact_changed",
        ):
            selected.apply_exact(request(selected, observed, closure()))

        self.assertEqual(len(commands.calls), 1)
        self.assertEqual(len(discovered.calls), 1)


class FakeProcess:
    pid = 4123

    def __init__(self, *, returncode=0, timeout=False):
        self.returncode = returncode
        self.timeout = timeout
        self.wait_calls = []

    def wait(self, timeout):
        self.wait_calls.append(timeout)
        if self.timeout and len(self.wait_calls) == 1:
            raise subprocess.TimeoutExpired(("private",), timeout)
        return self.returncode


class SubprocessExactCommandRunnerTests(unittest.TestCase):
    def test_runner_persists_started_and_finished_process_identity(self):
        process = FakeProcess()
        tracker = unittest.mock.Mock()
        marker = object()
        tracker.record_prepared.return_value = marker
        calls = []

        def popen(argv, **kwargs):
            calls.append((argv, kwargs))
            return process

        returncode = SubprocessExactCommandRunner(
            popen=popen,
            tracker=tracker,
        ).run(
            ("/absolute/tool", "arg"),
            cwd=Path("/private/tmp/stage"),
            environment={"PATH": "/usr/bin"},
            timeout_seconds=30,
        )

        self.assertEqual(returncode, 0)
        launcher_argv = calls[0][0]
        self.assertNotEqual(launcher_argv, ("/absolute/tool", "arg"))
        self.assertEqual(
            launcher_argv[1:3],
            ("-m", "mastic.infrastructure.owner_command_helper"),
        )
        tracker.record_prepared.assert_called_once_with(
            process.pid,
            ("/absolute/tool", "arg"),
            cwd=Path("/private/tmp/stage"),
            launcher_argv=launcher_argv,
        )
        tracker.record_finished.assert_called_once_with(marker)

    def test_tracking_failure_terminates_spawned_process_group(self):
        process = FakeProcess()
        tracker = unittest.mock.Mock()
        tracker.record_prepared.side_effect = OSError("state unavailable")

        with (
            patch("mastic.infrastructure.codex_vite_lifecycle.os.killpg") as killpg,
            self.assertRaises(OwnerUpgradeExecutionError) as raised,
        ):
            SubprocessExactCommandRunner(
                popen=lambda *_args, **_kwargs: process,
                tracker=tracker,
            ).run(
                ("/absolute/tool",),
                cwd=Path("/private/tmp/stage"),
                environment={"PATH": "/usr/bin"},
                timeout_seconds=30,
            )

        killpg.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertEqual(process.wait_calls, [5.0])
        self.assertEqual(raised.exception.reason_code, "owner_command_tracking_failed")

    def test_exact_command_is_released_only_after_durable_prepare(self):
        process = FakeProcess()
        tracker = unittest.mock.Mock()
        tracker.record_prepared.side_effect = lambda *_args, **_kwargs: (
            events.append("prepared") or object()
        )
        events = []

        def popen(*_args, **_kwargs):
            events.append("helper_spawned")
            return process

        def write(_fd, payload):
            events.append("released")
            return len(payload)

        with patch(
            "mastic.infrastructure.codex_vite_lifecycle.os.write", side_effect=write
        ):
            SubprocessExactCommandRunner(popen=popen, tracker=tracker).run(
                ("/absolute/tool",),
                cwd=Path("/private/tmp/stage"),
                environment={"PATH": "/usr/bin"},
                timeout_seconds=30,
            )

        self.assertEqual(events, ["helper_spawned", "prepared", "released"])

    def test_release_failure_terminates_prepared_helper(self):
        process = FakeProcess()
        tracker = unittest.mock.Mock()
        tracker.record_prepared.return_value = object()

        with (
            patch("mastic.infrastructure.codex_vite_lifecycle.os.write") as write,
            patch("mastic.infrastructure.codex_vite_lifecycle.os.killpg") as killpg,
            self.assertRaises(OwnerUpgradeExecutionError) as raised,
        ):
            write.side_effect = BrokenPipeError
            SubprocessExactCommandRunner(
                popen=lambda *_args, **_kwargs: process,
                tracker=tracker,
            ).run(
                ("/absolute/tool",),
                cwd=Path("/private/tmp/stage"),
                environment={"PATH": "/usr/bin"},
                timeout_seconds=30,
            )

        killpg.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertEqual(raised.exception.reason_code, "owner_command_tracking_failed")

    def test_surviving_process_group_prevents_completion(self):
        process = FakeProcess()
        tracker = unittest.mock.Mock()
        marker = object()
        tracker.record_prepared.return_value = marker
        tracker.record_finished.side_effect = OwnerCommandGroupActiveError(
            "child remains live"
        )

        with self.assertRaises(OwnerUpgradeExecutionError) as raised:
            SubprocessExactCommandRunner(
                popen=lambda *_args, **_kwargs: process,
                tracker=tracker,
            ).run(
                ("/absolute/tool",),
                cwd=Path("/private/tmp/stage"),
                environment={"PATH": "/usr/bin"},
                timeout_seconds=30,
            )

        tracker.record_finished.assert_called_once_with(marker)
        self.assertEqual(raised.exception.reason_code, "owner_command_tracking_failed")

    def test_runner_uses_no_shell_no_output_and_a_new_process_group(self):
        process = FakeProcess()
        calls = []

        def popen(argv, **kwargs):
            calls.append((argv, kwargs))
            return process

        returncode = SubprocessExactCommandRunner(popen=popen).run(
            ("/absolute/tool", "arg"),
            cwd=Path("/private/tmp/stage"),
            environment={"PATH": "/usr/bin"},
            timeout_seconds=30,
        )

        self.assertEqual(returncode, 0)
        _argv, kwargs = calls[0]
        self.assertEqual(kwargs["cwd"], Path("/private/tmp/stage"))
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["start_new_session"])
        self.assertTrue(kwargs["close_fds"])

    def test_timeout_kills_and_reaps_the_private_process_group(self):
        process = FakeProcess(timeout=True)
        runner = SubprocessExactCommandRunner(popen=lambda *_args, **_kwargs: process)

        with (
            patch("mastic.infrastructure.codex_vite_lifecycle.os.killpg") as killpg,
            self.assertRaises(OwnerUpgradeExecutionError) as raised,
        ):
            runner.run(
                ("/absolute/tool",),
                cwd=Path("/private/tmp/stage"),
                environment={"PATH": "/usr/bin"},
                timeout_seconds=0.1,
            )

        killpg.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertEqual(process.wait_calls, [0.1, 5.0])
        self.assertEqual(raised.exception.reason_code, "owner_command_timed_out")

    def test_timeout_remains_primary_when_completion_tracking_fails(self):
        process = FakeProcess(timeout=True)
        tracker = unittest.mock.Mock()
        marker = object()
        tracker.record_prepared.return_value = marker
        tracker.record_finished.side_effect = OSError("state unavailable")
        runner = SubprocessExactCommandRunner(
            popen=lambda *_args, **_kwargs: process,
            tracker=tracker,
        )

        with (
            patch("mastic.infrastructure.codex_vite_lifecycle.os.killpg"),
            self.assertRaises(OwnerUpgradeExecutionError) as raised,
        ):
            runner.run(
                ("/absolute/tool",),
                cwd=Path("/private/tmp/stage"),
                environment={"PATH": "/usr/bin"},
                timeout_seconds=0.1,
            )

        tracker.record_finished.assert_called_once_with(marker)
        self.assertEqual(raised.exception.reason_code, "owner_command_timed_out")


if __name__ == "__main__":
    unittest.main()
