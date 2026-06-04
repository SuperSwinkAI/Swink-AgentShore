"""Session learnings: load, save, prune, decay, reinforce, top-k selection.

The JSON file at ``cfg.learnings.file`` is the canonical store; the
``session_learnings`` SQLite table is an audit trail only.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pathlib import Path

_logger = structlog.get_logger(__name__)

_WINDOWS_REPLACE_ATTEMPTS = 50
_WINDOWS_REPLACE_RETRY_SECONDS = 0.02


@dataclass(frozen=True, slots=True)
class Learning:
    id: str
    pattern: str
    confidence: float
    sessions_since_use: int
    source_play_id: int | None
    last_reinforced_play_id: int | None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    scope: str = "project"
    category: str = "general"


def load(path: Path) -> list[Learning]:
    """Load learnings from *path*; return empty list if file absent or corrupt."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        # Handle agent-written format: {"version": N, "patterns": [...]}
        if isinstance(raw, dict):
            raw = raw.get("patterns", [])
        entries: list[Learning] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            # Accept "description" as a fallback for "pattern" (agent skill format)
            pattern = item.get("pattern") or item.get("description", "")
            entries.append(
                Learning(
                    id=item["id"],
                    pattern=str(pattern),
                    confidence=float(item.get("confidence", 0.5)),
                    sessions_since_use=int(item.get("sessions_since_use", 0)),
                    source_play_id=item.get("source_play_id"),
                    last_reinforced_play_id=item.get("last_reinforced_play_id"),
                    created_at=item.get("created_at", datetime.now(UTC).isoformat()),
                    scope=str(item.get("scope", "project")),
                    category=str(item.get("category", "general")),
                )
            )
        return entries
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        _logger.warning("learnings_load_failed", path=str(path), error=str(exc))
        return []


def save_atomic(path: Path, entries: list[Learning]) -> None:
    """Write *entries* to *path* atomically (temp + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [asdict(e) for e in entries]
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".learnings_", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        _replace_with_reader_retry(tmp_path, path)
    except (OSError, ValueError) as exc:
        _logger.warning("learnings_save_failed", path=str(path), error=str(exc))
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _replace_with_reader_retry(src: str, dst: Path) -> None:
    """Replace ``dst`` while tolerating transient Windows reader locks."""
    attempts = _WINDOWS_REPLACE_ATTEMPTS if os.name == "nt" else 1
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(_WINDOWS_REPLACE_RETRY_SECONDS)


def prune(entries: list[Learning], min_confidence: float = 0.3) -> list[Learning]:
    """Remove entries with confidence below *min_confidence*."""
    return [e for e in entries if e.confidence >= min_confidence]


def decay(
    entries: list[Learning],
    factor: float = 0.5,
    threshold_sessions: int = 5,
) -> list[Learning]:
    """Halve confidence for entries unused for *threshold_sessions* or more."""
    result: list[Learning] = []
    for e in entries:
        if e.sessions_since_use >= threshold_sessions:
            result.append(replace(e, confidence=max(0.0, e.confidence * factor)))
        else:
            result.append(e)
    return result


def reinforce(
    entries: list[Learning],
    pattern: str,
    source_play_id: int,
    bump: float = 0.1,
) -> list[Learning]:
    """Bump confidence and reset sessions_since_use for entries whose pattern
    is a substring of *pattern*.
    """
    result: list[Learning] = []
    for e in entries:
        if e.pattern in pattern:
            result.append(
                replace(
                    e,
                    confidence=min(1.0, e.confidence + bump),
                    sessions_since_use=0,
                    last_reinforced_play_id=source_play_id,
                )
            )
        else:
            result.append(e)
    return result


def top_k(entries: list[Learning], k: int = 10) -> list[Learning]:
    """Return the top *k* entries by confidence (descending)."""
    return sorted(entries, key=lambda e: e.confidence, reverse=True)[:k]
