"""Unit tests for ``WorktreeManager`` routing + verification primitives.

Pairs with ``test_codex_fixes.py`` (which exercises lifecycle integration);
this module focuses on the standalone classifier and the post-add
registry-verification helper that landed in the issue-#584 cleanup pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentshore.agents.worktree import WorktreeManager
from agentshore.agents.worktree.allocator import (
    AllocateResult,
    WorktreeAllocationFailed,
)
from agentshore.config import RuntimeConfig
from agentshore.data.store import DataStore
from agentshore.plays.base import PlayParams
from agentshore.state import PlayType


def _make_manager(store: DataStore, main_repo: Path, worktree_root: Path) -> WorktreeManager:
    return WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=main_repo,
        worktree_root=worktree_root,
        cfg=RuntimeConfig(),
    )


# --- classifier tripwires ---------------------------------------------------


def test_cleanup_classifies_as_trunk_not_branch_creating(
    tmp_path: Path,
) -> None:
    """CLEANUP must stay trunk-scoped — see manager.py _TRUNK_SCOPED_PLAYS.

    Tripwire: if a future refactor moves ``PlayType.CLEANUP`` back into
    ``_BRANCH_CREATING_PLAYS``, the manager will pre-allocate a worktree
    for the skill, which (a) leaks a PlayType enum through the JSON
    serializer and (b) duplicates the skill's own ``chore/cleanup-*``
    branch. The classifier must return ``"trunk"`` here.
    """
    store = DataStore(tmp_path / "_classify.db")  # not initialised — we don't touch DB
    wm = WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=tmp_path,
        worktree_root=tmp_path / "worktrees",
        cfg=RuntimeConfig(),
    )
    assert wm._classify(PlayType.CLEANUP, PlayParams()) == "trunk"


def test_issue_pickup_classifies_as_branch_creating(
    tmp_path: Path,
) -> None:
    """Positive counterpart: ISSUE_PICKUP stays in the branch-creating bucket."""
    store = DataStore(tmp_path / "_classify.db")
    wm = WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=tmp_path,
        worktree_root=tmp_path / "worktrees",
        cfg=RuntimeConfig(),
    )
    assert wm._classify(PlayType.ISSUE_PICKUP, PlayParams(issue_number=42)) == "branch_creating"


def test_systematic_debugging_routes_dynamically(tmp_path: Path) -> None:
    """SYSTEMATIC_DEBUGGING is the only play with conditional scope.

    Pickup-style (no PR number) → trunk; continuing-debug against a PR →
    pr. The comment in ``_classify`` documents the rationale.
    """
    store = DataStore(tmp_path / "_classify.db")
    wm = WorktreeManager(
        session_id="sess-1",
        store=store,
        main_repo=tmp_path,
        worktree_root=tmp_path / "worktrees",
        cfg=RuntimeConfig(),
    )
    assert wm._classify(PlayType.SYSTEMATIC_DEBUGGING, PlayParams()) == "trunk"
    assert wm._classify(PlayType.SYSTEMATIC_DEBUGGING, PlayParams(pr_number=99, branch="x")) == "pr"


# --- post-add registry verification (issue #584, item 5) --------------------


async def test_verify_worktree_registered_skips_when_reused(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """``created=False`` (reused worktree) short-circuits verification.

    Existing-worktree reuse already confirmed registration via
    ``_existing_worktree_for_path`` upstream — re-running ``git worktree
    list`` here would be redundant. The helper must return without
    touching git.
    """
    wm = _make_manager(store, main_repo, worktree_root)
    allocate = AllocateResult(
        path=worktree_root / "anywhere", created=False, fetched=True, head_sha="deadbeef"
    )
    # Should NOT raise even though the path was never created.
    await wm._verify_worktree_registered(allocate, scope="pr")


async def test_verify_worktree_registered_raises_on_mismatch(
    store: DataStore,
    main_repo: Path,
    worktree_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the post-add ``git worktree list`` lacks our path, raise.

    Simulates the window where ``git`` reports success on ``worktree add``
    but the admin entry never lands (process killed mid-add, FS race).
    Verifier must clean up the on-disk path and raise
    ``WorktreeAllocationFailed(reason="git_add_mismatch")``.
    """
    from agentshore.agents.worktree import manager as manager_mod

    async def fake_list_porcelain(*_args: Any, **_kwargs: Any) -> list[str]:
        return []  # registry is empty — our new worktree didn't land

    monkeypatch.setattr(manager_mod, "_list_worktrees_porcelain", fake_list_porcelain)

    # Materialise a real on-disk dir so the cleanup branch has something
    # to remove (otherwise _best_effort_remove is a no-op).
    fake_path = worktree_root / "phantom"
    fake_path.mkdir()

    wm = _make_manager(store, main_repo, worktree_root)
    allocate = AllocateResult(path=fake_path, created=True, fetched=True, head_sha="deadbeef")

    with pytest.raises(WorktreeAllocationFailed) as exc:
        await wm._verify_worktree_registered(allocate, scope="pr")
    assert exc.value.reason == "git_add_mismatch"
    # _best_effort_remove tries git worktree remove first (no-op, the path
    # was never registered) then rmtree the dir. The dir should be gone.
    assert not fake_path.exists()


