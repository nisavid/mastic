"""Installation-scoped identity fencing for detached owner commands."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol, Sequence

import psutil

from mastic.infrastructure.state_store import OperationalStateStore


_SNAPSHOT_KIND = "owner_command"
_GROUP_DRAIN_ATTEMPTS = 100
_GROUP_DRAIN_INTERVAL_SECONDS = 0.01


class OwnerCommandInspectionError(RuntimeError):
    """A live process could not be identified safely."""


class OwnerCommandAlreadyActiveError(RuntimeError):
    """An installation already has a possibly-live owner command."""


class OwnerCommandGroupActiveError(RuntimeError):
    """The tracked leader exited while its private process group remains live."""


@dataclass(frozen=True, slots=True)
class OwnerCommandProcessObservation:
    """Exact operating-system identity and invocation of one live process."""

    pid: int
    process_identity: str
    argv: tuple[str, ...]
    cwd: Path
    process_group_id: int
    session_id: int


@dataclass(frozen=True, slots=True)
class OwnerCommandMarker:
    """The durable identity returned before one owner command is released."""

    installation_identity: str
    command_fingerprint: str
    launcher_fingerprint: str
    pid: int
    process_identity: str
    process_group_id: int
    session_id: int
    prepared_version: str


OwnerCommandState = Literal[
    "absent",
    "completed",
    "prepared_live",
    "matching_live",
    "other_command_live",
    "descendants_live",
    "stale",
    "process_group_reused",
    "process_changed",
    "unverifiable",
]


@dataclass(frozen=True, slots=True)
class OwnerCommandStatus:
    """Installation-scoped retry disposition for an exact intended command.

    ``completed`` means only that the tracked process was reaped. ``stale`` means
    that its process no longer exists. Neither proves the target mutation outcome;
    callers must re-observe convergence before launching another command.
    """

    state: OwnerCommandState

    @property
    def blocks_retry(self) -> bool:
        return self.state in {
            "prepared_live",
            "matching_live",
            "other_command_live",
            "descendants_live",
            "process_group_reused",
            "process_changed",
            "unverifiable",
        }

    @property
    def requires_convergence_revalidation(self) -> bool:
        """Whether process absence must be followed by target re-observation."""

        return self.state in {"completed", "stale"}


class OwnerCommandProcessInspector(Protocol):
    def observe(self, pid: int) -> OwnerCommandProcessObservation | None: ...

    def group_active(self, process_group_id: int) -> bool: ...


class OwnerCommandTracker(Protocol):
    def record_prepared(
        self,
        pid: int,
        argv: Sequence[str],
        *,
        cwd: Path,
        launcher_argv: Sequence[str],
    ) -> OwnerCommandMarker: ...

    def record_finished(self, marker: OwnerCommandMarker) -> None: ...

    def inspect(self, argv: Sequence[str], *, cwd: Path) -> OwnerCommandStatus: ...


class PsutilOwnerCommandProcessInspector:
    """Observe a process without treating denied inspection as absence."""

    def __init__(
        self,
        *,
        process_factory: Callable[[int], psutil.Process] = psutil.Process,
        group_probe: Callable[[int, int], None] = os.killpg,
    ) -> None:
        self._process_factory = process_factory
        self._group_probe = group_probe

    def observe(self, pid: int) -> OwnerCommandProcessObservation | None:
        try:
            process = self._process_factory(pid)
            return self._observe_process(process)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return None
        except (OSError, psutil.AccessDenied) as error:
            raise OwnerCommandInspectionError(
                "owner command process identity is unavailable"
            ) from error

    def group_active(self, process_group_id: int) -> bool:
        try:
            self._group_probe(process_group_id, 0)
        except ProcessLookupError:
            return False
        except (OSError, psutil.AccessDenied, PermissionError) as error:
            raise OwnerCommandInspectionError(
                "owner command process group is unavailable"
            ) from error
        return True

    def _observe_process(
        self, process: psutil.Process
    ) -> OwnerCommandProcessObservation | None:
        with process.oneshot():
            if not process.is_running() or process.status() in {
                psutil.STATUS_DEAD,
                psutil.STATUS_ZOMBIE,
            }:
                return None
            return OwnerCommandProcessObservation(
                pid=process.pid,
                process_identity=_process_identity(process.create_time()),
                argv=tuple(process.cmdline()),
                cwd=Path(process.cwd()),
                process_group_id=os.getpgid(process.pid),
                session_id=os.getsid(process.pid),
            )


class DurableOwnerCommandTracker:
    """Fence every owner mutation for one installation across daemon restarts."""

    def __init__(
        self,
        state: OperationalStateStore,
        *,
        installation_identity: str,
        process_inspector: OwnerCommandProcessInspector | None = None,
        sleep: Callable[[float], object] = time.sleep,
    ) -> None:
        if not isinstance(state, OperationalStateStore):
            raise TypeError("owner command tracker requires operational state")
        if not isinstance(installation_identity, str) or not installation_identity:
            raise ValueError("owner command tracker requires an installation identity")
        self._state = state
        self._installation_identity = installation_identity
        self._process_inspector = (
            process_inspector or PsutilOwnerCommandProcessInspector()
        )
        self._sleep = sleep

    def record_prepared(
        self,
        pid: int,
        argv: Sequence[str],
        *,
        cwd: Path,
        launcher_argv: Sequence[str],
    ) -> OwnerCommandMarker:
        command_fingerprint = owner_command_fingerprint(argv, cwd=cwd)
        launcher_fingerprint = owner_command_fingerprint(launcher_argv, cwd=cwd)
        current = self._state.snapshot(_SNAPSHOT_KIND, self._installation_identity)
        expected_version = None if current is None else current.get("version")
        if expected_version is not None and not isinstance(
            expected_version, (str, int)
        ):
            raise OwnerCommandInspectionError("owner command marker is malformed")
        if current is not None:
            status = self._inspect_snapshot(current, command_fingerprint)
            if status.blocks_retry:
                raise OwnerCommandAlreadyActiveError(status.state)

        observed = self._process_inspector.observe(pid)
        if observed is None:
            raise OwnerCommandInspectionError(
                "owner command helper exited before its identity was retained"
            )
        if observed.pid != pid:
            raise OwnerCommandInspectionError("owner command helper PID changed")
        if observed.process_group_id != pid or observed.session_id != pid:
            raise OwnerCommandInspectionError(
                "owner command helper does not own its private process group"
            )
        if (
            owner_command_fingerprint(observed.argv, cwd=observed.cwd)
            != launcher_fingerprint
        ):
            raise OwnerCommandInspectionError(
                "owner command helper changed before it was retained"
            )
        prepared_version = f"{observed.process_identity}:prepared"
        marker = OwnerCommandMarker(
            installation_identity=self._installation_identity,
            command_fingerprint=command_fingerprint,
            launcher_fingerprint=launcher_fingerprint,
            pid=pid,
            process_identity=observed.process_identity,
            process_group_id=observed.process_group_id,
            session_id=observed.session_id,
            prepared_version=prepared_version,
        )
        self._state.compare_and_put_snapshot(
            {
                "kind": _SNAPSHOT_KIND,
                "id": self._installation_identity,
                "version": prepared_version,
                "command_fingerprint": command_fingerprint,
                "launcher_fingerprint": launcher_fingerprint,
                "pid": pid,
                "process_identity": observed.process_identity,
                "process_group_id": observed.process_group_id,
                "session_id": observed.session_id,
                "state": "prepared",
            },
            expected_current_version=expected_version,
        )
        return marker

    def record_finished(self, marker: OwnerCommandMarker) -> None:
        if marker.installation_identity != self._installation_identity:
            raise ValueError("owner command marker belongs to another installation")
        for attempt in range(_GROUP_DRAIN_ATTEMPTS):
            if not self._process_inspector.group_active(marker.process_group_id):
                break
            if attempt + 1 == _GROUP_DRAIN_ATTEMPTS:
                raise OwnerCommandGroupActiveError(
                    "owner command process group still has live members"
                )
            self._sleep(_GROUP_DRAIN_INTERVAL_SECONDS)
        self._state.compare_and_put_snapshot(
            {
                "kind": _SNAPSHOT_KIND,
                "id": self._installation_identity,
                "version": f"{marker.process_identity}:completed",
                "command_fingerprint": marker.command_fingerprint,
                "launcher_fingerprint": marker.launcher_fingerprint,
                "pid": marker.pid,
                "process_identity": marker.process_identity,
                "process_group_id": marker.process_group_id,
                "session_id": marker.session_id,
                "state": "completed",
            },
            expected_current_version=marker.prepared_version,
        )

    def inspect(self, argv: Sequence[str], *, cwd: Path) -> OwnerCommandStatus:
        command_fingerprint = owner_command_fingerprint(argv, cwd=cwd)
        snapshot = self._state.snapshot(_SNAPSHOT_KIND, self._installation_identity)
        if snapshot is None:
            return OwnerCommandStatus("absent")
        return self._inspect_snapshot(snapshot, command_fingerprint)

    def _inspect_snapshot(
        self,
        snapshot: dict[str, object],
        requested_fingerprint: str,
    ) -> OwnerCommandStatus:
        command_fingerprint = snapshot.get("command_fingerprint")
        launcher_fingerprint = snapshot.get("launcher_fingerprint")
        pid = snapshot.get("pid")
        process_identity = snapshot.get("process_identity")
        process_group_id = snapshot.get("process_group_id")
        session_id = snapshot.get("session_id")
        state = snapshot.get("state")
        if (
            snapshot.get("kind") != _SNAPSHOT_KIND
            or snapshot.get("id") != self._installation_identity
            or not _is_fingerprint(command_fingerprint)
            or not _is_fingerprint(launcher_fingerprint)
            or type(pid) is not int
            or pid <= 0
            or not isinstance(process_identity, str)
            or not process_identity
            or type(process_group_id) is not int
            or process_group_id != pid
            or type(session_id) is not int
            or session_id != pid
            or state not in {"completed", "prepared"}
            or snapshot.get("version") != f"{process_identity}:{state}"
        ):
            return OwnerCommandStatus("unverifiable")
        if state == "completed":
            return OwnerCommandStatus("completed")
        try:
            leader = self._process_inspector.observe(pid)
        except OwnerCommandInspectionError:
            return OwnerCommandStatus("unverifiable")
        if leader is None:
            try:
                return OwnerCommandStatus(
                    "descendants_live"
                    if self._process_inspector.group_active(process_group_id)
                    else "stale"
                )
            except OwnerCommandInspectionError:
                return OwnerCommandStatus("unverifiable")
        if (
            leader.process_identity != process_identity
            or leader.process_group_id != process_group_id
            or leader.session_id != session_id
        ):
            return OwnerCommandStatus("process_group_reused")
        try:
            observed_fingerprint = owner_command_fingerprint(
                leader.argv,
                cwd=leader.cwd,
            )
        except (TypeError, ValueError):
            return OwnerCommandStatus("unverifiable")
        if observed_fingerprint == launcher_fingerprint:
            return OwnerCommandStatus("prepared_live")
        if observed_fingerprint != command_fingerprint:
            return OwnerCommandStatus("process_changed")
        if requested_fingerprint == command_fingerprint:
            return OwnerCommandStatus("matching_live")
        return OwnerCommandStatus("other_command_live")


def owner_command_fingerprint(argv: Sequence[str], *, cwd: Path) -> str:
    exact_argv = tuple(argv)
    exact_cwd = Path(cwd)
    if not exact_argv or any(
        not isinstance(argument, str) or not argument or "\x00" in argument
        for argument in exact_argv
    ):
        raise ValueError("owner command arguments must be nonempty strings")
    if not Path(exact_argv[0]).is_absolute():
        raise ValueError("owner command executable must be absolute")
    if not exact_cwd.is_absolute():
        raise ValueError("owner command working directory must be absolute")
    canonical_cwd = exact_cwd.resolve(strict=False)
    encoded = json.dumps(
        {"argv": exact_argv, "cwd": str(canonical_cwd)},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _process_identity(created_at: float) -> str:
    return f"psutil-create-time:{float(created_at).hex()}"


def _is_fingerprint(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(
        character in "0123456789abcdef" for character in digest
    )
