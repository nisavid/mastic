"""Exact paths for a Vite+ Node Owner Runtime."""

from __future__ import annotations

import re
from pathlib import Path


_NODE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")


def vite_node_npm_path(vp_home: Path, node_version: str) -> Path:
    """Return the npm launcher bound to one exact Vite+ Node runtime."""

    if _NODE_VERSION.fullmatch(node_version) is None:
        raise ValueError("Vite+ owner runtime requires an exact Node version")
    return Path(vp_home) / "js_runtime" / "node" / node_version / "bin" / "npm"
