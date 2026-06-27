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
- **Disk pressure** (``reap_for_disk_pressure``): when free disk drops below
  a high-water mark, reap idle worktrees LRU (``stale`` first, then oldest
  ``active``), skipping any with a live dispatch. The build-agnostic governor
  that caps the worktree fleet's footprint when the host fills (#180).

All paths funnel through ``_reap_one``, which frees the on-disk bytes *before*
journaling the transition so a full disk can't wedge the reaper on the very op
meant to free space.
"""

from __future__ import annotations

import os
import shutil
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
    list_active,
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
    """Remove a worktree on disk + transition the row to ``reaped``.

    **ENOSPC-robust ordering:** free the on-disk bytes *first*, then journal
    the transition. The old order wrote ``status='reaping'`` to SQLite before
    removing anything; on a full disk that first DB write raised → the reaper
    bailed having freed nothing, so it could never dig the host out of an
    ENOSPC hole (``worktree_reap_failed`` ×N with no space reclaimed).
    ``remove_worktree`` needs no free space (it ``rmtree``s first), so doing it
    up front gives SQLite room to record the ``reaped`` transition afterward.
    """
    path = Path(row.worktree_path)
    try:
        ok = await remove_worktree(main_repo=main_repo, worktree_path=path, force=True)
        if ok:
            log.info("worktree_reap_freed_before_journal", worktree_id=row.worktree_id)
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
        # Drive the row to a terminal status so it leaves the
        # (session_id, branch_name) partial unique index (which covers only
        # 'active'/'reaping'). Left 'active' it collides on every later
        # reap or allocate for the same pair — the recurring
        # "UNIQUE constraint failed: worktrees.session_id, branch_name" (#32).
        # 'failed' is outside the index, so this also resolves a duplicate-key
        # collision that may itself have raised here. Never let cleanup raise.
        try:
            await mark_status(
                store,
                worktree_id=row.worktree_id,
                status="failed",
                failure_reason=f"reap_exception: {exc}",
            )
        except Exception as mark_exc:
            log.warning(
                "worktree_reap_status_update_failed",
                worktree_id=row.worktree_id,
                error=str(mark_exc),
            )
        report.failed.append(
            OrphanRecord(
                worktree_id=row.worktree_id,
                worktree_path=str(path),
                reason=f"exception: {exc}",
            )
        )


async def strip_non_origin_remotes(main_repo: Path) -> list[str]:
    """Remove every git remote whose name is not exactly ``origin``.

    Self-heals stray fork remotes (e.g. a ``fork`` remote left behind by an
    agent) so an unintended remote can't be used in subsequent plays.

    Best-effort: git failures are logged but never raised — mirrors the
    ``check=False`` style used throughout this module.

    Returns the list of remote names that were removed (empty when none or on
    git failure).
    """
    try:
        _, stdout, _ = await _run_git("remote", cwd=main_repo, check=False)
    except WorktreeAllocationFailed as exc:
        log.warning("strip_non_origin_remotes_list_failed", repo=str(main_repo), reason=exc.reason)
        return []

    removed: list[str] = []
    for name in stdout.splitlines():
        name = name.strip()
        if not name or name == "origin":
            continue
        try:
            await _run_git("remote", "remove", name, cwd=main_repo, check=False)
            removed.append(name)
        except WorktreeAllocationFailed as exc:
            log.warning(
                "strip_non_origin_remotes_remove_failed",
                repo=str(main_repo),
                name=name,
                reason=exc.reason,
            )

    if removed:
        log.info(
            "worktree_non_origin_remotes_stripped",
            repo=str(main_repo),
            removed=removed,
        )
    return removed


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

    After both reap passes, stray non-origin remotes (e.g. a ``fork`` remote
    accidentally added by a prior agent) are removed from the main repo so they
    can't be used in the new session.
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
    # Third pass: strip any non-origin remotes from the main repo so stray
    # fork remotes introduced by prior agents don't persist into this session.
    await strip_non_origin_remotes(main_repo)
    return report


def _canon_path(path: str | Path) -> str:
    """Canonical key for comparing paths from different sources.

    ``git worktree list --porcelain`` prints forward-slash paths on Windows,
    while DB rows and ``main_repo.resolve()`` carry native separators. Without
    folding them to one form the "never touch the main checkout" and
    "skip DB-known worktrees" guards below silently miss on Windows and the
    reaper deletes the main repo and live worktrees. normcase+normpath
    converts ``/``→``\\`` and case-folds on case-insensitive filesystems.
    """
    return os.path.normcase(os.path.normpath(str(path)))


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
    main_resolved = _canon_path(main_repo.resolve())
    candidates = [p for p in listed_paths if p and _canon_path(p) != main_resolved]
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
    db_canon = {_canon_path(d) for d in db_paths}

    removed: list[str] = []
    for path in candidates:
        if _canon_path(path) in db_canon:
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
        # Return native-normalised paths so the report matches the DB/manager
        # convention rather than git's forward-slash output on Windows.
        removed.append(os.path.normpath(path))
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
    protected_ids: set[int] | None = None,
    protected_paths: set[str] | None = None,
) -> ReapReport:
    """Remove ``stale`` rows older than ``ttl_seconds`` in the current session.

    The GitHub adapter marks a PR's worktree row ``stale`` when the PR is
    merged or closed (so the next finalize can downgrade it). After
    ``ttl_seconds`` of inactivity, the disk + row are cleaned up.

    ``protected_ids`` are the ``worktree_id``s of live dispatches — a PR can
    close while its worktree is mid-play, and reclaiming it out from under the
    running play is the "worktree reclaimed mid-play" failure (#189). They are
    never reaped. ``protected_paths`` (canonicalised via :func:`_canon_path`)
    adds the dup-path-alias defense: any ``stale`` row whose canonical path
    matches a live path is skipped EVEN IF its ``worktree_id`` differs — the
    ``pickup-<N>`` directory is reused across attempts, so a stale OLD-id row
    can share a path with the LIVE new-id row mid-play; reaping it would
    ``git worktree remove --force`` the live directory (#203). Mirrors
    :func:`reap_for_disk_pressure`'s id+path protection so the manager need not
    fork this loop.
    """
    if ttl_seconds < 0:
        msg = f"ttl_seconds must be >= 0, got {ttl_seconds}"
        raise ValueError(msg)
    protected = protected_ids or set()
    protected_path_set = protected_paths or set()
    cutoff = datetime.now(UTC) - timedelta(seconds=ttl_seconds)
    stale = await list_stale(store, session_id=session_id, before_iso=cutoff.isoformat())
    report = ReapReport()
    for row in stale:
        if row.worktree_id in protected:
            log.info(
                "worktree_closed_pr_reap_skipped_in_flight",
                worktree_id=row.worktree_id,
                branch=row.branch_name,
                reason="id_in_flight",
            )
            continue
        if _canon_path(row.worktree_path) in protected_path_set:
            log.info(
                "worktree_closed_pr_reap_skipped_in_flight",
                worktree_id=row.worktree_id,
                branch=row.branch_name,
                path=row.worktree_path,
                reason="path_in_flight",
            )
            continue
        await _reap_one(
            store,
            row=row,
            main_repo=main_repo,
            reason="closed_pr_ttl",
            report=report,
        )
    return report


def free_disk_mb(path: Path) -> int:
    """Free space (MiB) on the filesystem backing ``path``.

    Walks up to the nearest existing ancestor so a not-yet-created worktree
    root still reports its eventual filesystem's free space. Build-agnostic —
    measures bytes, not what produced them.
    """
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free // (1024 * 1024)


async def reap_for_disk_pressure(
    store: DataStore,
    *,
    session_id: str,
    main_repo: Path,
    worktree_root: Path,
    target_free_mb: int,
    protected_ids: set[int] | None = None,
    protected_paths: set[str] | None = None,
) -> ReapReport:
    """Reap idle worktrees LRU until free disk reaches ``target_free_mb``.

    The build-agnostic disk governor (#180). AgentShore can't dictate what
    agents build inside a worktree, but when the host fills it caps its own
    fleet's footprint by reclaiming the least-recently-used idle worktrees —
    ``stale`` rows first, then the oldest ``active`` ones — until free disk is
    back above target or nothing reclaimable remains. Worktrees with a live
    dispatch (``protected_ids``) are never touched. Reuses the ENOSPC-safe
    ``_reap_one``. No-op when ``target_free_mb <= 0`` or disk is already above
    target.

    When nothing reclaimable remains and disk is still below target, that is
    the pre-dispatch guard's signal to pause (Fix 4) — surfaced via the
    ``disk_pressure_reap_exhausted`` marker.
    """
    report = ReapReport()
    if target_free_mb <= 0:
        return report
    if free_disk_mb(worktree_root) >= target_free_mb:
        return report

    protected = protected_ids or set()
    protected_path_set = protected_paths or set()
    now_iso = datetime.now(UTC).isoformat()
    # ``stale`` (including crashed-mid-reap ``reaping``) ranks ahead of warm
    # ``active`` worktrees; within each group, oldest ``last_used_at`` first.
    stale = await list_stale(store, session_id=session_id, before_iso=now_iso)
    active = await list_active(store, session_id=session_id)
    ordered = sorted(stale, key=lambda r: r.last_used_at) + sorted(
        active, key=lambda r: r.last_used_at
    )

    log.info(
        "disk_pressure_reap_triggered",
        free_mb=free_disk_mb(worktree_root),
        target_mb=target_free_mb,
        candidates=len(ordered),
    )
    for row in ordered:
        if free_disk_mb(worktree_root) >= target_free_mb:
            break
        if row.worktree_id in protected:
            continue
        if _canon_path(row.worktree_path) in protected_path_set:
            log.info(
                "worktree_disk_pressure_reap_skipped_in_flight",
                worktree_id=row.worktree_id,
                branch=row.branch_name,
                path=row.worktree_path,
                reason="path_in_flight",
            )
            continue
        await _reap_one(
            store,
            row=row,
            main_repo=main_repo,
            reason="disk_pressure",
            report=report,
        )

    free_after = free_disk_mb(worktree_root)
    if free_after < target_free_mb:
        log.warning(
            "disk_pressure_reap_exhausted",
            free_mb=free_after,
            target_mb=target_free_mb,
            reaped=len(report.removed),
        )
    else:
        log.info(
            "disk_pressure_reap_freed",
            reaped=len(report.removed),
            failed=len(report.failed),
            free_mb_after=free_after,
            target_mb=target_free_mb,
        )
    return report


__all__ = [
    "OrphanRecord",
    "ReapReport",
    "free_disk_mb",
    "reap_for_closed_prs",
    "reap_for_disk_pressure",
    "reap_git_orphans",
    "strip_non_origin_remotes",
    "sweep_session_start",
]
