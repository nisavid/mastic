"""Resolve filesystem locations from the deployment contract."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    config_dir: Path
    state_dir: Path
    log_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"


def resolve_paths(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> RuntimePaths:
    """Return effective mlxd directories without creating them."""
    values = os.environ if environ is None else environ
    home_dir = Path.home() if home is None else home
    return RuntimePaths(
        config_dir=_directory(
            values, "MLXD_CONFIG_DIR", home_dir / ".config/mlxd", home_dir
        ),
        state_dir=_directory(
            values, "MLXD_STATE_DIR", home_dir / ".local/state/mlxd", home_dir
        ),
        log_dir=_directory(
            values, "MLXD_LOG_DIR", home_dir / "Library/Logs/mlxd", home_dir
        ),
    )


def _directory(
    values: Mapping[str, str], key: str, default: Path, home_dir: Path
) -> Path:
    value = values.get(key)
    if not value:
        return default
    if value == "~":
        return home_dir
    if value.startswith("~/"):
        return home_dir / value[2:]
    return Path(value).expanduser()
