"""Unit tests for the reaper.

Covers two reap modes:

- ``sweep_session_start`` — rows from prior sessions cleaned at bootstrap.
- ``reap_for_closed_prs`` — ``stale`` rows older than the TTL.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentshore.agents.worktree.reaper import (
    reap_for_closed_prs,
    sweep_session_start,
)
from agentshore.agents.worktree.registry import (
    insert_worktree,
    lookup_by_id,
    mark_status,
)
from agentshore.data.store import DataStore


def _git(*args: str, cwd: Path | None = None) -> str:
    import os

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


async def _seed_worktree_row(
    store: DataStore,
    main_repo: Path,
    worktree_root: Path,
    *,
    session_id: str,
    branch_name: str | None,
    pre_branch_key: str | None,
    dir_name: str,
    status: str = "active",
    last_used_at: str | None = None,
) -> tuple[int, Path]:
    """Create both an on-disk worktree (real git) and a row tracking it."""
    target = worktree_root / dir_name
    _git("worktree", "add", "-b", f"reap-{dir_name}", str(target), "HEAD", cwd=main_repo)
    row = await insert_worktree(
        store,
        session_id=session_id,
        branch_name=branch_name,
        pre_branch_key=pre_branch_key,
        worktree_path=str(target),
        original_play_type="code_review",
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
    return row.worktree_id, target


# --- session-start sweep ------------------------------------------------------


async def test_sweep_session_start_reaps_other_sessions(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """Rows whose session_id != current_session_id are reaped."""
    orphan_id, orphan_path = await _seed_worktree_row(
        store,
        main_repo,
        worktree_root,
        session_id="sess-other",
        branch_name="orphan-branch",
        pre_branch_key=None,
        dir_name="orphan-wt",
    )
    mine_id, mine_path = await _seed_worktree_row(
        store,
        main_repo,
        worktree_root,
        session_id="sess-1",
        branch_name="my-branch",
        pre_branch_key=None,
        dir_name="my-wt",
    )

    report = await sweep_session_start(store, current_session_id="sess-1", main_repo=main_repo)

    assert report.total == 1
    assert len(report.removed) == 1
    assert report.removed[0].worktree_id == orphan_id

    orphan_row = await lookup_by_id(store, worktree_id=orphan_id)
    assert orphan_row is not None
    assert orphan_row.status == "reaped"
    assert not orphan_path.exists()

    mine_row = await lookup_by_id(store, worktree_id=mine_id)
    assert mine_row is not None
    assert mine_row.status == "active"
    assert mine_path.exists()


async def test_reap_exception_marks_row_failed_freeing_unique_index(
    store: DataStore,
    main_repo: Path,
    worktree_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reap that raises must drive the row to terminal ``failed`` (#32).

    ``_reap_one`` first flips the row to ``reaping`` and then removes the
    worktree. If removal raises, the row used to be left in ``reaping`` —
    inside the partial unique index ``(session_id, branch_name) WHERE status IN
    ('active','reaping')`` — so every subsequent reap/allocate for the same pair
    hit ``UNIQUE constraint failed: worktrees.session_id, branch_name`` forever.
    The handler now transitions the row to ``failed`` (outside the index).
    """
    from agentshore.agents.worktree import reaper as reaper_mod
    from agentshore.agents.worktree.reaper import ReapReport, _reap_one

    wt_id, _path = await _seed_worktree_row(
        store,
        main_repo,
        worktree_root,
        session_id="sess-other",
        branch_name="boom-branch",
        pre_branch_key=None,
        dir_name="boom-wt",
    )
    row = await lookup_by_id(store, worktree_id=wt_id)
    assert row is not None

    async def _raise(**_kwargs: object) -> bool:
        raise RuntimeError("UNIQUE constraint failed: worktrees.session_id, branch_name")

    monkeypatch.setattr(reaper_mod, "remove_worktree", _raise)

    report = ReapReport()
    await _reap_one(store, row=row, main_repo=main_repo, reason="test", report=report)

    # Recorded as failed, and the row is in a terminal status (not 'reaping').
    assert len(report.failed) == 1
    persisted = await lookup_by_id(store, worktree_id=wt_id)
    assert persisted is not None
    assert persisted.status == "failed"

    # Regression: the (session_id, branch_name) partial unique index is now
    # free — a fresh allocation for the same pair no longer collides. Before the
    # fix the stuck 'reaping' row made this insert raise.
    new_row = await insert_worktree(
        store,
        session_id="sess-other",
        branch_name="boom-branch",
        pre_branch_key=None,
        worktree_path=str(worktree_root / "boom-wt-reattempt"),
        original_play_type="code_review",
        base_ref="origin/HEAD",
        head_sha=None,
    )
    assert new_row.worktree_id != wt_id


