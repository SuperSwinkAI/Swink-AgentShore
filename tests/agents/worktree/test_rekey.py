"""Unit tests for the rekey module.

Branch-creating worktrees come in with ``pre_branch_key='pickup-...'``
and ``branch_name=NULL``. After a successful play, ``rekey_worktree``
renames the on-disk directory and updates the row to point at the real
branch. Order is rename-first, DB-update-second so a DB failure between
the two steps leaves a recoverable state (row marked ``stale``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentshore.agents.worktree.registry import (
    insert_worktree,
    lookup_by_id,
)
from agentshore.agents.worktree.rekey import (
    detect_branch_in_worktree,
    rekey_worktree,
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


# --- detect_branch_in_worktree -----------------------------------------------


async def test_detect_branch_returns_branch_name(tmp_path: Path, fake_remote_repo: Path) -> None:
    wt = tmp_path / "wt"
    _git("clone", str(fake_remote_repo), str(wt))
    _git("checkout", "-b", "fix/issue-7", cwd=wt)
    branch = await detect_branch_in_worktree(wt)
    assert branch == "fix/issue-7"


async def test_detect_branch_returns_none_on_detached_head(
    tmp_path: Path, fake_remote_repo: Path
) -> None:
    wt = tmp_path / "wt"
    _git("clone", str(fake_remote_repo), str(wt))
    sha = _git("rev-parse", "HEAD", cwd=wt).strip()
    _git("checkout", sha, cwd=wt)
    branch = await detect_branch_in_worktree(wt)
    assert branch is None


async def test_detect_branch_returns_none_for_missing_path(tmp_path: Path) -> None:
    branch = await detect_branch_in_worktree(tmp_path / "nope")
    assert branch is None


# --- rekey_worktree happy path ----------------------------------------------


async def test_rekey_renames_dir_and_updates_row(
    store: DataStore, tmp_path: Path, worktree_root: Path, fake_remote_repo: Path
) -> None:
    """Rename moves the dir; row clears ``pre_branch_key`` and gains a branch."""
    original = worktree_root / "pickup-bd-42"
    _git("clone", str(fake_remote_repo), str(original))
    _git("checkout", "-b", "fix/42", cwd=original)

    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name=None,
        pre_branch_key="pickup-bd-42",
        worktree_path=str(original),
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
    )
    promoted = await rekey_worktree(
        store, row=row, branch_name="fix/42", worktree_root=worktree_root
    )
    assert promoted.branch_name == "fix/42"
    assert promoted.pre_branch_key is None
    assert Path(promoted.worktree_path).exists()
    assert not original.exists()


async def test_rekey_idempotent_when_path_already_matches(
    store: DataStore, worktree_root: Path, fake_remote_repo: Path
) -> None:
    """Rekey where the slug already matches the current path is a no-op rename."""
    target = worktree_root / "fix-already-matches"
    _git("clone", str(fake_remote_repo), str(target))
    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name=None,
        pre_branch_key="pickup-bd-99",
        worktree_path=str(target),
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
    )
    promoted = await rekey_worktree(
        store,
        row=row,
        branch_name="fix/already-matches",
        worktree_root=worktree_root,
    )
    assert promoted.branch_name == "fix/already-matches"
    assert target.exists()


# --- failure paths -----------------------------------------------------------


async def test_rekey_db_failure_after_rename_marks_stale(
    store: DataStore,
    tmp_path: Path,
    worktree_root: Path,
    fake_remote_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the DB update fails after the rename, the row is marked stale.

    Session-start sweep will then drop it on the next session.
    """
    original = worktree_root / "pickup-bd-77"
    _git("clone", str(fake_remote_repo), str(original))

    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name=None,
        pre_branch_key="pickup-bd-77",
        worktree_path=str(original),
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
    )

    from agentshore.agents.worktree import rekey as rekey_mod

    async def fake_rekey_row(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated DB write failure mid-rename")

    monkeypatch.setattr(rekey_mod, "rekey_row", fake_rekey_row)

    with pytest.raises(RuntimeError, match="simulated DB write failure"):
        await rekey_worktree(
            store,
            row=row,
            branch_name="fix/77",
            worktree_root=worktree_root,
        )

    # Row should now be 'stale' with a descriptive failure_reason. The
    # session-start reaper will pick it up on next start.
    fetched = await lookup_by_id(store, worktree_id=row.worktree_id)
    assert fetched is not None
    assert fetched.status == "stale"
    assert fetched.failure_reason is not None
    assert "rekey_db_update_failed" in fetched.failure_reason

    # The rename did happen — dir is at the new path, original is gone.
    new_path = worktree_root / "fix-77"
    assert new_path.exists()
    assert not original.exists()


async def test_rekey_rename_failure_keeps_row_active(
    store: DataStore,
    worktree_root: Path,
    fake_remote_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rename failure marks the row stale; the DB update is skipped."""
    original = worktree_root / "pickup-bd-rn"
    _git("clone", str(fake_remote_repo), str(original))

    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name=None,
        pre_branch_key="pickup-bd-rn",
        worktree_path=str(original),
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
    )

    from agentshore.agents.worktree import rekey as rekey_mod

    def bad_move(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(rekey_mod.shutil, "move", bad_move)

    with pytest.raises(OSError, match="simulated rename failure"):
        await rekey_worktree(
            store,
            row=row,
            branch_name="fix/rn",
            worktree_root=worktree_root,
        )
    fetched = await lookup_by_id(store, worktree_id=row.worktree_id)
    assert fetched is not None
    assert fetched.status == "stale"
    assert fetched.branch_name is None  # rekey did not advance the row
    assert fetched.failure_reason is not None
    assert "rekey_rename_failed" in fetched.failure_reason


async def test_rekey_rename_failure_releases_orphaned_worktree(
    store: DataStore,
    main_repo: Path,
    worktree_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rename failure → on-disk worktree force-removed so branch lock releases.

    Without this, the next allocation on the agent-created branch would
    collide until session-start sweep runs.
    """
    original = worktree_root / "pickup-bd-rl"
    _git("worktree", "add", "-b", "agent/created-branch", str(original), "HEAD", cwd=main_repo)
    assert original.exists()

    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name=None,
        pre_branch_key="pickup-bd-rl",
        worktree_path=str(original),
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
    )

    from agentshore.agents.worktree import rekey as rekey_mod

    def bad_move(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(rekey_mod.shutil, "move", bad_move)

    with pytest.raises(OSError, match="simulated rename failure"):
        await rekey_worktree(
            store,
            row=row,
            branch_name="agent/created-branch",
            worktree_root=worktree_root,
        )

    # On-disk orphan is gone; the branch is freed for the next allocator.
    assert not original.exists()


async def test_rekey_db_update_failure_runs_git_repair(
    store: DataStore,
    main_repo: Path,
    worktree_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB update failure post-rename → git worktree repair runs to align metadata."""
    original = worktree_root / "pickup-bd-rp"
    _git("worktree", "add", "-b", "agent/repaired", str(original), "HEAD", cwd=main_repo)
    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name=None,
        pre_branch_key="pickup-bd-rp",
        worktree_path=str(original),
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
    )

    from agentshore.agents.worktree import rekey as rekey_mod

    async def fake_rekey_row(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated DB write failure mid-rename")

    repair_calls: list[tuple[object, ...]] = []
    real_repair = rekey_mod._repair_git_worktree_metadata

    async def tracking_repair(**kwargs: object) -> None:
        repair_calls.append((kwargs,))
        await real_repair(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(rekey_mod, "rekey_row", fake_rekey_row)
    monkeypatch.setattr(rekey_mod, "_repair_git_worktree_metadata", tracking_repair)

    with pytest.raises(RuntimeError, match="simulated DB write failure"):
        await rekey_worktree(
            store,
            row=row,
            branch_name="agent/repaired",
            worktree_root=worktree_root,
        )

    # Repair was invoked with the right reason.
    assert len(repair_calls) == 1
    assert repair_calls[0][0]["reason"] == "rekey_db_update_failed"
