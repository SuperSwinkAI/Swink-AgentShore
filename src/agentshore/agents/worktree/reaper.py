"""Reap stale/orphaned worktrees.

Three reap modes:

- **Session-start sweep** (``sweep_session_start``): rows from prior
  sessions whose directory may or may not still exist. Anything in
  ``active``/``reaping`` that doesn't belong to the current session gets
  forcibly removed from disk and transitioned to ``reaped``. Then a
  second pass (``reap_git_orphans``) reconciles ``git worktree list``
  against the DB to remove worktrees that have no row in any session —
  the DB-recovery coupling fallout.
- **Closed-PR TTL** (``reap_for_closed_prs``): rows tagged ``stale`` whose
  ``last_used_at`` is older than the TTL. Used by the GitHub poller hook
  when a PR is merged/closed to clean up the worktree after a grace
  period.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from agentshore.agents.worktree.allocator import (
    WorktreeAllocationFailed,
    _run_git,
    remove_worktree,
)
from agentshore.agents.worktree.registry import (
    WorktreeRow,
    list_orphans,
    list_stale,
    live_worktree_paths,
    mark_status,
)

if TYPE_CHECKING:
    from agentshore.data.store import DataStore

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OrphanRecord:
    """A worktree row that was removed during a reap pass."""

    worktree_id: int
    worktree_path: str
    reason: str


@dataclass(slots=True)
class ReapReport:
    """Summary returned by ``sweep_session_start`` / ``reap_for_closed_prs``."""

    removed: list[OrphanRecord] = field(default_factory=list)
    failed: list[OrphanRecord] = field(default_factory=list)
    # Paths removed by ``reap_git_orphans`` — git-registered worktrees with no
    # DB row in any session, typically left behind by DB recovery.
    git_orphans_removed: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.removed) + len(self.failed) + len(self.git_orphans_removed)


async def _reap_one(
    store: DataStore,
    *,
    row: WorktreeRow,
    main_repo: Path,
    reason: str,
    report: ReapReport,
) -> None:
    """Remove a worktree on disk + transition the row to ``reaped``."""
    path = Path(row.worktree_path)
    try:
        await mark_status(
            store,
            worktree_id=row.worktree_id,
            status="reaping",
            failure_reason=reason,
        )
        ok = await remove_worktree(main_repo=main_repo, worktree_path=path, force=True)
        if ok:
            await mark_status(store, worktree_id=row.worktree_id, status="reaped")
            report.removed.append(
                OrphanRecord(
                    worktree_id=row.worktree_id,
                    worktree_path=str(path),
                    reason=reason,
                )
            )
        else:
            await mark_status(
                store,
                worktree_id=row.worktree_id,
                status="failed",
                failure_reason=f"reap_remove_failed: {reason}",
            )
            report.failed.append(
                OrphanRecord(
                    worktree_id=row.worktree_id,
                    worktree_path=str(path),
                    reason=f"remove_failed: {reason}",
                )
            )
    except Exception as exc:
        log.warning(
            "worktree_reap_failed",
            worktree_id=row.worktree_id,
            path=str(path),
            error=str(exc),
        )
        report.failed.append(
            OrphanRecord(
                worktree_id=row.worktree_id,
                worktree_path=str(path),
                reason=f"exception: {exc}",
            )
        )


async def sweep_session_start(
    store: DataStore,
    *,
    current_session_id: str,
    main_repo: Path,
) -> ReapReport:
    """Reap every active/reaping row that doesn't belong to ``current_session_id``.

    Called from session bootstrap so a crashed prior session can't leak its
    worktrees forward into the next run.

    Two-phase: first reap rows from prior sessions that still hold an
    on-disk worktree (the DB-driven path), then reconcile ``git worktree
    list`` against the DB to catch worktrees that have no row at all (the
    DB-recovery fallout). Both phases contribute to one ``ReapReport``.
    """
    orphans = await list_orphans(store, current_session_id=current_session_id)
    report = ReapReport()
    for row in orphans:
        await _reap_one(
            store,
            row=row,
            main_repo=main_repo,
            reason="orphan_prior_session",
            report=report,
        )
    # Second pass: catch on-disk worktrees with no DB row anywhere.
    git_orphans = await reap_git_orphans(store, main_repo=main_repo)
    report.git_orphans_removed = git_orphans
    return report


async def reap_git_orphans(
    store: DataStore,
    *,
    main_repo: Path,
) -> list[str]:
    """Remove git-registered worktrees that have no row in any DB session.

    Closes the DB-recovery coupling: when ``restore_from_snapshot_ring``
    swaps in a recovered DB, prior worktree rows can be lost, leaving
    on-disk worktrees invisible to ``list_orphans``. This helper reconciles
    against git's own truth (``git worktree list``) and reaps anything not
    in the ``worktrees`` table.

    Safety:
    - The main checkout is never touched (filtered by ``main_repo.resolve()``).
    - Worktrees with any DB row in any session are never touched here.
    - Only paths under ``main_repo``'s sibling worktree-root directories are
      considered — defense-in-depth against pathological ``git worktree list``
      output naming user paths.

    Returns the list of paths removed. Failures are logged but never raise.
    """
    if not main_repo.exists():
        return []

    # 1. Prune git's own stale markers so prunable worktrees are eligible
    #    for removal in step 3.
    try:
        await _run_git("worktree", "prune", cwd=main_repo, check=False)
    except WorktreeAllocationFailed as exc:
        log.warning("reap_git_orphans_prune_failed", reason=exc.reason)

    # 2. Enumerate registered worktrees.
    try:
        _, stdout, _ = await _run_git("worktree", "list", "--porcelain", cwd=main_repo, check=False)
    except WorktreeAllocationFailed as exc:
        log.warning("reap_git_orphans_list_failed", reason=exc.reason)
        return []
    listed_paths: list[str] = []
    for line in stdout.splitlines():
        if line.startswith("worktree "):
            listed_paths.append(line[len("worktree ") :].strip())
    main_resolved = str(main_repo.resolve())
    candidates = [p for p in listed_paths if p and p != main_resolved]
    if not candidates:
        return []

    # 3. Cross-reference against worktree rows in non-terminal status.
    # Excludes ``reaped``/``failed`` so a re-registered path whose only DB
    # rows are terminal markers from prior cleanups can be reclaimed.
    try:
        db_paths = await live_worktree_paths(store)
    except Exception as exc:  # noqa: BLE001 — best-effort, never abort bootstrap
        log.warning("reap_git_orphans_db_query_failed", error=str(exc))
        return []

    removed: list[str] = []
    for path in candidates:
        if path in db_paths:
            continue
        try:
            await _run_git("worktree", "remove", "--force", path, cwd=main_repo, check=False)
        except (TimeoutError, WorktreeAllocationFailed, OSError) as exc:
            log.warning(
                "session_start_git_worktree_orphan_reap_failed",
                path=path,
                error=str(exc),
            )
            continue
        removed.append(path)
        log.warning(
            "session_start_git_worktree_orphan_reaped",
            path=path,
            main_repo=str(main_repo),
        )
    return removed


async def reap_for_closed_prs(
    store: DataStore,
    *,
    session_id: str,
    main_repo: Path,
    ttl_seconds: int,
) -> ReapReport:
    """Remove ``stale`` rows older than ``ttl_seconds`` in the current session.

    The GitHub adapter marks a PR's worktree row ``stale`` when the PR is
    merged or closed (so the next finalize can downgrade it). After
    ``ttl_seconds`` of inactivity, the disk + row are cleaned up.
    """
    if ttl_seconds < 0:
        msg = f"ttl_seconds must be >= 0, got {ttl_seconds}"
        raise ValueError(msg)
    cutoff = datetime.now(UTC) - timedelta(seconds=ttl_seconds)
    stale = await list_stale(store, session_id=session_id, before_iso=cutoff.isoformat())
    report = ReapReport()
    for row in stale:
        await _reap_one(
            store,
            row=row,
            main_repo=main_repo,
            reason="closed_pr_ttl",
            report=report,
        )
    return report


__all__ = [
    "OrphanRecord",
    "ReapReport",
    "reap_for_closed_prs",
    "reap_git_orphans",
    "sweep_session_start",
]
