import json
import tempfile
import unittest
from pathlib import Path

from mastic.infrastructure.owner_command_tracker import (
    DurableOwnerCommandTracker,
    OwnerCommandAlreadyActiveError,
    OwnerCommandInspectionError,
    OwnerCommandGroupActiveError,
    OwnerCommandProcessObservation,
    owner_command_fingerprint,
)
from mastic.infrastructure.owner_command_helper import (
    _run_guarded_owner_command,
    run_controlled_owner_command,
)
from mastic.infrastructure.state_store import (
    OperationalStateStore,
    SnapshotCompareError,
)


INSTALLATION = "application-installation:codex:vite"
ARGV = ("/opt/vite/bin/vp", "install", "@openai/codex")
CWD = Path("/private/tmp/codex-owner-stage")
LAUNCHER = ("/opt/mastic/python", "-m", "mastic.owner_helper", "7")


class ProcessInspector:
    def __init__(self, observation, *, group_active=None):
        self.observation = observation
        self._group_active = group_active

    def observe(self, pid):
        if isinstance(self.observation, Exception):
            raise self.observation
        return self.observation

    def group_active(self, process_group_id):
        if isinstance(self._group_active, Exception):
            raise self._group_active
        if self._group_active is not None:
            return self._group_active
        return isinstance(self.observation, OwnerCommandProcessObservation)


class PidInspector(ProcessInspector):
    def __init__(self, observations, *, group_active=False):
        super().__init__(None, group_active=group_active)
        self.observations = observations

    def observe(self, pid):
        return self.observations.get(pid)


def process(
    *,
    pid=4123,
    identity="birth:one",
    argv=ARGV,
    cwd=CWD,
    process_group_id=4123,
    session_id=4123,
):
    return OwnerCommandProcessObservation(
        pid=pid,
        process_identity=identity,
        argv=argv,
        cwd=cwd,
        process_group_id=process_group_id,
        session_id=session_id,
    )


def tracker(state, observation):
    return DurableOwnerCommandTracker(
        state,
        installation_identity=INSTALLATION,
        process_inspector=ProcessInspector(observation),
        sleep=lambda _seconds: None,
    )


def prepare(selected, *, pid=4123):
    return selected.record_prepared(
        pid,
        ARGV,
        cwd=CWD,
        launcher_argv=LAUNCHER,
    )


