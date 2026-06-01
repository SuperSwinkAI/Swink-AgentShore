"""Tests for the codex-review fixes on the worktree epic (desktop-x44t).

Each test corresponds to one finding from the codex pass:

  Q1 (BROKEN) lifecycle integrity — existing-row reuse failure marks stale
  Q2 / Q5 (CONCERN) concurrency / pre-branch race — keyed asyncio.Lock
  Q3 (BROKEN) rekey atomicity — DB-fail leaves row pointing at new on-disk path
  adjacent #1 — disk worktree cleaned up when insert_worktree fails non-conflict
  adjacent #2 — closed-PR TTL reaper retries rows stuck in ``reaping``

These exercise real ``WorktreeManager`` against the same real-git fixtures used
by ``test_allocator.py`` / ``test_reaper.py``.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agentshore.agents.worktree import WorktreeManager
from agentshore.agents.worktree.registry import (
    insert_worktree,
    lookup_by_id,
    mark_status,
)
from agentshore.config import RuntimeConfig
from agentshore.data.store import DataStore
from agentshore.plays.base import PlayParams
from agentshore.state import PlayType


def _make_manager(store: DataStore, main_repo: Path, worktree_root: Path) -> WorktreeManager:
    cfg = RuntimeConfig()
    return WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=cfg,
    )


# --- Q1: lifecycle integrity ------------------------------------------------


async def test_pr_reuse_with_dirty_target_recovers_via_quarantine(
    store: DataStore, main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    """Reuse against a dirty target path heals via quarantine; row stays active.

    Pre-#570 the allocator raised ``WorktreeAllocationFailed`` on
    ``target_path_dirty`` and the manager flipped the row to ``stale`` so
    the next allocation didn't loop on a broken disk state. The new
    allocator auto-quarantines orphan dirs and proceeds, so the original
    failure mode no longer exists — the row stays ``active`` because the
    worktree was successfully rebuilt at the same path.
    """
    dirty = worktree_root / "dirty-existing"
    dirty.mkdir()
    (dirty / "file.txt").write_text("not a worktree\n")
    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name=remote_branch,
        pre_branch_key=None,
        worktree_path=str(dirty),
        original_play_type="code_review",
        base_ref=f"origin/{remote_branch}",
        head_sha=None,
    )

    wm = _make_manager(store, main_repo, worktree_root)
    params = PlayParams(branch=remote_branch)

    allocation = await wm.allocate_for_dispatch(play_type=PlayType.CODE_REVIEW, params=params)

    # Allocation succeeded; the path is now a real worktree.
    assert (Path(str(allocation.path)) / ".git").exists()  # type: ignore[attr-defined]
    refreshed = await lookup_by_id(store, worktree_id=row.worktree_id)
    assert refreshed is not None
    assert refreshed.status == "active"


async def test_branch_creating_reuse_with_dirty_target_recovers(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """Branch-creating allocation also recovers from dirty target via quarantine.

    Same invariant flip as ``test_pr_reuse_with_dirty_target_recovers_via_quarantine``:
    pre-#570 the row went stale on dirty-target failure; now the dirty dir
    is quarantined and the allocate proceeds, so the row stays ``active``.
    """
    dirty = worktree_root / "pickup-dirty"
    dirty.mkdir()
    (dirty / "file.txt").write_text("not a worktree\n")
    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name=None,
        pre_branch_key="pickup-77",
        worktree_path=str(dirty),
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
    )

    wm = _make_manager(store, main_repo, worktree_root)
    params = PlayParams(issue_number=77)
    allocation = await wm.allocate_for_dispatch(play_type=PlayType.ISSUE_PICKUP, params=params)
    assert (Path(str(allocation.path)) / ".git").exists()  # type: ignore[attr-defined]

    refreshed = await lookup_by_id(store, worktree_id=row.worktree_id)
    assert refreshed is not None
    assert refreshed.status == "active"


# --- Q2 / Q5: concurrency lock ---------------------------------------------


async def test_concurrent_pr_allocations_share_a_single_worktree(
    store: DataStore, main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    """Two concurrent dispatches against the same branch get the same row.

    Without the lock the two coroutines would both pass ``lookup_by_branch``
    while the row didn't exist, both call ``git worktree add`` (one would
    fail with target_path_dirty), and at most one ``insert_worktree`` would
    survive — leaving one allocation in a broken state.
    """
    wm = _make_manager(store, main_repo, worktree_root)
    params = PlayParams(branch=remote_branch)

    results = await asyncio.gather(
        wm.allocate_for_dispatch(play_type=PlayType.CODE_REVIEW, params=params),
        wm.allocate_for_dispatch(play_type=PlayType.UNBLOCK_PR, params=params),
    )
    a, b = results
    # Both allocations resolve to the same worktree row + path.
    assert a.worktree_id == b.worktree_id  # type: ignore[union-attr]
    assert a.path == b.path  # type: ignore[union-attr]


async def test_concurrent_branch_creating_share_prebranch_row(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """Two concurrent Issue Pickups on the same bd issue share the row."""
    wm = _make_manager(store, main_repo, worktree_root)
    params = PlayParams(issue_number=123)

    results = await asyncio.gather(
        wm.allocate_for_dispatch(play_type=PlayType.ISSUE_PICKUP, params=params),
        wm.allocate_for_dispatch(play_type=PlayType.ISSUE_PICKUP, params=params),
    )
    a, b = results
    assert a.worktree_id == b.worktree_id  # type: ignore[union-attr]
    assert a.pre_branch_key == "pickup-123"  # type: ignore[union-attr]


# --- Q3: rekey atomicity ----------------------------------------------------


async def test_rekey_db_failure_updates_worktree_path_for_reap(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """If rekey's DB write fails after the directory move, the stale row's
    worktree_path is the *new* on-disk location so the reaper finds it.

    We simulate the DB failure by patching ``rekey_row`` to raise after
    the rename has succeeded. The row should be marked stale with the
    *target* path, not the (now-missing) source path.
    """
    from agentshore.agents.worktree import rekey as rekey_mod

    # Seed: branch-creating row whose dir exists on disk.
    src_dir = worktree_root / "pickup-tmp"
    src_dir.mkdir()
    (src_dir / "marker.txt").write_text("rekey source\n")
    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name=None,
        pre_branch_key="pickup-tmp",
        worktree_path=str(src_dir),
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
    )

    async def _raise_rekey(*_a: object, **_k: object) -> None:
        raise RuntimeError("simulated DB failure")

    original = rekey_mod.rekey_row
    rekey_mod.rekey_row = _raise_rekey  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="simulated DB failure"):
            await rekey_mod.rekey_worktree(
                store,
                row=row,
                branch_name="feature/new-branch",
                worktree_root=worktree_root,
            )
    finally:
        rekey_mod.rekey_row = original  # type: ignore[assignment]

    target = worktree_root / "feature-new-branch"
    refreshed = await lookup_by_id(store, worktree_id=row.worktree_id)
    assert refreshed is not None
    assert refreshed.status == "stale"
    # The on-disk location moved; the row must reflect the new path so
    # the next reap can drop the actual worktree, not chase the gone src.
    assert refreshed.worktree_path == str(target)
    assert target.exists()
    assert not src_dir.exists()


# --- Adjacent #1: orphan disk on insert failure -----------------------------


async def test_insert_failure_cleans_up_orphan_worktree_directory(
    store: DataStore, main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    """Non-conflict insert exception triggers a disk cleanup of the new wt.

    Otherwise the dispatcher would leak an on-disk worktree that has no
    owning row and would never be reaped (sweep / TTL both look at rows).
    """
    from agentshore.agents.worktree import manager as manager_mod

    real_insert = manager_mod.insert_worktree

    async def _explode(*_a: object, **_k: object) -> Any:
        raise RuntimeError("simulated DB outage")

    wm = _make_manager(store, main_repo, worktree_root)
    params = PlayParams(branch=remote_branch)

    manager_mod.insert_worktree = _explode  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="simulated DB outage"):
            await wm.allocate_for_dispatch(play_type=PlayType.CODE_REVIEW, params=params)
    finally:
        manager_mod.insert_worktree = real_insert  # type: ignore[assignment]

    # No orphan dir on disk.
    expected = worktree_root / "feature-x"
    assert not expected.exists()


# --- Adjacent #2: TTL reaper retries 'reaping' rows -------------------------


async def test_closed_pr_reaper_retries_rows_stuck_in_reaping(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """A row stuck in 'reaping' (prior reap crashed) is retried by the TTL pass."""
    from agentshore.agents.worktree.reaper import reap_for_closed_prs

    # Materialise a worktree on disk via git so the reaper's remove succeeds.
    target = worktree_root / "stuck-reaping"
    subprocess.check_call(
        ["git", "worktree", "add", "-b", "stuck-branch", str(target), "HEAD"],
        cwd=str(main_repo),
    )

    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name="stuck-branch",
        pre_branch_key=None,
        worktree_path=str(target),
        original_play_type="code_review",
        base_ref="origin/stuck-branch",
        head_sha=None,
    )
    # Force into 'reaping' as if a prior attempt crashed mid-flight, and
    # back-date last_used_at so it's past the TTL cutoff.
    await mark_status(store, worktree_id=row.worktree_id, status="reaping")
    await store._conn.execute(
        "UPDATE worktrees SET last_used_at = ? WHERE worktree_id = ?",
        ("2020-01-01T00:00:00+00:00", row.worktree_id),
    )
    await store._conn.commit()

    report = await reap_for_closed_prs(
        store,
        session_id="sess-1",
        main_repo=main_repo,
        ttl_seconds=60,
    )
    assert report.total == 1
    refreshed = await lookup_by_id(store, worktree_id=row.worktree_id)
    assert refreshed is not None
    assert refreshed.status == "reaped"
    assert not target.exists()


# --- desktop-kdl5: _alloc_locks eviction -----------------------------------


async def test_prune_locks_drops_entries_with_no_live_row(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """``_prune_locks`` removes locks whose (scope, key) maps to no active row.

    Without eviction the dict grows unbounded over a long-lived session.
    """
    wm = _make_manager(store, main_repo, worktree_root)
    # Seed two locks by calling _get_alloc_lock directly — neither has a
    # corresponding row in the worktrees table.
    await wm._get_alloc_lock("branch", "main")
    await wm._get_alloc_lock("prebranch", "pickup-9999")
    assert len(wm._alloc_locks) == 2

    await wm._prune_locks()

    # No matching active rows → both pruned.
    assert wm._alloc_locks == {}


async def test_prune_locks_preserves_locks_for_active_rows(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """Locks whose (scope, key) matches an active row are retained."""
    # Seed an active row with branch_name="kept".
    await insert_worktree(
        store,
        session_id="sess-1",
        branch_name="kept",
        pre_branch_key=None,
        worktree_path="/tmp/agentshore-kept",
        original_play_type="code_review",
        base_ref="origin/kept",
        head_sha=None,
    )
    wm = _make_manager(store, main_repo, worktree_root)
    await wm._get_alloc_lock("branch", "kept")
    await wm._get_alloc_lock("branch", "dropped")  # no matching row

    await wm._prune_locks()

    assert "branch:kept" in wm._alloc_locks
    assert "branch:dropped" not in wm._alloc_locks


async def test_finalize_after_rekey_evicts_prebranch_lock(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """After branch-creating success + rekey, the prebranch lock is dropped.

    The prebranch key (e.g. "pickup-123") is no longer addressable once the
    row has been promoted to a real branch_name; its lock leaks otherwise.
    """
    import subprocess

    from agentshore.agents.worktree import WorktreeAllocation
    from agentshore.state import PlayOutcome, SkillResult

    # Seed: pickup row with on-disk worktree on a real branch (so the
    # rekey path can detect_branch_in_worktree → real branch name and
    # the directory move into worktree_root/<slug> succeeds).
    src = worktree_root / "pickup-tmp"
    subprocess.check_call(
        ["git", "worktree", "add", "-b", "feature/from-pickup", str(src), "HEAD"],
        cwd=str(main_repo),
    )
    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name=None,
        pre_branch_key="pickup-42",
        worktree_path=str(src),
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
    )

    wm = _make_manager(store, main_repo, worktree_root)
    # Pre-seed the prebranch lock so we can assert it's evicted later.
    await wm._get_alloc_lock("prebranch", "pickup-42")
    assert "prebranch:pickup-42" in wm._alloc_locks

    alloc = WorktreeAllocation(
        worktree_id=row.worktree_id,
        path=src,
        branch_name=None,
        pre_branch_key="pickup-42",
        play_type=PlayType.ISSUE_PICKUP,
        scope="branch_creating",
    )
    skill_result = SkillResult(success=True, branch="feature/from-pickup")
    outcome = PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id=None,
        success=True,
        partial=False,
        duration_seconds=0.0,
        token_cost=0,
        dollar_cost=0.0,
        artifacts=[],
        alignment_delta=0.0,
    )

    await wm.finalize_after_dispatch(alloc, result=skill_result, play_outcome=outcome)

    assert "prebranch:pickup-42" not in wm._alloc_locks


async def test_reap_session_start_prunes_locks(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """reap_session_start triggers _prune_locks after the sweep."""
    wm = _make_manager(store, main_repo, worktree_root)
    # Seed an orphan lock with no corresponding row.
    await wm._get_alloc_lock("branch", "orphan")
    assert "branch:orphan" in wm._alloc_locks

    await wm.reap_session_start()

    assert "branch:orphan" not in wm._alloc_locks


# --- desktop-hqht: lock-instrumented concurrency tests ----------------------


class _InstrumentedLock:
    """``asyncio.Lock`` subclass-by-composition that counts acquire/release.

    Replaces a real ``asyncio.Lock`` in ``WorktreeManager._alloc_locks`` so a
    test can assert the lock was actually held across lookup → materialize
    → insert — not just that the end state looks consistent (which would
    also pass if one coroutine threw and the other fell through to the
    WorktreeAllocationConflict re-lookup branch).
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.acquire_count = 0
        self.release_count = 0
        self.holder_history: list[str] = []
        self.concurrent_holders = 0
        self._max_concurrent = 0

    async def acquire(self) -> bool:
        await self._lock.acquire()
        self.acquire_count += 1
        self.concurrent_holders += 1
        if self.concurrent_holders > self._max_concurrent:
            self._max_concurrent = self.concurrent_holders
        return True

    def release(self) -> None:
        self.concurrent_holders -= 1
        self.release_count += 1
        self._lock.release()

    @property
    def max_concurrent_holders(self) -> int:
        return self._max_concurrent

    async def __aenter__(self) -> _InstrumentedLock:
        await self.acquire()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.release()