async def test_verify_worktree_registered_passes_when_listed(
    store: DataStore,
    main_repo: Path,
    worktree_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: porcelain lists the path → verification is a no-op."""
    from agentshore.agents.worktree import manager as manager_mod

    target = worktree_root / "good-wt"
    target.mkdir()
    resolved = str(target.resolve())

    async def fake_list_porcelain(*_args: Any, **_kwargs: Any) -> list[str]:
        return [resolved]

    monkeypatch.setattr(manager_mod, "_list_worktrees_porcelain", fake_list_porcelain)

    wm = _make_manager(store, main_repo, worktree_root)
    allocate = AllocateResult(path=target, created=True, fetched=True, head_sha="deadbeef")
    await wm._verify_worktree_registered(allocate, scope="branch_creating")
    # Path survives because verification didn't trip cleanup.
    assert target.exists()


# --- finalize_after_dispatch fallback branch detection ----------------------


async def test_finalize_branch_creating_detects_branch_when_result_missing(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """Successful branch-creating play without ``result.branch`` still rekeys.

    Some skill implementations create a branch (``git checkout -b``) but
    omit the field from the JSON result block. The finalize path falls
    back to ``detect_branch_in_worktree`` so the row is promoted from the
    prebranch key to the real branch instead of being marked stale.
    """
    import subprocess

    from agentshore.agents.worktree import WorktreeAllocation
    from agentshore.agents.worktree.registry import (
        insert_worktree,
        lookup_by_branch,
    )
    from agentshore.state import PlayOutcome, SkillResult

    src = worktree_root / "pickup-no-branch-field"
    subprocess.check_call(
        ["git", "worktree", "add", "-b", "feature/detected", str(src), "HEAD"],
        cwd=str(main_repo),
    )
    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name=None,
        pre_branch_key="pickup-77",
        worktree_path=str(src),
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
    )

    wm = _make_manager(store, main_repo, worktree_root)
    alloc = WorktreeAllocation(
        worktree_id=row.worktree_id,
        path=src,
        branch_name=None,
        pre_branch_key="pickup-77",
        play_type=PlayType.ISSUE_PICKUP,
        scope="branch_creating",
    )
    # SkillResult.success=True, branch=None — the trigger for the fallback.
    skill_result = SkillResult(success=True, branch=None)
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

    returned_branch = await wm.finalize_after_dispatch(
        alloc, result=skill_result, play_outcome=outcome
    )

    # desktop-edtl: finalize returns the detected branch so the executor can
    # back-fill params.branch before PR records are persisted.
    assert returned_branch == "feature/detected"

    # Row should now be keyed by the detected branch, status active.
    promoted = await lookup_by_branch(store, session_id="sess-1", branch_name="feature/detected")
    assert promoted is not None
    assert promoted.worktree_id == row.worktree_id
    assert promoted.status == "active"
    assert promoted.pre_branch_key is None


async def test_finalize_branch_creating_returns_branch_from_skill_result(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """When SkillResult provides a branch, finalize returns it (desktop-edtl)."""
    import subprocess

    from agentshore.agents.worktree import WorktreeAllocation
    from agentshore.agents.worktree.registry import insert_worktree
    from agentshore.state import PlayOutcome, SkillResult

    src = worktree_root / "pickup-with-branch-field"
    subprocess.check_call(
        ["git", "worktree", "add", "-b", "feature/from-result", str(src), "HEAD"],
        cwd=str(main_repo),
    )
    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name=None,
        pre_branch_key="pickup-88",
        worktree_path=str(src),
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
    )

    wm = _make_manager(store, main_repo, worktree_root)
    alloc = WorktreeAllocation(
        worktree_id=row.worktree_id,
        path=src,
        branch_name=None,
        pre_branch_key="pickup-88",
        play_type=PlayType.ISSUE_PICKUP,
        scope="branch_creating",
    )
    skill_result = SkillResult(success=True, branch="feature/from-result")
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

    returned_branch = await wm.finalize_after_dispatch(
        alloc, result=skill_result, play_outcome=outcome
    )
    assert returned_branch == "feature/from-result"


async def test_finalize_pr_scoped_returns_none(
    store: DataStore, main_repo: Path, worktree_root: Path
) -> None:
    """PR-scoped finalize returns None (no branch discovery needed)."""
    import subprocess

    from agentshore.agents.worktree import WorktreeAllocation
    from agentshore.agents.worktree.registry import insert_worktree
    from agentshore.state import PlayOutcome

    src = worktree_root / "review-existing"
    subprocess.check_call(
        ["git", "worktree", "add", "-b", "feature/existing", str(src), "HEAD"],
        cwd=str(main_repo),
    )
    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name="feature/existing",
        pre_branch_key=None,
        worktree_path=str(src),
        original_play_type="code_review",
        base_ref="origin/HEAD",
        head_sha=None,
    )

    wm = _make_manager(store, main_repo, worktree_root)
    alloc = WorktreeAllocation(
        worktree_id=row.worktree_id,
        path=src,
        branch_name="feature/existing",
        pre_branch_key=None,
        play_type=PlayType.CODE_REVIEW,
        scope="pr",
    )
    outcome = PlayOutcome(
        play_type=PlayType.CODE_REVIEW,
        agent_id=None,
        success=True,
        partial=False,
        duration_seconds=0.0,
        token_cost=0,
        dollar_cost=0.0,
        artifacts=[],
        alignment_delta=0.0,
    )

    returned_branch = await wm.finalize_after_dispatch(alloc, result=None, play_outcome=outcome)
    assert returned_branch is None


# --- TOCTOU insert-conflict re-lookup ---------------------------------------


async def test_pr_scoped_insert_conflict_relookup_returns_existing(
    store: DataStore,
    main_repo: Path,
    worktree_root: Path,
    remote_branch: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent insert on the same branch resolves to the existing row.

    Two dispatches racing on the same branch: one wins the insert, the
    other gets ``WorktreeAllocationConflict``. The losing dispatch must
    re-lookup the existing row and return it (sharing the worktree) rather
    than crashing the play.
    """
    from agentshore.agents.worktree import manager as manager_mod
    from agentshore.agents.worktree.registry import (
        WorktreeAllocationConflict,
    )
    from agentshore.agents.worktree.registry import (
        insert_worktree as real_insert,
    )

    pre_seeded: dict[str, int] = {}

    async def conflicting_insert(*args: Any, **kwargs: Any) -> Any:
        # Simulate a parallel writer winning the race: insert the row
        # ourselves on first call, then raise the conflict the manager
        # would have caught.
        if "winner_id" not in pre_seeded:
            row = await real_insert(*args, **kwargs)
            pre_seeded["winner_id"] = row.worktree_id
            raise WorktreeAllocationConflict("simulated concurrent insert")
        raise AssertionError("insert called twice; relookup branch should win")

    monkeypatch.setattr(manager_mod, "insert_worktree", conflicting_insert)

    wm = _make_manager(store, main_repo, worktree_root)
    params = PlayParams(branch=remote_branch, pr_number=1)
    allocation = await wm._allocate_pr_scoped(PlayType.CODE_REVIEW, params)
    assert allocation.worktree_id == pre_seeded["winner_id"]
    assert allocation.branch_name == remote_branch


async def test_pr_scoped_insert_failure_cleans_up_ondisk(
    store: DataStore,
    main_repo: Path,
    worktree_root: Path,
    remote_branch: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-conflict DB failure → the just-materialised worktree gets removed.

    If ``insert_worktree`` raises for any reason other than
    ``WorktreeAllocationConflict`` (DB connection dropped, operational
    error, etc.), the on-disk worktree has no owning row and would leak.
    The manager must drop it before re-raising.
    """
    from agentshore.agents.worktree import manager as manager_mod

    async def failing_insert(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(manager_mod, "insert_worktree", failing_insert)

    wm = _make_manager(store, main_repo, worktree_root)
    params = PlayParams(branch=remote_branch, pr_number=2)

    with pytest.raises(RuntimeError, match="simulated DB failure"):
        await wm._allocate_pr_scoped(PlayType.CODE_REVIEW, params)

    # The on-disk worktree the manager created before the insert must be gone.
    expected_path = worktree_root / "feature-x"
    assert not expected_path.exists()
