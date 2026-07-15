"""Supported-v1 per-user filesystem layout."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class MlxctlPaths:
    config_dir: Path
    state_dir: Path
    data_dir: Path
    log_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def state_db(self) -> Path:
        return self.state_dir / "state.sqlite3"

    @property
    def control_socket(self) -> Path:
        return self.state_dir / "mlxd.sock"

    @property
    def gateway_credential(self) -> Path:
        return self.state_dir / "gateway.token"

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
) -> MlxctlPaths:
    """Resolve paths without creating files or reading desired state."""
    env = os.environ if environment is None else environment
    user_home = Path.home() if home is None else home
    config_home = Path(env.get("XDG_CONFIG_HOME", user_home / ".config"))
    state_home = Path(env.get("XDG_STATE_HOME", user_home / ".local/state"))
    data_home = Path(env.get("XDG_DATA_HOME", user_home / ".local/share"))
    return MlxctlPaths(
        config_dir=Path(env.get("MLXCTL_CONFIG_DIR", config_home / "mlxctl")),
        state_dir=Path(env.get("MLXCTL_STATE_DIR", state_home / "mlxctl")),
        data_dir=Path(env.get("MLXCTL_DATA_DIR", data_home / "mlxctl")),
        log_dir=Path(env.get("MLXCTL_LOG_DIR", user_home / "Library/Logs/mlxctl")),
    )


def _prepare_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, mode=0o700)
        metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"private mlxctl path is not a directory: {path}")
    if metadata.st_uid != os.getuid():
        raise PermissionError(f"private mlxctl path is not user-owned: {path}")
    os.chmod(path, 0o700, follow_symlinks=False)
