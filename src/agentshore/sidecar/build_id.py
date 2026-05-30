"""Resolve the desktop sidecar ``build_id`` at runtime.

In the current pkg-installer model the sidecar always runs unfrozen
(pip-installed into a managed venv), so ``sys._MEIPASS`` is never set and
``load_build_info()`` returns the ``"dev"`` sentinel. The Rust supervisor's
``resolve_build_id()`` falls back to the same sentinel, so the handshake
build-ids match by construction.

The frozen-bundle code path (``sys._MEIPASS`` branch) is retained so the
``build_id`` mechanism can be re-introduced for a future self-contained
``.app`` distribution variant without rewriting this module.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TypedDict


class BuildInfo(TypedDict):
    build_id: str
    git_sha: str
    built_at: str


_DEV_BUILD: BuildInfo = {"build_id": "dev", "git_sha": "dev", "built_at": "dev"}


def _bundle_root() -> Path | None:
    """Return PyInstaller bundle root if frozen, else ``None``."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass is None:
        return None
    return Path(meipass)


def load_build_info() -> BuildInfo:
    """Load embedded build info, or return the development sentinel."""
    root = _bundle_root()
    if root is None:
        return _DEV_BUILD
    payload = root / "build_id.json"
    if not payload.is_file():
        return _DEV_BUILD
    try:
        raw = json.loads(payload.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _DEV_BUILD
    if not isinstance(raw, dict):
        return _DEV_BUILD
    build_id = raw.get("build_id")
    git_sha = raw.get("git_sha")
    built_at = raw.get("built_at")
    if not (isinstance(build_id, str) and isinstance(git_sha, str) and isinstance(built_at, str)):
        return _DEV_BUILD
    return {"build_id": build_id, "git_sha": git_sha, "built_at": built_at}
