"""Unit tests for the ``worktrees`` table I/O helpers.

Real aiosqlite (no mocks): the partial unique indexes are SQL-engine
behaviour and the entire point of these tests is to verify them.
"""

from __future__ import annotations

import pytest

from agentshore.agents.worktree.registry import (
    WorktreeAllocationConflict,
    insert_worktree,
    list_active,
    list_orphans,
    lookup_by_branch,
    lookup_by_id,
    lookup_by_prebranch_key,
    mark_status,
    rekey_row,
    touch,
)
from agentshore.data.store import DataStore

# --- insert + lookup ---------------------------------------------------------


async def test_insert_and_lookup_by_branch(store: DataStore) -> None:
    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name="feature/x",
        pre_branch_key=None,
        worktree_path="/tmp/wt/feature-x",
        original_play_type="code_review",
        base_ref="origin/feature/x",
        head_sha="abc123",
    )
    assert row.worktree_id > 0
    assert row.status == "active"

    fetched = await lookup_by_branch(store, session_id="sess-1", branch_name="feature/x")
    assert fetched is not None
    assert fetched.worktree_id == row.worktree_id
    assert fetched.head_sha == "abc123"


async def test_insert_requires_branch_or_prebranch(store: DataStore) -> None:
    with pytest.raises(ValueError):
        await insert_worktree(
            store,
            session_id="sess-1",
            branch_name=None,
            pre_branch_key=None,
            worktree_path="/tmp/wt/bogus",
            original_play_type="issue_pickup",
            base_ref="origin/HEAD",
            head_sha=None,
        )


async def test_insert_invalid_status_rejected(store: DataStore) -> None:
    with pytest.raises(ValueError):
        await insert_worktree(
            store,
            session_id="sess-1",
            branch_name="b",
            pre_branch_key=None,
            worktree_path="/tmp/wt/b",
            original_play_type="code_review",
            base_ref="origin/b",
            head_sha=None,
            status="bogus",  # type: ignore[arg-type]
        )


# --- partial unique index coverage -------------------------------------------


async def test_unique_branch_index_blocks_double_insert(store: DataStore) -> None:
    await insert_worktree(
        store,
        session_id="sess-1",
        branch_name="feature/x",
        pre_branch_key=None,
        worktree_path="/tmp/wt/a",
        original_play_type="code_review",
        base_ref="origin/feature/x",
        head_sha=None,
    )
    with pytest.raises(WorktreeAllocationConflict):
        await insert_worktree(
            store,
            session_id="sess-1",
            branch_name="feature/x",
            pre_branch_key=None,
            worktree_path="/tmp/wt/b",
            original_play_type="unblock_pr",
            base_ref="origin/feature/x",
            head_sha=None,
        )


async def test_unique_prebranch_index_blocks_double_insert(store: DataStore) -> None:
    await insert_worktree(
        store,
        session_id="sess-1",
        branch_name=None,
        pre_branch_key="pickup-bd-1",
        worktree_path="/tmp/wt/pkup-a",
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
    )
    with pytest.raises(WorktreeAllocationConflict):
        await insert_worktree(
            store,
            session_id="sess-1",
            branch_name=None,
            pre_branch_key="pickup-bd-1",
            worktree_path="/tmp/wt/pkup-b",
            original_play_type="issue_pickup",
            base_ref="origin/HEAD",
            head_sha=None,
        )


async def test_unique_indexes_scope_to_session(store: DataStore) -> None:
    """Different sessions on the same branch coexist (no cross-session collision)."""
    await insert_worktree(
        store,
        session_id="sess-1",
        branch_name="feature/x",
        pre_branch_key=None,
        worktree_path="/tmp/wt/a",
        original_play_type="code_review",
        base_ref="origin/feature/x",
        head_sha=None,
    )
    other = await insert_worktree(
        store,
        session_id="sess-other",
        branch_name="feature/x",
        pre_branch_key=None,
        worktree_path="/tmp/wt/b",
        original_play_type="code_review",
        base_ref="origin/feature/x",
        head_sha=None,
    )
    assert other.session_id == "sess-other"


async def test_partial_index_skips_terminal_rows(store: DataStore) -> None:
    """``reaped`` row on a branch is excluded from the unique index."""
    first = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name="feature/x",
        pre_branch_key=None,
        worktree_path="/tmp/wt/a",
        original_play_type="code_review",
        base_ref="origin/feature/x",
        head_sha=None,
    )
    await mark_status(store, worktree_id=first.worktree_id, status="reaped")
    second = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name="feature/x",
        pre_branch_key=None,
        worktree_path="/tmp/wt/b",
        original_play_type="code_review",
        base_ref="origin/feature/x",
        head_sha=None,
    )
    assert second.worktree_id != first.worktree_id


# --- status transitions + touch ---------------------------------------------


async def test_mark_status_durable_and_idempotent(store: DataStore) -> None:
    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name="branch-1",
        pre_branch_key=None,
        worktree_path="/tmp/wt/branch-1",
        original_play_type="code_review",
        base_ref="origin/branch-1",
        head_sha=None,
    )
    await mark_status(
        store,
        worktree_id=row.worktree_id,
        status="stale",
        failure_reason="branch_gone",
    )
    fetched = await lookup_by_id(store, worktree_id=row.worktree_id)
    assert fetched is not None
    assert fetched.status == "stale"
    assert fetched.failure_reason == "branch_gone"

    # Idempotent re-mark.
    await mark_status(store, worktree_id=row.worktree_id, status="stale")
    fetched2 = await lookup_by_id(store, worktree_id=row.worktree_id)
    assert fetched2 is not None
    assert fetched2.status == "stale"


