"""Diagnostic signal extraction for the ``RECONCILE_STATE`` play.

The orchestrator pre-writes a structured ``recent_wedge_signals`` block
into ``RECONCILE_STATE``'s per-play context JSON so the skill can read
ground-truth data (git status, worktree list, recent play history)
without re-implementing the same logic in bash heuristics.

All helpers are pure / best-effort: each one swallows its own errors and
returns ``None`` / empty list rather than propagating, because a missing
diagnostic field is far less harmful than failing the dispatch itself.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from agentshore import command
from agentshore.paths import project_db_path, project_dir

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.state import OrchestratorState

_logger = structlog.get_logger(__name__)

# Untracked paths that every AgentShore session leaves in the target project's
# working tree. Filtered out of ``dirty_trunk_paths`` so reconcile_state
# doesn't try to "restore" runtime state it has no business touching. Kept
# in sync with the example-project session observation 2026-05-23 (see
# AgentShore #594 — the merge_pr skill's own dirty-trunk check needs the same
# allowlist).
_AGENTSHORE_OWNED_UNTRACKED_PREFIXES: frozenset[str] = frozenset(
    {
        ".agentshore/",
        ".beads/",
        ".agents/",
        "agentshore.yaml",
        "timelapse-runs/",
    }
)


@dataclass(frozen=True, slots=True)
class DirtyTrunkEntry:
    """A dirty-trunk entry: a tracked modification or an untracked root artifact.

    ``mtime_utc`` is the file's modification time (ISO-8601 Z), captured so the
    wedge-signal builder can decide whether an untracked root file is owned by a
    still-active trunk-scoped play (in-flight work, not a wedge — #162). ``None``
    when the file could not be stat'd.
    """

    path: str
    status: str
    mtime_utc: str | None = None


@dataclass(frozen=True, slots=True)
class RecentFailedPlay:
    """A recent failed play from the session DB."""

    play_id: int
    play_type: str | None
    agent_id: str | None
    error_excerpt: str | None
    is_timeout: bool


def _iso_to_epoch(ts: str | None) -> float | None:
    """Parse an ISO-8601 (``Z`` or offset) timestamp to epoch seconds, or None."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _path_is_agentshore_owned(path: str) -> bool:
    """True when *path* is one of the AgentShore/beads runtime sidecars."""
    for prefix in _AGENTSHORE_OWNED_UNTRACKED_PREFIXES:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            return True
    return False


def collect_dirty_trunk_paths(project_path: Path) -> list[DirtyTrunkEntry]:
    """Return tracked-file modifications on trunk, filtered for sidecars.

    Each entry carries the file path and the two-char porcelain status
    (e.g. ``" M"``, ``"MM"``, ``"??"``). Untracked paths owned by
    AgentShore/beads are excluded. Errors return an empty list — diagnosis
    falls back to the skill's own ``git status`` call.
    """
    result = command.git_sync("status", "--porcelain", cwd=project_path, timeout_seconds=10.0)
    if result.tool_missing:
        return []
    if result.returncode != 0:
        return []
    entries: list[DirtyTrunkEntry] = []
    for line in result.stdout.splitlines():
        if len(line) < 3:
            continue
        status, path = line[:2], line[3:].strip()
        if not path:
            continue
        if status == "??" and _path_is_agentshore_owned(path):
            continue
        mtime_utc: str | None = None
        try:
            st = (project_path / path).stat()
            mtime_utc = (
                datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat().replace("+00:00", "Z")
            )
        except OSError:
            pass
        entries.append(
            DirtyTrunkEntry(path=path, status=status.strip() or status, mtime_utc=mtime_utc)
        )
    return entries


def collect_orphan_worktree_paths(
    project_path: Path,
    *,
    db_path: Path | None = None,
    session_id: str | None = None,
) -> list[str]:
    """Return git-registered worktree paths with no active row in this session.

    Cross-references ``git worktree list --porcelain`` against the
    ``worktrees`` table filtered to ``status='active'`` for the current
    session. The main checkout itself is excluded. Errors return an
    empty list.
    """
    result = command.git_sync(
        "worktree", "list", "--porcelain", cwd=project_path, timeout_seconds=10.0
    )
    if result.tool_missing:
        return []
    if result.returncode != 0:
        return []
    listed: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            listed.append(line[len("worktree ") :].strip())
    main_path = str(project_path.resolve())
    listed = [p for p in listed if p and p != main_path]
    if not listed:
        return []
    active: set[str] = set()
    db_path = db_path or project_db_path(project_path)
    if session_id and db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                cursor = conn.execute(
                    "SELECT worktree_path FROM worktrees "
                    "WHERE session_id = ? AND status = 'active'",
                    (session_id,),
                )
                active = {str(row[0]) for row in cursor}
            finally:
                conn.close()
        except (sqlite3.DatabaseError, OSError) as exc:
            _logger.warning("wedge_signals_db_query_failed", error=str(exc))
            return []
    return [p for p in listed if p not in active]


def collect_recent_failed_plays(
    project_path: Path,
    *,
    session_id: str | None = None,
    limit: int = 5,
    db_path: Path | None = None,
) -> list[RecentFailedPlay]:
    """Return up to *limit* recent failed plays from this session.

    Reads from the ``plays`` table. The first entry is the most-recent
    failure; downstream callers can inspect ``error_excerpt`` to subset
    to timeout-classified rows. Returns ``[]`` on any error so the
    dispatch never blocks on a stale or missing DB.
    """
    db_path = db_path or project_db_path(project_path)
    if not session_id or not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute(
                "SELECT play_id, play_type, agent_id, error "
                "FROM plays "
                "WHERE session_id = ? AND success = 0 "
                "ORDER BY play_id DESC LIMIT ?",
                (session_id, limit),
            )
            rows = cursor.fetchall()
        finally:
            conn.close()
    except (sqlite3.DatabaseError, OSError) as exc:
        _logger.warning("wedge_signals_recent_fails_query_failed", error=str(exc))
        return []
    return [
        RecentFailedPlay(
            play_id=int(row[0]),
            play_type=str(row[1]) if row[1] is not None else None,
            agent_id=str(row[2]) if row[2] is not None else None,
            error_excerpt=(str(row[3])[:200] if row[3] is not None else None),
            is_timeout=bool(row[3] and "timed out" in str(row[3])),
        )
        for row in rows
    ]


def build_recent_wedge_signals(
    state: OrchestratorState,
    project_path: Path,
    *,
    session_id: str,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Assemble the ``recent_wedge_signals`` block written into RECONCILE_STATE's context.

    Reads ground-truth git + DB state and stitches it with the
    state-derived counters (failure streak, last play type) so the
    skill can diagnose without re-deriving everything from the log.
    Each sub-call is best-effort; this assembly never raises.

    ``active_agents_in_flight`` is a list of agents that currently have an
    active in-flight play (``current_play_id`` is set on their snapshot).
    The reconcile skill MUST cross-check any zombie candidate's ``play_id``
    against this list: if the play is still active (i.e. its agent appears
    here), the process must NOT be classified as a zombie or killed.
    """
    recent_failed = collect_recent_failed_plays(
        project_path, session_id=session_id, db_path=db_path
    )
    last_failed = recent_failed[0] if recent_failed else None

    # Build the in-flight agent list for zombie cross-checking. Each entry
    # carries enough information for the skill to match against a candidate
    # PID's backing play_id.
    active_agents_in_flight = [
        {
            "agent_id": a.agent_id,
            "agent_type": a.agent_type.value,
            "current_play_id": a.current_play_id,
            "current_play_type": (a.current_play_type.value if a.current_play_type else None),
            "current_play_started_at": a.current_play_started_at,
        }
        for a in state.agents
        if a.current_play_id is not None
    ]

    # Earliest start (epoch) of each in-flight *trunk-scoped* play. An untracked
    # root file whose mtime is at/after one of these is in-flight work owned by a
    # live play, not a wedge — reconcile_state must leave it alone (#162).
    from agentshore.core.trunk_artifacts import TRUNK_SCOPED_PLAY_TYPES

    active_trunk_starts = [
        epoch
        for a in state.agents
        if a.current_play_id is not None
        and a.current_play_type in TRUNK_SCOPED_PLAY_TYPES
        and (epoch := _iso_to_epoch(a.current_play_started_at)) is not None
    ]

    dirty_payload: list[dict[str, Any]] = []
    for entry in collect_dirty_trunk_paths(project_path):
        record = asdict(entry)
        owned = False
        if entry.status == "??" and "/" not in entry.path and entry.mtime_utc:
            mtime = _iso_to_epoch(entry.mtime_utc)
            owned = mtime is not None and any(start <= mtime for start in active_trunk_starts)
        record["owned_by_active_play"] = owned
        dirty_payload.append(record)

    return {
        "same_type_failure_streak": int(state.same_type_failure_streak),
        "last_play_type": state.last_play_type.value if state.last_play_type else None,
        "last_failed_play_type": last_failed.play_type if last_failed else None,
        "last_failed_error": last_failed.error_excerpt if last_failed else None,
        "recent_failed_plays": [asdict(r) for r in recent_failed],
        "dirty_trunk_paths": dirty_payload,
        "orphan_worktree_paths": collect_orphan_worktree_paths(
            project_path, db_path=db_path, session_id=session_id
        ),
        # Cross-check list for zombie classification: agents with active plays
        # must never be classified as zombies or killed by reconcile_state.
        "active_agents_in_flight": active_agents_in_flight,
    }


SESSION_START_DIRTY_SIDECAR = "session_start_dirty.json"


def write_session_start_dirty_baseline(
    project_path: Path,
    *,
    session_id: str,
    session_start_utc: str,
) -> Path | None:
    """Snapshot pre-session dirty trunk state to ``.agentshore/session_start_dirty.json``.

    Survives DB corruption — lives outside the DB. RECONCILE_STATE reads
    this as authoritative pre-session evidence: any path here cannot be
    *this* session's WIP, so its ownership is either prior-session output
    (likely, by mtime-cluster signature) or pre-AgentShore user WIP (rare).
    Mtime clustering distinguishes the two.

    Writes the file unconditionally — empty ``modified_paths`` when trunk
    is clean — so the skill can rely on the file's existence to declare
    "baseline captured" vs "no baseline available".

    Returns the written path, or ``None`` on I/O failure / not a git repo.
    Errors are swallowed; a missing sidecar degrades RECONCILE_STATE to
    pre-sidecar log-scan behavior, not a startup failure.
    """
    agentshore_dir = project_dir(project_path)
    if not agentshore_dir.is_dir():
        return None

    entries = collect_dirty_trunk_paths(project_path)

    # Enrich each entry with mtime + size. The skill needs both for cluster
    # analysis; we capture once at session start so the skill doesn't race
    # with subsequent in-session writes.
    enriched: list[dict[str, Any]] = []
    for entry in entries:
        abs_path = project_path / entry.path
        try:
            st = abs_path.stat()
        except OSError:
            continue
        mtime_utc = datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat().replace("+00:00", "Z")
        enriched.append(
            {
                "path": entry.path,
                "status": entry.status,
                "mtime_utc": mtime_utc,
                "size_bytes": int(st.st_size),
            }
        )

    summary: dict[str, Any] = {"count": len(enriched)}
    if enriched:
        mtimes = sorted(e["mtime_utc"] for e in enriched)
        summary["oldest_mtime_utc"] = mtimes[0]
        summary["newest_mtime_utc"] = mtimes[-1]
        try:
            oldest_ts = datetime.fromisoformat(mtimes[0].replace("Z", "+00:00"))
            newest_ts = datetime.fromisoformat(mtimes[-1].replace("Z", "+00:00"))
            summary["mtime_cluster_span_seconds"] = int((newest_ts - oldest_ts).total_seconds())
            start_ts = datetime.fromisoformat(session_start_utc.replace("Z", "+00:00"))
            summary["pre_session"] = newest_ts < start_ts
        except (ValueError, TypeError):
            # Defensive — bad timestamp format shouldn't fail the dump.
            pass

    payload = {
        "session_id": session_id,
        "session_start_utc": session_start_utc,
        "modified_paths": enriched,
        "summary": summary,
    }

    dest = agentshore_dir / SESSION_START_DIRTY_SIDECAR
    try:
        dest.write_text(json.dumps(payload, indent=2, sort_keys=True))
    except OSError as exc:
        _logger.warning(
            "session_start_dirty_baseline_write_failed",
            error=str(exc),
            path=str(dest),
        )
        return None
    return dest