class DurableOwnerCommandTrackerTests(unittest.TestCase):
    def test_command_fingerprint_canonicalizes_working_directory_aliases(self):
        self.assertEqual(
            owner_command_fingerprint(ARGV, cwd=Path("/private/tmp/stage")),
            owner_command_fingerprint(ARGV, cwd=Path("/tmp/stage")),
        )

    def test_reopened_tracker_recognizes_the_exact_live_process(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            prepared = tracker(
                OperationalStateStore(database),
                process(argv=LAUNCHER),
            )
            marker = prepare(prepared)

            status = tracker(
                OperationalStateStore(database),
                process(),
            ).inspect(ARGV, cwd=CWD)

        self.assertEqual(marker.pid, 4123)
        self.assertEqual(status.state, "matching_live")
        self.assertTrue(status.blocks_retry)


class DurableOwnerCommandTrackerRecoveryTests(unittest.TestCase):
    def test_live_prepared_helper_blocks_a_different_stage_for_the_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = OperationalStateStore(Path(directory) / "state.sqlite3")
            selected = tracker(state, process(argv=LAUNCHER))
            prepare(selected)

            different_argv = ("/opt/vite/bin/vp", "install", "./new-stage.tgz")
            status = selected.inspect(
                different_argv,
                cwd=Path("/private/tmp/new-codex-stage"),
            )

        self.assertEqual(status.state, "prepared_live")
        self.assertTrue(status.blocks_retry)

    def test_live_old_command_denies_preparing_a_new_command_for_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = OperationalStateStore(Path(directory) / "state.sqlite3")
            prepare(tracker(state, process(argv=LAUNCHER)))
            selected = tracker(state, process())
            different_argv = (
                "/opt/vite/bin/vp",
                "install",
                "./new-stage.tgz",
            )

            status = selected.inspect(
                different_argv,
                cwd=Path("/private/tmp/new-codex-stage"),
            )

            with self.assertRaises(OwnerCommandAlreadyActiveError):
                selected.record_prepared(
                    4224,
                    different_argv,
                    cwd=Path("/private/tmp/new-codex-stage"),
                    launcher_argv=("/opt/mastic/python", "-m", "helper", "9"),
                )

        self.assertEqual(status.state, "other_command_live")
        self.assertTrue(status.blocks_retry)

    def test_dead_and_pid_reused_markers_do_not_block_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            state = OperationalStateStore(Path(directory) / "state.sqlite3")
            prepare(tracker(state, process(argv=LAUNCHER)))

            dead = tracker(state, None).inspect(ARGV, cwd=CWD)
            reused = tracker(
                state,
                process(identity="birth:two"),
            ).inspect(ARGV, cwd=CWD)

        self.assertEqual(dead.state, "stale")
        self.assertFalse(dead.blocks_retry)
        self.assertTrue(dead.requires_convergence_revalidation)
        self.assertEqual(reused.state, "process_group_reused")
        self.assertTrue(reused.blocks_retry)

    def test_completed_means_reaped_but_does_not_claim_convergence(self):
        with tempfile.TemporaryDirectory() as directory:
            state = OperationalStateStore(Path(directory) / "state.sqlite3")
            selected = tracker(state, process(argv=LAUNCHER))
            marker = prepare(selected)
            tracker(state, None).record_finished(marker)

            status = selected.inspect(ARGV, cwd=CWD)

        self.assertEqual(status.state, "completed")
        self.assertFalse(status.blocks_retry)
        self.assertTrue(status.requires_convergence_revalidation)

    def test_tampered_completed_envelope_is_unverifiable(self):
        with tempfile.TemporaryDirectory() as directory:
            state = OperationalStateStore(Path(directory) / "state.sqlite3")
            selected = tracker(state, process(argv=LAUNCHER))
            marker = prepare(selected)
            tracker(state, None).record_finished(marker)
            state.put_snapshot(
                {
                    "kind": "owner_command",
                    "id": INSTALLATION,
                    "version": "tampered:completed",
                    "command_fingerprint": marker.command_fingerprint,
                    "launcher_fingerprint": marker.launcher_fingerprint,
                    "pid": marker.pid,
                    "process_identity": marker.process_identity,
                    "process_group_id": marker.process_group_id,
                    "session_id": marker.session_id,
                    "state": "completed",
                }
            )

            status = selected.inspect(ARGV, cwd=CWD)

        self.assertEqual(status.state, "unverifiable")
        self.assertTrue(status.blocks_retry)

    def test_same_process_with_unknown_command_is_not_treated_as_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            state = OperationalStateStore(Path(directory) / "state.sqlite3")
            prepare(tracker(state, process(argv=LAUNCHER)))

            status = tracker(
                state,
                process(argv=("/opt/vite/bin/node", "child.js")),
            ).inspect(ARGV, cwd=CWD)

        self.assertEqual(status.state, "process_changed")
        self.assertTrue(status.blocks_retry)

    def test_reopened_tracker_fences_surviving_process_group_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            marker = prepare(
                tracker(
                    OperationalStateStore(database),
                    process(argv=LAUNCHER),
                )
            )
            reopened = DurableOwnerCommandTracker(
                OperationalStateStore(database),
                installation_identity=INSTALLATION,
                process_inspector=ProcessInspector(
                    None,
                    group_active=True,
                ),
            )

            status = reopened.inspect(ARGV, cwd=CWD)
            with self.assertRaises(OwnerCommandGroupActiveError):
                reopened.record_finished(marker)

        self.assertEqual(status.state, "descendants_live")
        self.assertTrue(status.blocks_retry)

    def test_unverifiable_live_process_blocks_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            state = OperationalStateStore(Path(directory) / "state.sqlite3")
            prepare(tracker(state, process(argv=LAUNCHER)))

            status = tracker(
                state,
                OwnerCommandInspectionError("permission denied"),
            ).inspect(ARGV, cwd=CWD)

        self.assertEqual(status.state, "unverifiable")
        self.assertTrue(status.blocks_retry)

    def test_durable_marker_contains_no_raw_argv_or_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            state = OperationalStateStore(Path(directory) / "state.sqlite3")
            prepare(tracker(state, process(argv=LAUNCHER)))

            snapshots = state.snapshot_history("owner_command")

        self.assertEqual(len(snapshots), 1)
        encoded = repr(snapshots[0])
        self.assertNotIn("/opt/vite/bin/vp", encoded)
        self.assertNotIn(str(CWD), encoded)

    def test_older_process_cannot_mark_a_newer_execution_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            state = OperationalStateStore(Path(directory) / "state.sqlite3")
            older_tracker = tracker(state, process(argv=LAUNCHER))
            older = prepare(older_tracker)
            newer_process = process(
                pid=4224,
                identity="birth:two",
                argv=LAUNCHER,
                process_group_id=4224,
                session_id=4224,
            )
            newer_inspector = PidInspector({4224: newer_process})
            newer_tracker = DurableOwnerCommandTracker(
                state,
                installation_identity=INSTALLATION,
                process_inspector=newer_inspector,
            )
            newer_tracker.record_prepared(
                4224,
                ARGV,
                cwd=CWD,
                launcher_argv=LAUNCHER,
            )
            newer_inspector.observations[4224] = process(
                pid=4224,
                identity="birth:two",
                argv=ARGV,
                process_group_id=4224,
                session_id=4224,
            )

            with self.assertRaises(SnapshotCompareError):
                tracker(state, None).record_finished(older)

            status = newer_tracker.inspect(ARGV, cwd=CWD)

        self.assertEqual(status.state, "matching_live")
        self.assertTrue(status.blocks_retry)


class ControlledOwnerCommandHelperTests(unittest.TestCase):
    def test_eof_before_release_exits_without_mutation(self):
        execv = unittest.mock.Mock()

        exit_code = run_controlled_owner_command(
            7,
            read=lambda _fd, _size: b"",
            execv=execv,
        )

        self.assertEqual(exit_code, 125)
        execv.assert_not_called()

    def test_partial_release_exits_without_mutation(self):
        payloads = iter((b'["/absolute/tool"', b""))
        execv = unittest.mock.Mock()

        exit_code = run_controlled_owner_command(
            7,
            read=lambda _fd, _size: next(payloads),
            execv=execv,
        )

        self.assertEqual(exit_code, 125)
        execv.assert_not_called()

    def test_complete_release_execs_the_exact_command(self):
        payloads = iter((json.dumps(ARGV).encode("utf-8"), b""))
        calls = []

        exit_code = run_controlled_owner_command(
            7,
            read=lambda _fd, _size: next(payloads),
            close=lambda fd: calls.append(("closed", fd)),
            guard=lambda argv: calls.append(("guarded", argv)) or 0,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [("closed", 7), ("guarded", list(ARGV))])

    def test_guard_launch_oserror_returns_bounded_failure(self):
        payloads = iter((json.dumps(ARGV).encode("utf-8"), b""))

        exit_code = run_controlled_owner_command(
            7,
            read=lambda _fd, _size: next(payloads),
            close=lambda _fd: None,
            guard=lambda _argv: (_ for _ in ()).throw(OSError("private detail")),
        )

        self.assertEqual(exit_code, 126)

    def test_helper_propagates_the_direct_target_exit_status(self):
        exit_code = _run_guarded_owner_command(
            list(ARGV),
            fork=lambda: 4999,
            waitpid=lambda pid, _options: (pid, 7 << 8),
        )

        self.assertEqual(exit_code, 7)


if __name__ == "__main__":
    unittest.main()