async def test_mark_reaped_stamps_reaped_at(store: DataStore) -> None:
    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name="branch-rk",
        pre_branch_key=None,
        worktree_path="/tmp/wt/branch-rk",
        original_play_type="code_review",
        base_ref="origin/branch-rk",
        head_sha=None,
    )
    await mark_status(store, worktree_id=row.worktree_id, status="reaped")
    fetched = await lookup_by_id(store, worktree_id=row.worktree_id)
    assert fetched is not None
    assert fetched.status == "reaped"
    assert fetched.reaped_at is not None


async def test_touch_updates_last_used_at(store: DataStore) -> None:
    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name="branch-2",
        pre_branch_key=None,
        worktree_path="/tmp/wt/branch-2",
        original_play_type="code_review",
        base_ref="origin/branch-2",
        head_sha=None,
    )
    initial_last_used = row.last_used_at
    await touch(store, worktree_id=row.worktree_id, head_sha="def456")
    fetched = await lookup_by_id(store, worktree_id=row.worktree_id)
    assert fetched is not None
    assert fetched.head_sha == "def456"
    # last_used_at must advance or at least be re-stamped (string compare ok ISO).
    assert fetched.last_used_at >= initial_last_used


# --- rekey -------------------------------------------------------------------


async def test_rekey_clears_prebranch_key(store: DataStore) -> None:
    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name=None,
        pre_branch_key="pickup-bd-99",
        worktree_path="/tmp/wt/pickup-bd-99",
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
    )
    promoted = await rekey_row(
        store,
        worktree_id=row.worktree_id,
        branch_name="fix/issue-99",
        new_path="/tmp/wt/fix-issue-99",
    )
    assert promoted.branch_name == "fix/issue-99"
    assert promoted.pre_branch_key is None
    assert promoted.worktree_path == "/tmp/wt/fix-issue-99"

    # The pre-branch key is now reusable.
    reused = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name=None,
        pre_branch_key="pickup-bd-99",
        worktree_path="/tmp/wt/pickup-bd-99-v2",
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
    )
    assert reused.worktree_id != row.worktree_id


async def test_rekey_conflict_when_branch_taken(store: DataStore) -> None:
    await insert_worktree(
        store,
        session_id="sess-1",
        branch_name="fix/issue-1",
        pre_branch_key=None,
        worktree_path="/tmp/wt/fix-issue-1",
        original_play_type="code_review",
        base_ref="origin/fix/issue-1",
        head_sha=None,
    )
    pre = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name=None,
        pre_branch_key="pickup-bd-1",
        worktree_path="/tmp/wt/pickup-bd-1",
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
    )
    with pytest.raises(WorktreeAllocationConflict):
        await rekey_row(
            store,
            worktree_id=pre.worktree_id,
            branch_name="fix/issue-1",
            new_path="/tmp/wt/fix-issue-1-collide",
        )


# --- listing helpers ---------------------------------------------------------


async def test_list_active_excludes_terminal_rows(store: DataStore) -> None:
    row1 = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name="b1",
        pre_branch_key=None,
        worktree_path="/tmp/wt/b1",
        original_play_type="code_review",
        base_ref="origin/b1",
        head_sha=None,
    )
    await insert_worktree(
        store,
        session_id="sess-1",
        branch_name="b2",
        pre_branch_key=None,
        worktree_path="/tmp/wt/b2",
        original_play_type="code_review",
        base_ref="origin/b2",
        head_sha=None,
    )
    await mark_status(store, worktree_id=row1.worktree_id, status="reaped")
    active = await list_active(store, session_id="sess-1")
    assert len(active) == 1
    assert active[0].branch_name == "b2"


async def test_list_orphans_returns_other_sessions(store: DataStore) -> None:
    await insert_worktree(
        store,
        session_id="sess-1",
        branch_name="mine",
        pre_branch_key=None,
        worktree_path="/tmp/wt/mine",
        original_play_type="code_review",
        base_ref="origin/mine",
        head_sha=None,
    )
    await insert_worktree(
        store,
        session_id="sess-other",
        branch_name="orphan",
        pre_branch_key=None,
        worktree_path="/tmp/wt/orphan",
        original_play_type="code_review",
        base_ref="origin/orphan",
        head_sha=None,
    )
    orphans = await list_orphans(store, current_session_id="sess-1")
    assert len(orphans) == 1
    assert orphans[0].branch_name == "orphan"


async def test_lookup_by_prebranch_returns_active_only(store: DataStore) -> None:
    row = await insert_worktree(
        store,
        session_id="sess-1",
        branch_name=None,
        pre_branch_key="pickup-bd-7",
        worktree_path="/tmp/wt/pkup-7",
        original_play_type="issue_pickup",
        base_ref="origin/HEAD",
        head_sha=None,
    )
    found = await lookup_by_prebranch_key(store, session_id="sess-1", pre_branch_key="pickup-bd-7")
    assert found is not None
    assert found.worktree_id == row.worktree_id

    await mark_status(store, worktree_id=row.worktree_id, status="reaped")
    after = await lookup_by_prebranch_key(store, session_id="sess-1", pre_branch_key="pickup-bd-7")
    assert after is None
