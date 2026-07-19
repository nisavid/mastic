"""Safe per-user launchd registration for the mastic Supervisor."""

from __future__ import annotations

import os
import plistlib
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\Z")
_PID = re.compile(r"^\s*pid\s*=\s*(\d+)\s*$", re.MULTILINE)
_RUNNING = re.compile(r"^\s*state\s*=\s*running\s*$", re.MULTILINE)


class LaunchdConfigurationError(ValueError):
    """A LaunchAgent definition or filesystem target is unsafe."""


class LaunchdCommandError(RuntimeError):
    """launchctl rejected an explicit lifecycle request."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class LaunchdStatus:
    registered: bool
    running: bool
    pid: int | None = None
    detail: str | None = None


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str]) -> CommandResult: ...


class SubprocessCommandRunner:
    """Execute exact argv directly, never through a shell."""

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("launchctl command timeout must be positive")
        self._timeout_seconds = timeout_seconds

    def run(self, argv: Sequence[str]) -> CommandResult:
        completed = subprocess.run(
            tuple(argv),
            check=False,
            shell=False,
            capture_output=True,
            text=True,
            timeout=self._timeout_seconds,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class LaunchdAdapter:
    """Register masticd inactive and expose only explicit launchctl transitions."""

    def __init__(
        self,
        *,
        label: str,
        program_arguments: Sequence[str],
        plist_path: str | Path,
        runner: CommandRunner | None = None,
        uid: int | None = None,
    ) -> None:
        effective_uid = os.getuid() if uid is None else uid
        arguments = tuple(program_arguments)
        path = Path(plist_path)
        if _LABEL.fullmatch(label) is None or "." not in label or ".." in label:
            raise LaunchdConfigurationError(
                "LaunchAgent label must be a reverse-domain identifier"
            )
        if effective_uid < 0:
            raise LaunchdConfigurationError("LaunchAgent UID must be non-negative")
        if not arguments or not Path(arguments[0]).is_absolute():
            raise LaunchdConfigurationError(
                "LaunchAgent ProgramArguments must begin with an absolute executable"
            )
        if any(not argument or "\x00" in argument for argument in arguments):
            raise LaunchdConfigurationError(
                "LaunchAgent ProgramArguments contain an unsafe value"
            )
        if not path.is_absolute():
            raise LaunchdConfigurationError(
                "LaunchAgent plist target must be an absolute path"
            )
        if path.name != f"{label}.plist":
            raise LaunchdConfigurationError(
                "LaunchAgent plist filename must match its label"
            )
        self._label = label
        self._arguments = arguments
        self._plist_path = path
        self._runner = runner or SubprocessCommandRunner()
        self._uid = effective_uid

    @property
    def domain(self) -> str:
        return f"gui/{self._uid}"

    @property
    def target(self) -> str:
        return f"{self.domain}/{self._label}"

    def preview(self) -> bytes:
        """Return the exact inactive plist without writing or registering it."""

        return plistlib.dumps(
            {
                "KeepAlive": False,
                "Label": self._label,
                "ProcessType": "Background",
                "ProgramArguments": list(self._arguments),
                "RunAtLoad": False,
            },
            fmt=plistlib.FMT_XML,
            sort_keys=True,
        )

    def install(self) -> Path:
        """Atomically install a private user-owned plist without following links."""

        parent = self._plist_path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_stat = parent.lstat()
        if stat.S_ISLNK(parent_stat.st_mode):
            raise LaunchdConfigurationError(
                "LaunchAgent directory must not be a symbolic link"
            )
        if parent_stat.st_uid != self._uid:
            raise LaunchdConfigurationError(
                "LaunchAgent directory is not owned by the target user"
            )
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise LaunchdConfigurationError("LaunchAgent parent is not a directory")
        if stat.S_IMODE(parent_stat.st_mode) & 0o022:
            raise LaunchdConfigurationError(
                "LaunchAgent directory must not be group- or world-writable"
            )
        if self._plist_path.exists() or self._plist_path.is_symlink():
            target_stat = self._plist_path.lstat()
            if stat.S_ISLNK(target_stat.st_mode):
                raise LaunchdConfigurationError(
                    "LaunchAgent plist target is a symbolic link"
                )
            if not stat.S_ISREG(target_stat.st_mode):
                raise LaunchdConfigurationError(
                    "LaunchAgent plist target is not a regular file"
                )
            if target_stat.st_uid != self._uid:
                raise LaunchdConfigurationError(
                    "LaunchAgent plist is not owned by the target user"
                )

        descriptor, temporary_name = tempfile.mkstemp(
            dir=parent, prefix=f".{self._label}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(self.preview())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._plist_path)
            os.chmod(self._plist_path, 0o600)
            directory = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return self._plist_path

    def register(self) -> LaunchdStatus:
        """Register the LaunchAgent without activating masticd."""

        self.install()
        self._require_success(
            self._runner.run(
                ("launchctl", "bootstrap", self.domain, str(self._plist_path))
            ),
            "register",
        )
        return LaunchdStatus(registered=True, running=False)

    def kickstart(self) -> LaunchdStatus:
        """Explicitly activate the registered Supervisor."""

        self._require_success(
            self._runner.run(("launchctl", "kickstart", self.target)), "start"
        )
        return LaunchdStatus(registered=True, running=True)

    def bootout(self) -> LaunchdStatus:
        """Explicitly remove the Supervisor job from the user domain."""

        self._require_success(
            self._runner.run(("launchctl", "bootout", self.target)), "stop"
        )
        return LaunchdStatus(registered=False, running=False)

    def status(self) -> LaunchdStatus:
        """Inspect launchd without registering or activating the Supervisor."""

        result = self._runner.run(("launchctl", "print", self.target))
        if result.returncode != 0:
            return LaunchdStatus(
                registered=False,
                running=False,
                detail=(result.stderr or result.stdout).strip() or None,
            )
        match = _PID.search(result.stdout)
        return LaunchdStatus(
            registered=True,
            running=_RUNNING.search(result.stdout) is not None,
            pid=int(match.group(1)) if match else None,
            detail=result.stdout,
        )

    @staticmethod
    def _require_success(result: CommandResult, action: str) -> None:
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise LaunchdCommandError(
                f"launchctl could not {action} masticd"
                + (f": {detail}" if detail else "")
            )
