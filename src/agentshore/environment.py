"""Neutral runtime environment detection helpers."""

from __future__ import annotations

import shutil

AGENT_BINARIES = ("claude", "codex", "grok", "grok-build", "agy")


def resolve_executable(name: str) -> str | None:
    """Return an absolute executable path from PATH, or None."""
    return shutil.which(name)


def detect_agent_binaries() -> tuple[str, ...]:
    """Return coding-agent CLI names present on PATH."""
    return tuple(name for name in AGENT_BINARIES if resolve_executable(name))
