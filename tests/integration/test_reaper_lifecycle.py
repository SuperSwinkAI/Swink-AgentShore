"""End-to-end lifecycle wiring for the worktree reapers (desktop-12g9).

Exercises the two reaper hooks against a real ``DataStore`` + real git repo:

- ``_phase_session_start_worktree_sweep`` removes prior-session orphans
  before any dispatch starts.
- ``_CompletionMixin._sweep_closed_pr_worktrees`` reaps ``stale`` rows past
  the configured TTL, and ``_mark_worktrees_stale_for_closed_prs``
  transitions ``active`` rows whose PR just merged.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from agentshore.agents.worktree import WorktreeManager, default_worktree_root
from agentshore.agents.worktree.registry import insert_worktree, lookup_by_id
from agentshore.config import RuntimeConfig, WorktreeConfig
from agentshore.core.phases import _phase_session_start_worktree_sweep
from agentshore.data.store import DataStore, PullRequestRecord

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


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


@pytest.fixture
def main_repo(tmp_path: Path) -> Path:
    """Real git repo seeded with a single commit on ``main``."""
    repo = tmp_path / "proj"
    repo.mkdir()
    _git("init", "--initial-branch=main", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    (repo / "README.md").write_text("# proj\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    return repo


@pytest.fixture
def worktree_root(main_repo: Path) -> Path:
    root = default_worktree_root(main_repo)
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[DataStore]:
    db_path = tmp_path / "lifecycle.db"
    s = DataStore(db_path)
    await s.initialize()
    await s._conn.execute(
        "INSERT INTO sessions (session_id, project_path, started_at) "
        "VALUES ('sess-current', '/tmp/proj', '2026-05-21T00:00:00+00:00')"
    )
    await s._conn.execute(
        "INSERT INTO sessions (session_id, project_path, started_at) "
        "VALUES ('sess-prior', '/tmp/proj', '2026-05-20T00:00:00+00:00')"
    )
    await s._conn.commit()
    try:
        yield s
    finally:
        await s.close()


async def _seed_real_worktree(
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


async def test_session_start_sweep_phase_reaps_orphans_only(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """``_phase_session_start_worktree_sweep`` reaps prior-session rows.

    Current-session ``active`` rows are preserved; current-session ``stale``
    rows are preserved (they go through the TTL reaper, not session-start).
    """
    orphan_id, orphan_path = await _seed_real_worktree(
        store,
        main_repo,
        worktree_root,
        session_id="sess-prior",
        branch_name="prior-feature",
        pre_branch_key=None,
        dir_name="prior-wt",
    )
    current_id, current_path = await _seed_real_worktree(
        store,
        main_repo,
        worktree_root,
        session_id="sess-current",
        branch_name="current-feature",
        pre_branch_key=None,
        dir_name="current-wt",
    )

    # Minimal orchestrator stub — _phase_session_start_worktree_sweep only
    # touches ``orch._runtime.worktrees``, so we don't need a fully bootstrapped
    # orchestrator.
    class _OrchStub:
        pass

    orch = _OrchStub()
    orch._runtime = SimpleNamespace(  # type: ignore[attr-defined]
        worktrees=WorktreeManager(
            session_id="sess-current",
            store=store,
            main_repo=main_repo,
            worktree_root=worktree_root,
            cfg=RuntimeConfig(),
        )
    )

    await _phase_session_start_worktree_sweep(orch=orch, sid="sess-current")  # type: ignore[arg-type]

    orphan_row = await lookup_by_id(store, worktree_id=orphan_id)
    assert orphan_row is not None and orphan_row.status == "reaped"
    assert not orphan_path.exists()

    current_row = await lookup_by_id(store, worktree_id=current_id)
    assert current_row is not None and current_row.status == "active"
    assert current_path.exists()


async def test_session_start_sweep_phase_is_noop_when_disabled(
    store: DataStore, main_repo: Path
) -> None:
    """If ``orch._runtime.worktrees`` is None, the phase short-circuits cleanly."""

    class _OrchStub:
        _runtime = SimpleNamespace(worktrees=None)

    # Should not raise.
    await _phase_session_start_worktree_sweep(orch=_OrchStub(), sid="sess-current")  # type: ignore[arg-type]


async def test_closed_pr_ttl_reaper_runs_through_manager(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """``WorktreeManager.reap_closed_prs`` removes stale rows past the TTL.

    Mixes three current-session rows: a fresh ``stale`` (within TTL, kept),
    an ancient ``stale`` (past TTL, reaped), and an ``active`` row (always
    kept).
    """
    fresh_id, fresh_path = await _seed_real_worktree(
        store,
        main_repo,
        worktree_root,
        session_id="sess-current",
        branch_name="recent-closed-pr",
        pre_branch_key=None,
        dir_name="fresh-stale",
        status="stale",
    )
    ancient_ts = (datetime.now(UTC) - timedelta(hours=4)).isoformat()
    ancient_id, ancient_path = await _seed_real_worktree(
        store,
        main_repo,
        worktree_root,
        session_id="sess-current",
        branch_name="old-closed-pr",
        pre_branch_key=None,
        dir_name="ancient-stale",
        status="stale",
        last_used_at=ancient_ts,
    )
    active_id, active_path = await _seed_real_worktree(
        store,
        main_repo,
        worktree_root,
        session_id="sess-current",
        branch_name="open-pr",
        pre_branch_key=None,
        dir_name="active-pr",
        status="active",
        last_used_at=ancient_ts,
    )

    wm = WorktreeManager(
        session_id="sess-current",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=RuntimeConfig(worktrees=WorktreeConfig(reap_ttl_seconds=3600)),
    )
    report = await wm.reap_closed_prs(ttl_seconds=3600)

    assert report.total == 1
    assert report.removed[0].worktree_id == ancient_id

    ancient_row = await lookup_by_id(store, worktree_id=ancient_id)
    assert ancient_row is not None and ancient_row.status == "reaped"
    assert not ancient_path.exists()

    fresh_row = await lookup_by_id(store, worktree_id=fresh_id)
    assert fresh_row is not None and fresh_row.status == "stale"
    assert fresh_path.exists()

    active_row = await lookup_by_id(store, worktree_id=active_id)
    assert active_row is not None and active_row.status == "active"
    assert active_path.exists()


async def test_mark_worktrees_stale_transitions_active_to_stale_on_pr_close(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """A closed/merged PR moves its branch's worktree row to ``stale``.

    Exercises ``_CompletionMixin._mark_worktrees_stale_for_closed_prs`` —
    which the GitHub poll tick calls with the PRs it just re-pulled at
    ``state='all'`` to confirm new state.
    """
    from agentshore.core.mixins.completion import CompletionProcessor

    active_id, _ = await _seed_real_worktree(
        store,
        main_repo,
        worktree_root,
        session_id="sess-current",
        branch_name="branch-that-merged",
        pre_branch_key=None,
        dir_name="merged-pr-wt",
    )

    wm = WorktreeManager(
        session_id="sess-current",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=RuntimeConfig(),
    )

    # _mark_worktrees_stale_for_closed_prs reads ``_worktrees`` via the host and
    # ``_store``/``_session_id`` as constructor deps on the processor itself.
    completion_stub = SimpleNamespace(
        _runtime=SimpleNamespace(worktrees=wm),
        _store=store,
        _session_id="sess-current",
    )

    merged_pr = PullRequestRecord(
        pr_number=42,
        session_id="sess-current",
        state="merged",
        created_at="2026-05-20T00:00:00+00:00",
        branch="branch-that-merged",
    )
    still_open = PullRequestRecord(
        pr_number=43,
        session_id="sess-current",
        state="open",
        created_at="2026-05-20T00:00:00+00:00",
        branch="branch-still-open",
    )
    await CompletionProcessor._mark_worktrees_stale_for_closed_prs(
        completion_stub,  # type: ignore[arg-type]
        [merged_pr, still_open],
    )

    row = await lookup_by_id(store, worktree_id=active_id)
    assert row is not None
    assert row.status == "stale"
    assert row.failure_reason == "pr_closed_state_merged"
