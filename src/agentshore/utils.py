"""Shared utility functions."""

from __future__ import annotations

from datetime import UTC, datetime


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def iso_to_epoch(ts: str | None) -> float | None:
    """Parse an ISO-8601 (``Z`` or offset) timestamp to epoch seconds, or None."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None