async def test_sweep_session_start_handles_missing_directory(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """Row whose on-disk directory is already gone still transitions to reaped."""
    orphan_id, orphan_path = await _seed_worktree_row(
        store,
        main_repo,
        worktree_root,
        session_id="sess-other",
        branch_name="vanished-branch",
        pre_branch_key=None,
        dir_name="vanished-wt",
    )
    # Caller bypassed git → simulate a crashed-mid-flight scenario.
    import shutil

    shutil.rmtree(orphan_path)

    report = await sweep_session_start(store, current_session_id="sess-1", main_repo=main_repo)
    assert report.total == 1
    row = await lookup_by_id(store, worktree_id=orphan_id)
    assert row is not None
    assert row.status == "reaped"


async def test_sweep_session_start_with_no_orphans(store: DataStore, main_repo: Path) -> None:
    report = await sweep_session_start(store, current_session_id="sess-1", main_repo=main_repo)
    assert report.total == 0


# --- closed-PR TTL reaper ---------------------------------------------------


async def test_reap_closed_prs_removes_stale_rows_past_ttl(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """``stale`` rows older than ``ttl_seconds`` get reaped."""
    old_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    stale_id, stale_path = await _seed_worktree_row(
        store,
        main_repo,
        worktree_root,
        session_id="sess-1",
        branch_name="closed-pr-branch",
        pre_branch_key=None,
        dir_name="closed-pr-wt",
        status="stale",
        last_used_at=old_ts,
    )

    report = await reap_for_closed_prs(
        store, session_id="sess-1", main_repo=main_repo, ttl_seconds=3600
    )
    assert report.total == 1
    assert report.removed[0].worktree_id == stale_id

    row = await lookup_by_id(store, worktree_id=stale_id)
    assert row is not None
    assert row.status == "reaped"
    assert not stale_path.exists()


async def test_reap_closed_prs_skips_recent_stale_rows(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """A row marked ``stale`` just now is preserved (still within TTL)."""
    fresh_id, fresh_path = await _seed_worktree_row(
        store,
        main_repo,
        worktree_root,
        session_id="sess-1",
        branch_name="recent-branch",
        pre_branch_key=None,
        dir_name="recent-wt",
        status="stale",
    )
    report = await reap_for_closed_prs(
        store, session_id="sess-1", main_repo=main_repo, ttl_seconds=3600
    )
    assert report.total == 0
    row = await lookup_by_id(store, worktree_id=fresh_id)
    assert row is not None
    assert row.status == "stale"
    assert fresh_path.exists()


async def test_reap_closed_prs_skips_active_rows(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """``active`` rows are never reaped by the closed-PR sweep."""
    old_ts = (datetime.now(UTC) - timedelta(hours=4)).isoformat()
    active_id, active_path = await _seed_worktree_row(
        store,
        main_repo,
        worktree_root,
        session_id="sess-1",
        branch_name="active-branch",
        pre_branch_key=None,
        dir_name="active-wt",
        status="active",
        last_used_at=old_ts,
    )
    report = await reap_for_closed_prs(
        store, session_id="sess-1", main_repo=main_repo, ttl_seconds=3600
    )
    assert report.total == 0
    row = await lookup_by_id(store, worktree_id=active_id)
    assert row is not None
    assert row.status == "active"
    assert active_path.exists()


async def test_reap_closed_prs_rejects_negative_ttl(store: DataStore, main_repo: Path) -> None:
    with pytest.raises(ValueError):
        await reap_for_closed_prs(store, session_id="sess-1", main_repo=main_repo, ttl_seconds=-1)


# --- mixed scenarios --------------------------------------------------------


async def test_sweep_handles_stale_rows_from_other_sessions(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """Prior-session ``stale`` rows ARE reaped by session-start.

    A prior session that crashed mid-rekey can leave behind a stale row
    whose directory is still on disk. session-start is the only point we
    can guarantee will run before any concurrent allocator, so it owns
    cleanup of those leftovers regardless of status (active/reaping/stale).
    Excluding stale would leak the worktree forward indefinitely because
    the closed-PR TTL reaper only scans the current session.
    """
    sid, sid_path = await _seed_worktree_row(
        store,
        main_repo,
        worktree_root,
        session_id="sess-other",
        branch_name="stale-from-other",
        pre_branch_key=None,
        dir_name="stale-other",
        status="active",
    )
    # Move it to stale to simulate the other session having already noticed.
    await mark_status(store, worktree_id=sid, status="stale")

    report = await sweep_session_start(store, current_session_id="sess-1", main_repo=main_repo)
    assert report.total == 1
    assert not sid_path.exists()
    row = await lookup_by_id(store, worktree_id=sid)
    assert row is not None
    assert row.status == "reaped"


# --- WorktreeManager-driven reaper hooks (desktop-12g9) ---------------------


async def test_manager_reap_session_start_removes_prior_session_orphans(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """``WorktreeManager.reap_session_start`` removes other-session rows.

    The current-session row stays ``active``; the prior-session orphan is
    reaped and its on-disk directory deleted.
    """
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.config import RuntimeConfig

    orphan_id, orphan_path = await _seed_worktree_row(
        store,
        main_repo,
        worktree_root,
        session_id="sess-other",
        branch_name="orphan-branch",
        pre_branch_key=None,
        dir_name="orphan-mgr",
    )
    mine_id, mine_path = await _seed_worktree_row(
        store,
        main_repo,
        worktree_root,
        session_id="sess-1",
        branch_name="my-branch",
        pre_branch_key=None,
        dir_name="my-mgr",
    )

    wm = WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=RuntimeConfig(),
    )
    report = await wm.reap_session_start()

    assert report.total == 1
    assert report.removed[0].worktree_id == orphan_id
    orphan_row = await lookup_by_id(store, worktree_id=orphan_id)
    assert orphan_row is not None and orphan_row.status == "reaped"
    assert not orphan_path.exists()

    mine_row = await lookup_by_id(store, worktree_id=mine_id)
    assert mine_row is not None and mine_row.status == "active"
    assert mine_path.exists()


async def test_manager_reap_closed_prs_reaps_stale_past_ttl(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """``reap_closed_prs`` removes stale rows older than the supplied TTL."""
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.config import RuntimeConfig

    old_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    stale_id, stale_path = await _seed_worktree_row(
        store,
        main_repo,
        worktree_root,
        session_id="sess-1",
        branch_name="closed-pr-mgr",
        pre_branch_key=None,
        dir_name="closed-mgr",
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
    report = await wm.reap_closed_prs(ttl_seconds=3600)
    assert report.removed[0].worktree_id == stale_id
    row = await lookup_by_id(store, worktree_id=stale_id)
    assert row is not None and row.status == "reaped"
    assert not stale_path.exists()


async def test_manager_reap_closed_prs_does_not_touch_active_rows(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """An open-PR (status='active') worktree is preserved even past the TTL.

    The TTL reaper only touches ``stale`` rows — open PRs whose worktrees
    were active well beyond the TTL window are not affected.
    """
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.config import RuntimeConfig

    ancient = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    active_id, active_path = await _seed_worktree_row(
        store,
        main_repo,
        worktree_root,
        session_id="sess-1",
        branch_name="long-running-pr",
        pre_branch_key=None,
        dir_name="long-running",
        status="active",
        last_used_at=ancient,
    )

    wm = WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=RuntimeConfig(),
    )
    report = await wm.reap_closed_prs(ttl_seconds=60)
    assert report.total == 0
    row = await lookup_by_id(store, worktree_id=active_id)
    assert row is not None and row.status == "active"
    assert active_path.exists()


# ---------------------------------------------------------------------------
# reap_git_orphans + sweep two-phase reconciliation
# ---------------------------------------------------------------------------


async def test_reap_git_orphans_is_noop_on_clean_state(store: DataStore, main_repo: Path) -> None:
    """No worktrees beyond main → returns empty list, never errors."""
    from agentshore.agents.worktree.reaper import reap_git_orphans

    removed = await reap_git_orphans(store, main_repo=main_repo)
    assert removed == []


async def test_reap_git_orphans_removes_worktree_with_no_db_row(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """A git-registered worktree that has zero rows in any session is reaped."""
    from agentshore.agents.worktree.reaper import reap_git_orphans

    orphan = worktree_root / "pickup-999"
    _git("worktree", "add", "-b", "ghost-999", str(orphan), "HEAD", cwd=main_repo)
    assert orphan.exists()

    removed = await reap_git_orphans(store, main_repo=main_repo)
    assert removed == [str(orphan)]
    assert not orphan.exists()


async def test_reap_git_orphans_preserves_main_checkout(store: DataStore, main_repo: Path) -> None:
    """Main repo path is never returned as an orphan even with no rows."""
    from agentshore.agents.worktree.reaper import reap_git_orphans

    removed = await reap_git_orphans(store, main_repo=main_repo)
    assert str(main_repo.resolve()) not in removed


async def test_reap_git_orphans_preserves_worktree_with_active_row(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """A worktree with an active row in the current session is NOT reaped."""
    from agentshore.agents.worktree.reaper import reap_git_orphans

    _, kept_path = await _seed_worktree_row(
        store,
        main_repo,
        worktree_root,
        session_id="sess-1",
        branch_name="kept",
        pre_branch_key=None,
        dir_name="kept",
        status="active",
    )
    removed = await reap_git_orphans(store, main_repo=main_repo)
    assert str(kept_path) not in removed
    assert kept_path.exists()


async def test_reap_git_orphans_preserves_worktree_with_any_db_row(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """A worktree with a row in a DIFFERENT session is left for the DB-driven path."""
    from agentshore.agents.worktree.reaper import reap_git_orphans

    _, kept_path = await _seed_worktree_row(
        store,
        main_repo,
        worktree_root,
        session_id="sess-other",
        branch_name="other-session",
        pre_branch_key=None,
        dir_name="other",
        status="active",
    )
    removed = await reap_git_orphans(store, main_repo=main_repo)
    assert str(kept_path) not in removed


async def test_sweep_session_start_returns_both_reap_sources(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """One DB-tracked orphan + one git-only orphan → both surface in ReapReport."""
    from agentshore.agents.worktree.reaper import sweep_session_start

    # DB-tracked orphan (different session, active).
    db_orphan_id, db_orphan_path = await _seed_worktree_row(
        store,
        main_repo,
        worktree_root,
        session_id="sess-other",
        branch_name="db-tracked",
        pre_branch_key=None,
        dir_name="db-tracked",
        status="active",
    )
    # Git-only orphan (no DB row at all).
    git_orphan_path = worktree_root / "git-only-orphan"
    _git("worktree", "add", "-b", "git-only", str(git_orphan_path), "HEAD", cwd=main_repo)

    report = await sweep_session_start(store, current_session_id="sess-1", main_repo=main_repo)
    assert len(report.removed) == 1
    assert report.removed[0].worktree_id == db_orphan_id
    assert str(git_orphan_path) in report.git_orphans_removed
    assert not db_orphan_path.exists()
    assert not git_orphan_path.exists()
    assert report.total == 2
