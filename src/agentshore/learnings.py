"""Session learnings: load, save, prune, decay, reinforce, top-k selection.

The JSON file at ``cfg.learnings.file`` (default ``.agentshore/learnings.json``)
is the single source of truth for learnings.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
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
    """Bump confidence and reset sessions_since_use for the entry whose pattern
    matches *pattern* exactly.
    """
    result: list[Learning] = []
    for e in entries:
        if e.pattern == pattern:
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


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _normalize_tokens(pattern: str) -> frozenset[str]:
    """Lowercase and split *pattern* into alphanumeric tokens for comparison."""
    return frozenset(_TOKEN_RE.findall(pattern.lower()))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard overlap of two token sets; two empty sets are identical (1.0)."""
    if not a and not b:
        return 1.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def consolidate(
    entries: list[Learning],
    overlap_threshold: float = 0.8,
) -> list[Learning]:
    """Merge near-duplicate learnings within the same category.

    Two entries are near-duplicates when their normalized token sets have a
    Jaccard overlap of at least *overlap_threshold*. Each group collapses to one
    representative — the highest-confidence entry's ``id``/``pattern`` — with
    ``confidence = max``, ``sessions_since_use = min``, the most-recent
    ``last_reinforced_play_id`` and the earliest ``created_at`` folded in.
    Deterministic (stable order, no randomness); singletons pass through
    untouched. A non-positive threshold disables consolidation.
    """
    if overlap_threshold <= 0 or len(entries) < 2:
        return entries

    tokens = [_normalize_tokens(e.pattern) for e in entries]
    consumed = [False] * len(entries)
    result: list[Learning] = []

    for i, base in enumerate(entries):
        if consumed[i]:
            continue
        consumed[i] = True
        group = [base]
        for j in range(i + 1, len(entries)):
            if consumed[j] or entries[j].category != base.category:
                continue
            if _jaccard(tokens[i], tokens[j]) >= overlap_threshold:
                group.append(entries[j])
                consumed[j] = True
        if len(group) == 1:
            result.append(base)
            continue
        rep = max(group, key=lambda e: e.confidence)
        reinforced_ids = [
            m.last_reinforced_play_id for m in group if m.last_reinforced_play_id is not None
        ]
        result.append(
            replace(
                rep,
                confidence=max(m.confidence for m in group),
                sessions_since_use=min(m.sessions_since_use for m in group),
                last_reinforced_play_id=(
                    max(reinforced_ids) if reinforced_ids else rep.last_reinforced_play_id
                ),
                created_at=min(m.created_at for m in group),
            )
        )
    return result
