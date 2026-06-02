"""``worktrees`` table I/O — thin delegation to the DataStore mixin.

The DB I/O for the ``worktrees`` table lives in
``agentshore.data.store.mixins.worktrees`` (``_WorktreesMixin``) alongside the
other 21 tables, so the data layer has one home and one convention. These
free functions preserve the historical call surface (``insert_worktree(store,
...)`` etc.) by delegating to the store methods, so existing callers and tests
do not change.

``WorktreeRow`` / ``WorktreeStatus`` are re-exported from
``agentshore.data.models``; ``WorktreeAllocationConflict`` from the mixin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.data.models import WorktreeRow, WorktreeStatus
from agentshore.data.store.mixins.worktrees import WorktreeAllocationConflict

if TYPE_CHECKING:
    from agentshore.data.store import DataStore


async def insert_worktree(
    store: DataStore,
    *,
    session_id: str,
    branch_name: str | None,
    pre_branch_key: str | None,
    worktree_path: str,
    original_play_type: str,
    base_ref: str,
    head_sha: str | None,
    status: WorktreeStatus = "active",
) -> WorktreeRow:
    """Insert a fresh row and return the populated ``WorktreeRow``.

    Raises ``WorktreeAllocationConflict`` if the unique partial index fires
    for an active ``(session_id, branch_name)`` or
    ``(session_id, pre_branch_key)``.
    """
    return await store.insert_worktree(
        session_id=session_id,
        branch_name=branch_name,
        pre_branch_key=pre_branch_key,
        worktree_path=worktree_path,
        original_play_type=original_play_type,
        base_ref=base_ref,
        head_sha=head_sha,
        status=status,
    )


async def lookup_by_branch(
    store: DataStore,
    *,
    session_id: str,
    branch_name: str,
) -> WorktreeRow | None:
    """Return the active row for ``(session_id, branch_name)``, if any."""
    return await store.lookup_worktree_by_branch(session_id=session_id, branch_name=branch_name)


async def lookup_by_prebranch_key(
    store: DataStore,
    *,
    session_id: str,
    pre_branch_key: str,
) -> WorktreeRow | None:
    """Return the active row for ``(session_id, pre_branch_key)``, if any."""
    return await store.lookup_worktree_by_prebranch_key(
        session_id=session_id, pre_branch_key=pre_branch_key
    )


async def lookup_by_id(
    store: DataStore,
    *,
    worktree_id: int,
) -> WorktreeRow | None:
    """Return a row by primary key, regardless of status."""
    return await store.lookup_worktree_by_id(worktree_id=worktree_id)


async def list_active(store: DataStore, *, session_id: str) -> list[WorktreeRow]:
    """Every row in ``active`` or ``reaping`` status for the session."""
    return await store.list_active_worktrees(session_id=session_id)


async def list_orphans(store: DataStore, *, current_session_id: str) -> list[WorktreeRow]:
    """Rows from prior sessions that still hold an on-disk worktree."""
    return await store.list_orphan_worktrees(current_session_id=current_session_id)


async def live_worktree_paths(store: DataStore) -> set[str]:
    """Worktree paths from rows in non-terminal status across all sessions."""
    return await store.live_worktree_paths()


async def list_stale(store: DataStore, *, session_id: str, before_iso: str) -> list[WorktreeRow]:
    """Reapable rows in the current session older than ``before_iso``."""
    return await store.list_stale_worktrees(session_id=session_id, before_iso=before_iso)


async def rekey_row(
    store: DataStore,
    *,
    worktree_id: int,
    branch_name: str,
    new_path: str | None = None,
) -> WorktreeRow:
    """Promote a pre-branch row to a branch-keyed row."""
    return await store.rekey_worktree(
        worktree_id=worktree_id, branch_name=branch_name, new_path=new_path
    )


async def mark_status(
    store: DataStore,
    *,
    worktree_id: int,
    status: WorktreeStatus,
    failure_reason: str | None = None,
    head_sha: str | None = None,
    worktree_path: str | None = None,
) -> None:
    """Transition a row's status (idempotent — fine to call repeatedly)."""
    await store.mark_worktree_status(
        worktree_id=worktree_id,
        status=status,
        failure_reason=failure_reason,
        head_sha=head_sha,
        worktree_path=worktree_path,
    )


async def touch(store: DataStore, *, worktree_id: int, head_sha: str | None = None) -> None:
    """Bump ``last_used_at`` (and optionally ``head_sha``) for an active row."""
    await store.touch_worktree(worktree_id=worktree_id, head_sha=head_sha)


__all__ = [
    "WorktreeAllocationConflict",
    "WorktreeRow",
    "WorktreeStatus",
    "insert_worktree",
    "list_active",
    "list_orphans",
    "list_stale",
    "live_worktree_paths",
    "lookup_by_branch",
    "lookup_by_id",
    "lookup_by_prebranch_key",
    "mark_status",
    "rekey_row",
    "touch",
]
