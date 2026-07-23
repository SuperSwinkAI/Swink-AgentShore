"""Persistent memory helpers backed by the beads kv store."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from agentshore.beads.lock import BdError

if TYPE_CHECKING:
    from pathlib import Path


async def remember(project_path: Path, key: str, value: str) -> None:
    """Store a persistent memory under *key* in the beads kv store.

    Uses ``bd kv set``. Silently no-ops if beads is not initialised.
    """
    from agentshore.beads import bd  # noqa: PLC0415

    if not (project_path / ".beads").exists():
        return
    with contextlib.suppress(BdError):
        await bd("kv", "set", key, value, "--dolt-auto-commit=on", cwd=project_path)
