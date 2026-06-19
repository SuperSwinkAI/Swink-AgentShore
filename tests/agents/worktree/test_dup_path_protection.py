"""Tests for the #203 dup-path alias fix.

Root cause: the ``pickup-<N>`` directory is reused across attempts, so many
distinct ``worktree_id`` rows can share one on-disk path. In-flight protection
was ``worktree_id``-keyed while the reaper removes by ``worktree_path``, so the
closed-PR TTL reaper would reap a stale OLD-id row at a path and
``git worktree remove --force`` the directory a LIVE new-id row was using.

Covers:

- A stale OLD-id row sharing a LIVE in-flight path is NOT reaped (id differs,
  path protected) — both the manager wrapper and the bare reaper function.
- An unprotected stale row IS still reaped.
- Per-attempt allocation keys produce distinct on-disk paths once the canonical
  path is held by a live row (prebranch-key reuse preserved otherwise).
- ``_canon_path`` matching is separator/case-correct.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentshore.agents.worktree.reaper import (
    _canon_path,
    reap_for_closed_prs,
)
from agentshore.agents.worktree.registry import (
    insert_worktree,
    lookup_by_id,
)
from agentshore.data.store import DataStore


def _git(*args: str, cwd: Path | None = None) -> str:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "AgentShore Test")
    env.setdefault("GIT_AUTHOR_EMAIL", "test@agentshore.example")
    env.setdefault("GIT_COMMITTER_NAME", "AgentShore Test")
    env.setdefault("GIT_COMMITTER_EMAIL", "test@agentshore.example")
    env.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
    env.setdefault("GIT_CONFIG_SYSTEM", "/dev/null")
    return subprocess.check_output(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        text=True,
        stderr=subprocess.STDOUT,
    )


async def _insert_row_at(
    store: DataStore,
    *,
    session_id: str,
    pre_branch_key: str,
    worktree_path: str,
    status: str,
    last_used_at: str | None = None,
) -> int:
    """Insert a worktree row pointing at ``worktree_path`` (no git side effect).

    Lets a test seed two DB rows that *share* one on-disk path — the alias
    class behind #203 — which ``_seed_worktree_row`` can't, since it runs
    ``git worktree add`` per call.
    """
    row = await insert_worktree(
        store,
        session_id=session_id,
        branch_name=None,
        pre_branch_key=pre_branch_key,
        worktree_path=worktree_path,
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
        status=status,  # type: ignore[arg-type]
    )
    if last_used_at is not None:
        await store._conn.execute(
            "UPDATE worktrees SET last_used_at = ? WHERE worktree_id = ?",
            (last_used_at, row.worktree_id),
        )
        await store._conn.commit()
    return row.worktree_id


# --- bare reaper: path-aware protection --------------------------------------


async def test_reap_closed_prs_skips_stale_row_sharing_live_path(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """A stale OLD-id row whose path matches a LIVE path is NOT reaped (#203).

    The live row has a *different* id (not in any id-protected set), so only the
    path-keyed guard can save it. The on-disk ``pickup-7`` directory must
    survive because a live new-id row is mid-play in it.
    """
    target = worktree_root / "pickup-7"
    _git("worktree", "add", "-b", "pickup-7-wt", str(target), "HEAD", cwd=main_repo)

    old_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    # Stale OLD-id row at the shared path (a prior attempt's row).
    stale_old_id = await _insert_row_at(
        store,
        session_id="sess-1",
        pre_branch_key="pickup-7-old",
        worktree_path=str(target),
        status="stale",
        last_used_at=old_ts,
    )

    # The LIVE row uses the same directory under a different id — protected via
    # its canonical path, NOT its id.
    report = await reap_for_closed_prs(
        store,
        session_id="sess-1",
        main_repo=main_repo,
        ttl_seconds=3600,
        protected_paths={_canon_path(target)},
    )

    assert report.total == 0
    row = await lookup_by_id(store, worktree_id=stale_old_id)
    assert row is not None and row.status == "stale"
    assert target.exists(), "live in-flight directory must not be removed"


async def test_reap_closed_prs_reaps_unprotected_stale_row(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """An unprotected stale row IS reaped while a path-protected one is held."""
    protected = worktree_root / "pickup-7"
    reapable = worktree_root / "pickup-9"
    _git("worktree", "add", "-b", "pickup-7-wt", str(protected), "HEAD", cwd=main_repo)
    _git("worktree", "add", "-b", "pickup-9-wt", str(reapable), "HEAD", cwd=main_repo)

    old_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    protected_id = await _insert_row_at(
        store,
        session_id="sess-1",
        pre_branch_key="pickup-7-old",
        worktree_path=str(protected),
        status="stale",
        last_used_at=old_ts,
    )
    reapable_id = await _insert_row_at(
        store,
        session_id="sess-1",
        pre_branch_key="pickup-9",
        worktree_path=str(reapable),
        status="stale",
        last_used_at=old_ts,
    )

    report = await reap_for_closed_prs(
        store,
        session_id="sess-1",
        main_repo=main_repo,
        ttl_seconds=3600,
        protected_paths={_canon_path(protected)},
    )

    assert report.total == 1
    assert report.removed[0].worktree_id == reapable_id
    assert not reapable.exists()

    protected_row = await lookup_by_id(store, worktree_id=protected_id)
    assert protected_row is not None and protected_row.status == "stale"
    assert protected.exists()


# --- manager wrapper: id differs, path protected -----------------------------


async def test_manager_reap_closed_prs_skips_dup_path_alias(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """Manager-level: stale OLD-id row sharing a LIVE path is held back (#203).

    ``protected_ids`` does NOT contain the stale row's id (it's an old attempt),
    yet ``protected_paths`` does — the manager must skip it anyway.
    """
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.config import RuntimeConfig

    target = worktree_root / "pickup-7"
    _git("worktree", "add", "-b", "pickup-7-wt", str(target), "HEAD", cwd=main_repo)

    old_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    stale_old_id = await _insert_row_at(
        store,
        session_id="sess-1",
        pre_branch_key="pickup-7-old",
        worktree_path=str(target),
        status="stale",
        last_used_at=old_ts,
    )

    wm = WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=RuntimeConfig(),
    )
    # A LIVE new-id dispatch shares this path; only the path protects it.
    report = await wm.reap_closed_prs(
        ttl_seconds=3600,
        protected_ids={9999},  # arbitrary live id, NOT the stale row
        protected_paths={_canon_path(target)},
    )

    assert report.total == 0
    row = await lookup_by_id(store, worktree_id=stale_old_id)
    assert row is not None and row.status == "stale"
    assert target.exists()


# --- per-attempt unique paths ------------------------------------------------


async def test_per_attempt_keys_produce_distinct_paths(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """A second pickup allocation at an occupied canonical path gets a unique dir.

    The first ISSUE_PICKUP allocation for issue 7 lands at ``pickup-7``. A live
    row holds it; a second fresh allocation (e.g. the old row never rekeyed) is
    routed to a unique ``pickup-7-<shortid>`` directory rather than aliasing the
    live one — killing the dup-path alias class at the source.
    """
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.config import RuntimeConfig
    from agentshore.plays.base import PlayParams
    from agentshore.state import PlayType

    wm = WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=RuntimeConfig(),
    )

    # First allocation — canonical pickup-7 path.
    alloc1 = await wm.allocate_for_dispatch(
        play_type=PlayType.ISSUE_PICKUP,
        params=PlayParams(issue_number=7),
    )
    assert alloc1.path.name == "pickup-7"  # type: ignore[union-attr]

    # Force the prebranch-key reuse lookup to MISS so a fresh insert runs while
    # the live row still holds the canonical path: rekey-away the pre_branch_key
    # so ``lookup_by_prebranch_key`` returns None, but the row stays active at
    # the pickup-7 path (the exact OLD/NEW alias hazard).
    await store._conn.execute(
        "UPDATE worktrees SET pre_branch_key = 'pickup-7-resolved' WHERE worktree_id = ?",
        (alloc1.worktree_id,),  # type: ignore[union-attr]
    )
    await store._conn.commit()

    alloc2 = await wm.allocate_for_dispatch(
        play_type=PlayType.ISSUE_PICKUP,
        params=PlayParams(issue_number=7),
    )

    assert alloc2.path != alloc1.path  # type: ignore[union-attr]
    assert alloc2.path.name.startswith("pickup-7-")  # type: ignore[union-attr]
    assert _canon_path(alloc2.path) != _canon_path(alloc1.path)  # type: ignore[union-attr]


async def test_prebranch_key_reuse_preserved(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """A repeat pickup for the same issue REUSES the existing row (resumability).

    The uniquification must only kick in on a fresh insert against an occupied
    path; the normal same-key, same-attempt path must still share one worktree.
    """
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.config import RuntimeConfig
    from agentshore.plays.base import PlayParams
    from agentshore.state import PlayType

    wm = WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=RuntimeConfig(),
    )
    params = PlayParams(issue_number=7)
    alloc1 = await wm.allocate_for_dispatch(play_type=PlayType.ISSUE_PICKUP, params=params)
    alloc2 = await wm.allocate_for_dispatch(play_type=PlayType.ISSUE_PICKUP, params=params)

    assert alloc1.worktree_id == alloc2.worktree_id  # type: ignore[union-attr]
    assert alloc1.path == alloc2.path  # type: ignore[union-attr]


# --- _canon_path correctness -------------------------------------------------


def test_canon_path_separator_and_case() -> None:
    """``_canon_path`` folds separators (and case on case-insensitive FS)."""
    # Forward vs native separators collapse to the same key.
    a = _canon_path("/tmp/agentshore-worktrees/pickup-7")
    b = _canon_path(Path("/tmp/agentshore-worktrees/pickup-7"))
    assert a == b

    # normpath collapses redundant components.
    assert _canon_path("/tmp/wt/./pickup-7") == _canon_path("/tmp/wt/pickup-7")
    assert _canon_path("/tmp/wt/sub/../pickup-7") == _canon_path("/tmp/wt/pickup-7")

    # On a case-insensitive filesystem (normcase lowercases), the two fold
    # together; on a case-sensitive one they don't. Assert the platform-correct
    # behaviour rather than a fixed answer.
    upper = _canon_path("/tmp/WT/Pickup-7")
    lower = _canon_path("/tmp/wt/pickup-7")
    if os.path.normcase("A") == "a":
        assert upper == lower
    else:
        assert upper != lower
