"""Small formatting helpers shared across UI widgets."""

from __future__ import annotations


def truncate(value: str, limit: int) -> str:
    """Truncate ``value`` to ``limit`` chars, replacing the tail with ``…``."""
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"