async def test_pr_alloc_lock_is_actually_held_across_lookup_insert(
    store: DataStore, main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    """Lock-instrumented proof that the per-branch lock serializes allocation.

    Two concurrent allocations should each take the lock exactly once and
    release it once. Critically, ``max_concurrent_holders == 1`` proves
    the second coroutine waited for the first to release rather than
    racing through the unique-index fallback path (desktop-hqht).
    """
    wm = _make_manager(store, main_repo, worktree_root)
    params = PlayParams(branch=remote_branch)

    instrumented = _InstrumentedLock()

    async def fake_get_lock(scope: str, key: str) -> _InstrumentedLock:
        # Identity: same instrumented lock for both calls (same branch).
        return instrumented

    wm._get_alloc_lock = fake_get_lock  # type: ignore[assignment]

    await asyncio.gather(
        wm.allocate_for_dispatch(play_type=PlayType.CODE_REVIEW, params=params),
        wm.allocate_for_dispatch(play_type=PlayType.UNBLOCK_PR, params=params),
    )

    # Each coroutine entered the critical section exactly once and exited it.
    assert instrumented.acquire_count == 2
    assert instrumented.release_count == 2
    # The whole point of the lock: never more than one holder at a time.
    assert instrumented.max_concurrent_holders == 1


async def test_branch_creating_lock_is_actually_held(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """Same property for the prebranch lock: serialized, single holder."""
    wm = _make_manager(store, main_repo, worktree_root)
    params = PlayParams(issue_number=321)

    instrumented = _InstrumentedLock()

    async def fake_get_lock(scope: str, key: str) -> _InstrumentedLock:
        return instrumented

    wm._get_alloc_lock = fake_get_lock  # type: ignore[assignment]

    await asyncio.gather(
        wm.allocate_for_dispatch(play_type=PlayType.ISSUE_PICKUP, params=params),
        wm.allocate_for_dispatch(play_type=PlayType.ISSUE_PICKUP, params=params),
    )

    assert instrumented.acquire_count == 2
    assert instrumented.release_count == 2
    assert instrumented.max_concurrent_holders == 1


async def test_different_branches_use_independent_locks(
    store: DataStore, main_repo: Path, worktree_root: Path, remote_branch: str
) -> None:
    """Two allocations on different branches don't block each other.

    The lock is keyed on ``(scope, key)`` — a lock per branch is correct,
    a single global lock would serialize unrelated allocations.
    """
    wm = _make_manager(store, main_repo, worktree_root)

    # Touch the lock for one branch to verify _get_alloc_lock keys it properly.
    lock_a = await wm._get_alloc_lock("branch", "feature/a")
    lock_b = await wm._get_alloc_lock("branch", "feature/b")
    assert lock_a is not lock_b

    # And the prebranch namespace is disjoint from the branch namespace.
    lock_p = await wm._get_alloc_lock("prebranch", "feature/a")
    assert lock_p is not lock_a
