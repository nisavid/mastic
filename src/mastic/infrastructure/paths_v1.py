"""Supported-v1 per-user filesystem layout."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class MasticPaths:
    config_dir: Path
    state_dir: Path
    data_dir: Path
    log_dir: Path
    coordination_dir: Path | None = None

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def state_db(self) -> Path:
        return self.state_dir / "state.sqlite3"

    @property
    def control_socket(self) -> Path:
        return self.state_dir / "masticd.sock"

    @property
    def gateway_credential(self) -> Path:
        return self.state_dir / "gateway.token"

    @property
    def planning_grant_key(self) -> Path:
        return self.state_dir / "planning-grant.key"

    @property
    def runtime_dir(self) -> Path:
        return self.data_dir / "runtimes"

    def prepare(self) -> None:
        for path in (
            self.config_dir,
            self.state_dir,
            self.data_dir,
            self.runtime_dir,
            self.log_dir,
        ):
            _prepare_private_directory(path)


def resolve_paths(
    *,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> MasticPaths:
    """Resolve paths without creating files or reading desired state."""
    env = os.environ if environment is None else environment
    user_home = Path.home() if home is None else home
    if not user_home.is_absolute():
        raise ValueError("home must be an absolute path")
    config_home = _environment_path(env, "XDG_CONFIG_HOME", user_home / ".config")
    state_home = _environment_path(env, "XDG_STATE_HOME", user_home / ".local/state")
    data_home = _environment_path(env, "XDG_DATA_HOME", user_home / ".local/share")
    return MasticPaths(
        config_dir=_environment_path(env, "MASTIC_CONFIG_DIR", config_home / "mastic"),
        state_dir=_environment_path(env, "MASTIC_STATE_DIR", state_home / "mastic"),
        data_dir=_environment_path(env, "MASTIC_DATA_DIR", data_home / "mastic"),
        log_dir=_environment_path(
            env, "MASTIC_LOG_DIR", user_home / "Library/Logs/mastic"
        ),
        coordination_dir=user_home / ".local/state/.mastic-locks",
    )


def _environment_path(env: Mapping[str, str], key: str, default: Path) -> Path:
    raw = env.get(key)
    if raw is not None and not raw:
        raise ValueError(f"{key} must be a non-empty absolute path")
    path = default if raw is None else Path(raw)
    if not path.is_absolute():
        raise ValueError(f"{key} must be an absolute path")
    return path


def _prepare_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"private mastic path is not a directory: {path}")
    if metadata.st_uid != os.getuid():
        raise PermissionError(f"private mastic path is not user-owned: {path}")
    os.chmod(path, 0o700, follow_symlinks=False)
