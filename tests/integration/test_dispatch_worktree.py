"""End-to-end integration: AgentManager.dispatch into a AgentShore-managed worktree.

Exercises the wiring landed in ``desktop-mr1i``:

- ``WorktreeManager.allocate_for_dispatch`` materialises a real ``git worktree``
  for a branch-scoped play, and ``AgentManager.dispatch`` runs the fake CLI
  binary inside that worktree.
- Trunk-scoped plays get a ``TrunkAllocation`` and run inside the main repo.
- ``AGENTSHORE_PROJECT_PATH`` continues to point at the main repo regardless of
  the per-dispatch cwd.
- The ``_DispatchMixin`` drops the play with ``worktree_create_failed`` when
  the allocator raises ``WorktreeAllocationFailed`` — no PPO penalty, session
  keeps running.

Mocked surface: the CLI binary itself (a shell script that writes its cwd +
env into a sentinel file before emitting a JSON success block). Everything
else — git, aiosqlite, subprocess — is real.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentshore.agents.manager import AgentManager
from agentshore.agents.worktree import (
    TrunkAllocation,
    WorktreeAllocation,
    WorktreeAllocationFailed,
)
from agentshore.config import AgentConfig, RuntimeConfig
from agentshore.core.main_repo_guard import MainRepoGuard
from agentshore.core.override_queue import OverrideQueue
from agentshore.data.store import DataStore, SessionRecord
from agentshore.plays.base import PlayParams
from agentshore.state import AgentType, PlayType

# ---------------------------------------------------------------------------
# Fake git repo + remote
# ---------------------------------------------------------------------------


def _run(*args: str, cwd: Path) -> str:
    """Run a subprocess synchronously; raise with full output on failure."""
    proc = subprocess.run(
        args,
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"command {args!r} in {cwd} failed (rc={proc.returncode}):\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return proc.stdout


def _init_repo_with_remote(tmp_path: Path, branch: str) -> Path:
    """Create a bare 'origin' and a working repo with ``branch`` pushed upstream."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _run("git", "init", "--bare", "--initial-branch=main", str(origin), cwd=tmp_path)

    main_repo = tmp_path / "project"
    main_repo.mkdir()
    _run("git", "init", "--initial-branch=main", str(main_repo), cwd=tmp_path)
    _run("git", "config", "user.email", "test@example.com", cwd=main_repo)
    _run("git", "config", "user.name", "Test User", cwd=main_repo)
    _run("git", "config", "commit.gpgsign", "false", cwd=main_repo)
    _run("git", "remote", "add", "origin", str(origin), cwd=main_repo)

    (main_repo / "README.md").write_text("hello\n")
    _run("git", "add", "README.md", cwd=main_repo)
    _run("git", "commit", "-m", "initial", cwd=main_repo)
    _run("git", "push", "-u", "origin", "main", cwd=main_repo)

    # Create + push the target branch so PR-scoped allocation can find it.
    _run("git", "switch", "-c", branch, cwd=main_repo)
    (main_repo / "feature.txt").write_text("feature\n")
    _run("git", "add", "feature.txt", cwd=main_repo)
    _run("git", "commit", "-m", f"start {branch}", cwd=main_repo)
    _run("git", "push", "-u", "origin", branch, cwd=main_repo)
    _run("git", "switch", "main", cwd=main_repo)
    return main_repo


# ---------------------------------------------------------------------------
# Fake CLI binary
# ---------------------------------------------------------------------------


def _write_fake_cli(
    *,
    target: Path,
    sentinel_path: Path,
) -> Path:
    """Write an executable shell script that records cwd+env then prints a JSON block.

    The script writes JSON to ``sentinel_path`` with the exact ``pwd`` it
    saw plus the ``AGENTSHORE_PROJECT_PATH`` env var, then emits a success
    skill-result on stdout so ``parse_skill_result`` is happy.
    """
    script = f"""#!/usr/bin/env bash
set -euo pipefail
sentinel="{sentinel_path}"
cat > "$sentinel" <<JSON
{{
  "cwd": "$(pwd)",
  "agentshore_project_path": "${{AGENTSHORE_PROJECT_PATH:-<unset>}}",
  "argv": $(printf '%s\\n' "$@" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read().splitlines()))")
}}
JSON
cat <<'OUT'
```json
{{
  "schema_version": 1,
  "success": true,
  "artifacts": [],
  "issues_created": [],
  "requested_mutations": [],
  "metrics": {{}},
  "error": null
}}
```
OUT
"""
    target.write_text(script)
    target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return target


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(binary: Path) -> RuntimeConfig:
    """Build a minimal config with one CLI agent pointed at the fake binary."""
    return RuntimeConfig(
        agents={
            "claude_code": AgentConfig(
                binary=str(binary),
                model="test-model",
                timeout=30,
                stream_idle_timeout=30,
            )
        }
    )


async def _init_store(tmp_path: Path, *, session_id: str, project_path: Path) -> DataStore:
    db_path = tmp_path / ".agentshore" / "agentshore.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = DataStore(db_path)
    await store.initialize()
    await store.create_session(
        SessionRecord(
            session_id=session_id,
            project_path=str(project_path),
            started_at="2026-05-21T00:00:00+00:00",
        )
    )
    return store


async def _count_active_worktrees(store: DataStore, *, session_id: str) -> int:
    cur = await store._db.execute(  # type: ignore[attr-defined]
        "SELECT COUNT(*) FROM worktrees WHERE session_id = ? AND status = 'active'",
        (session_id,),
    )
    row = await cur.fetchone()
    return int(row[0]) if row is not None else 0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_branch_scoped_dispatch_runs_in_worktree_with_main_repo_env(
    tmp_path: Path,
) -> None:
    """PR-scoped Code Review dispatches into the worktree; env stays main repo."""
    branch = "feature-x"
    main_repo = _init_repo_with_remote(tmp_path, branch)
    sentinel = tmp_path / "sentinel.json"
    binary = _write_fake_cli(target=tmp_path / "fake-cli.sh", sentinel_path=sentinel)

    cfg = _cfg(binary)
    store = await _init_store(tmp_path, session_id="s-branch", project_path=main_repo)
    try:
        manager = AgentManager(
            session_id="s-branch",
            store=store,
            cfg=cfg,
            working_dir=main_repo,
        )

        # Allocate the worktree the way the dispatch mixin does.
        params = PlayParams(branch=branch, pr_number=42)
        allocation = await manager.worktrees.allocate_for_dispatch(
            play_type=PlayType.CODE_REVIEW, params=params
        )
        assert isinstance(allocation, WorktreeAllocation)
        assert allocation.scope == "pr"
        assert allocation.branch_name == branch
        assert allocation.path.exists()
        assert allocation.path != main_repo
        assert (allocation.path / "feature.txt").exists()  # branch checkout worked

        # Dispatch — manager spawns the fake CLI with cwd_override.
        handle = await manager.instantiate(AgentType.CLAUDE_CODE)
        result = await manager.dispatch(
            handle.agent_id,
            "ignored prompt",
            play_type=PlayType.CODE_REVIEW.value,
            cwd_override=allocation.path,
        )
        assert result.exit_code == 0, f"fake CLI failed: {result.raw_output!r}"

        # The fake CLI wrote the cwd + env it saw.
        observed = json.loads(sentinel.read_text())
        assert Path(observed["cwd"]).resolve() == allocation.path.resolve()
        assert Path(observed["agentshore_project_path"]).resolve() == main_repo.resolve(), (
            "AGENTSHORE_PROJECT_PATH must point at main repo, not the worktree"
        )

        # Worktrees table has an active row for the branch.
        assert await _count_active_worktrees(store, session_id="s-branch") == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_trunk_scoped_dispatch_runs_in_main_repo(tmp_path: Path) -> None:
    """RUN_QA returns a TrunkAllocation; dispatch cwd is the main repo."""
    branch = "feature-trunk"
    main_repo = _init_repo_with_remote(tmp_path, branch)
    sentinel = tmp_path / "sentinel.json"
    binary = _write_fake_cli(target=tmp_path / "fake-cli.sh", sentinel_path=sentinel)

    cfg = _cfg(binary)
    store = await _init_store(tmp_path, session_id="s-trunk", project_path=main_repo)
    try:
        manager = AgentManager(
            session_id="s-trunk",
            store=store,
            cfg=cfg,
            working_dir=main_repo,
        )

        params = PlayParams()
        allocation = await manager.worktrees.allocate_for_dispatch(
            play_type=PlayType.RUN_QA, params=params
        )
        assert isinstance(allocation, TrunkAllocation)
        assert allocation.path.resolve() == main_repo.resolve()

        handle = await manager.instantiate(AgentType.CLAUDE_CODE)
        result = await manager.dispatch(
            handle.agent_id,
            "ignored prompt",
            play_type=PlayType.RUN_QA.value,
            cwd_override=allocation.path,
        )
        assert result.exit_code == 0

        observed = json.loads(sentinel.read_text())
        assert Path(observed["cwd"]).resolve() == main_repo.resolve()
        assert Path(observed["agentshore_project_path"]).resolve() == main_repo.resolve()

        # Trunk allocation doesn't create a worktree row.
        assert await _count_active_worktrees(store, session_id="s-trunk") == 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_allocation_failure_drops_play_with_worktree_create_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed allocate drops the selected play with the right reason — no PPO penalty.

    Exercises ``_DispatchMixin._dispatch_play``'s allocator-failure branch
    by monkeypatching ``WorktreeManager.allocate_for_dispatch`` to raise
    ``WorktreeAllocationFailed``. The orchestrator stub is the same harness
    pattern used by ``tests/test_pre_dispatch_path_validation.py``.
    """
    from agentshore.core.orchestrator import Orchestrator

    branch = "feature-fail"
    main_repo = _init_repo_with_remote(tmp_path, branch)
    store = await _init_store(tmp_path, session_id="s-fail", project_path=main_repo)
    try:
        cfg = RuntimeConfig()
        manager = AgentManager(
            session_id="s-fail",
            store=store,
            cfg=cfg,
            working_dir=main_repo,
        )
        # Force the allocator to fail.
        failing_allocate = AsyncMock(
            side_effect=WorktreeAllocationFailed("simulated allocate failure", reason="simulated")
        )
        monkeypatch.setattr(manager.worktrees, "allocate_for_dispatch", failing_allocate)

        orch = Orchestrator.__new__(Orchestrator)
        orch._session_id = "s-fail"
        orch._store = store
        orch._cfg = cfg
        orch._repo_root = main_repo
        orch._main_repo = MainRepoGuard()
        orch._draining = False
        orch._stop_requested = False
        orch._end_session_dispatch_started = False
        orch._in_flight = {}
        orch._dispatch_ctx = {}
        orch._overrides = OverrideQueue()
        orch._registry = None
        orch._selector = None
        orch._last_selection_digest = None
        orch._manager = manager  # real manager — its worktrees is patched above
        orch._state_provider = MagicMock()

        # Capture the drop call without exercising the rest of the drop helper
        # (which writes to several DB tables we don't need exercised here).
        drop_mock = AsyncMock()
        orch._drop_selected_play_before_dispatch = drop_mock  # type: ignore[method-assign]

        state_mock = MagicMock()
        state_mock.session_state = MagicMock()
        state_mock.agents = []

        params = PlayParams(branch=branch, pr_number=99)
        result = await orch._dispatch_play(
            PlayType.CODE_REVIEW,
            params,
            state_mock,
        )

        assert result is False
        drop_mock.assert_awaited_once()
        call = drop_mock.await_args
        assert call.kwargs["reason"] == "worktree_create_failed"
        assert call.kwargs["event"] == "dispatch_worktree_create_failed"
        # No worktrees row was written for the failed attempt.
        assert await _count_active_worktrees(store, session_id="s-fail") == 0
        # No active_play snapshot was published (allocator runs before that).
        assert not orch._in_flight
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_handle_working_dir_stays_pinned_to_main_repo(tmp_path: Path) -> None:
    """``AgentHandle.working_dir`` must not be mutated by cwd_override."""
    branch = "feature-pin"
    main_repo = _init_repo_with_remote(tmp_path, branch)
    sentinel = tmp_path / "sentinel.json"
    binary = _write_fake_cli(target=tmp_path / "fake-cli.sh", sentinel_path=sentinel)

    cfg = _cfg(binary)
    store = await _init_store(tmp_path, session_id="s-pin", project_path=main_repo)
    try:
        manager = AgentManager(
            session_id="s-pin",
            store=store,
            cfg=cfg,
            working_dir=main_repo,
        )
        handle = await manager.instantiate(AgentType.CLAUDE_CODE)
        original = handle.working_dir

        allocation = await manager.worktrees.allocate_for_dispatch(
            play_type=PlayType.CODE_REVIEW,
            params=PlayParams(branch=branch, pr_number=42),
        )
        assert isinstance(allocation, WorktreeAllocation)
        await manager.dispatch(
            handle.agent_id,
            "ignored",
            play_type=PlayType.CODE_REVIEW.value,
            cwd_override=allocation.path,
        )
        # The handle's working_dir is still the main repo, not the worktree.
        assert handle.working_dir == original
        assert handle.working_dir.resolve() == main_repo.resolve()
    finally:
        await store.close()
