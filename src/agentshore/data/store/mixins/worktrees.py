"""DataStore mixin for the ``worktrees`` table.

Thin async helpers that translate ``aiosqlite`` rows into typed
``WorktreeRow`` dataclasses. The unique partial indexes on
``(session_id, branch_name)`` and ``(session_id, pre_branch_key)`` are the
concurrency guard; an ``IntegrityError`` from a concurrent insert is
surfaced as ``WorktreeAllocationConflict`` so the dispatcher can re-look
up the existing row.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from agentshore.data.models import (
    _VALID_WORKTREE_STATUSES,
    WorktreeRow,
    WorktreeStatus,
)
from agentshore.errors import OrchestratorError
from agentshore.utils import now_iso

if TYPE_CHECKING:
    import aiosqlite


class WorktreeAllocationConflict(OrchestratorError):
    """A concurrent insert hit the unique partial index."""

    error_type = "worktree_allocation_conflict"
    recoverable = True
    recovery_action = "re-lookup existing row, share the worktree"


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _row_to_worktree(row: sqlite3.Row | dict[str, object]) -> WorktreeRow:
    status = str(row["status"])
    if status not in _VALID_WORKTREE_STATUSES:
        msg = f"unexpected worktree status: {status!r}"
        raise ValueError(msg)
    return WorktreeRow(
        worktree_id=int(str(row["worktree_id"])),
        session_id=str(row["session_id"]),
        branch_name=_opt_str(row["branch_name"]),
        pre_branch_key=_opt_str(row["pre_branch_key"]),
        worktree_path=str(row["worktree_path"]),
        status=status,
        original_play_type=str(row["original_play_type"]),
        head_sha=_opt_str(row["head_sha"]),
        base_ref=str(row["base_ref"]),
        created_at=str(row["created_at"]),
        last_used_at=str(row["last_used_at"]),
        reaped_at=_opt_str(row["reaped_at"]),
        failure_reason=_opt_str(row["failure_reason"]),
    )


class _WorktreesMixin:
    """Methods that operate on the ``worktrees`` table."""

    _db: aiosqlite.Connection | None
    _conn: aiosqlite.Connection

    async def insert_worktree(
        self,
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
        if status not in _VALID_WORKTREE_STATUSES:
            msg = f"invalid worktree status: {status!r}"
            raise ValueError(msg)
        if branch_name is None and pre_branch_key is None:
            msg = "either branch_name or pre_branch_key must be set"
            raise ValueError(msg)
        ts = now_iso()
        try:
            async with self._conn.execute(
                """
                INSERT INTO worktrees (
                    session_id, branch_name, pre_branch_key, worktree_path, status,
                    original_play_type, head_sha, base_ref, created_at, last_used_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING worktree_id
                """,
                (
                    session_id,
                    branch_name,
                    pre_branch_key,
                    worktree_path,
                    status,
                    original_play_type,
                    head_sha,
                    base_ref,
                    ts,
                    ts,
                ),
            ) as cursor:
                row = await cursor.fetchone()
        except sqlite3.IntegrityError as exc:
            raise WorktreeAllocationConflict(
                f"worktree row collides on unique index: {exc}"
            ) from exc
        await self._conn.commit()
        if row is None:
            msg = "INSERT...RETURNING returned no row"
            raise RuntimeError(msg)
        return WorktreeRow(
            worktree_id=int(row["worktree_id"]),
            session_id=session_id,
            branch_name=branch_name,
            pre_branch_key=pre_branch_key,
            worktree_path=worktree_path,
            status=status,
            original_play_type=original_play_type,
            head_sha=head_sha,
            base_ref=base_ref,
            created_at=ts,
            last_used_at=ts,
            reaped_at=None,
            failure_reason=None,
        )

    async def lookup_worktree_by_branch(
        self,
        *,
        session_id: str,
        branch_name: str,
    ) -> WorktreeRow | None:
        """Return the active row for ``(session_id, branch_name)``, if any."""
        async with self._conn.execute(
            """
            SELECT * FROM worktrees
             WHERE session_id = ?
               AND branch_name = ?
               AND status IN ('active', 'reaping')
             ORDER BY worktree_id DESC
             LIMIT 1
            """,
            (session_id, branch_name),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_worktree(row) if row is not None else None

    async def lookup_worktree_by_prebranch_key(
        self,
        *,
        session_id: str,
        pre_branch_key: str,
    ) -> WorktreeRow | None:
        """Return the active row for ``(session_id, pre_branch_key)``, if any."""
        async with self._conn.execute(
            """
            SELECT * FROM worktrees
             WHERE session_id = ?
               AND pre_branch_key = ?
               AND status IN ('active', 'reaping')
             ORDER BY worktree_id DESC
             LIMIT 1
            """,
            (session_id, pre_branch_key),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_worktree(row) if row is not None else None

    async def lookup_worktree_by_id(
        self,
        *,
        worktree_id: int,
    ) -> WorktreeRow | None:
        """Return a row by primary key, regardless of status."""
        async with self._conn.execute(
            "SELECT * FROM worktrees WHERE worktree_id = ?",
            (worktree_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_worktree(row) if row is not None else None

    async def list_active_worktrees(self, *, session_id: str) -> list[WorktreeRow]:
        """Every row in ``active`` or ``reaping`` status for the session."""
        async with self._conn.execute(
            """
            SELECT * FROM worktrees
             WHERE session_id = ?
               AND status IN ('active', 'reaping')
             ORDER BY worktree_id ASC
            """,
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_worktree(r) for r in rows]

    async def list_orphan_worktrees(self, *, current_session_id: str) -> list[WorktreeRow]:
        """Rows from prior sessions that still hold an on-disk worktree.

        Includes ``active``, ``reaping``, and ``stale`` — a prior session that
        crashed mid-flight (active), crashed mid-reap (reaping), or completed a
        half-applied rekey (stale) all need cleanup at the next session start.
        ``failed`` is excluded: those rows are terminal markers from a previous
        reap that already gave up.
        """
        async with self._conn.execute(
            """
            SELECT * FROM worktrees
             WHERE session_id != ?
               AND status IN ('active', 'reaping', 'stale')
             ORDER BY worktree_id ASC
            """,
            (current_session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_worktree(r) for r in rows]

    async def all_known_worktree_paths(self) -> set[str]:
        """Every ``worktree_path`` recorded in any row, any status, any session.

        Used by ``reap_git_orphans`` to cross-reference ``git worktree list``
        against the DB and identify on-disk worktrees that have no row at all
        (the DB-recovery coupling fallout). Includes terminal rows
        (``reaped``/``failed``) intentionally: if a row exists in any state, the
        on-disk path is "known to AgentShore" and shouldn't be force-removed via
        the git-side reconciliation path.
        """
        async with self._conn.execute("SELECT worktree_path FROM worktrees") as cursor:
            rows = await cursor.fetchall()
        return {str(r[0]) for r in rows if r[0]}

    async def live_worktree_paths(self) -> set[str]:
        """Worktree paths from rows in non-terminal status across all sessions.

        Returns paths whose status is ``active``, ``reaping``, or ``stale`` —
        excludes ``reaped`` and ``failed``. Used by ``reap_git_orphans`` so a
        re-registered path can be cleaned up when its only DB rows are terminal
        markers from prior cleanups.
        """
        async with self._conn.execute(
            """
            SELECT worktree_path FROM worktrees
             WHERE status IN ('active', 'reaping', 'stale')
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return {str(r[0]) for r in rows if r[0]}

    async def list_stale_worktrees(self, *, session_id: str, before_iso: str) -> list[WorktreeRow]:
        """Reapable rows in the current session older than ``before_iso``.

        Includes both ``stale`` and ``reaping`` — a prior closed-PR reap that
        crashed before transitioning to ``reaped`` would leave the row in
        ``reaping`` forever otherwise. ``_reap_one`` is idempotent against that.
        """
        async with self._conn.execute(
            """
            SELECT * FROM worktrees
             WHERE session_id = ?
               AND status IN ('stale', 'reaping')
               AND last_used_at < ?
             ORDER BY worktree_id ASC
            """,
            (session_id, before_iso),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_worktree(r) for r in rows]

    async def rekey_worktree(
        self,
        *,
        worktree_id: int,
        branch_name: str,
        new_path: str | None = None,
    ) -> WorktreeRow:
        """Promote a pre-branch row to a branch-keyed row.

        Clears ``pre_branch_key`` and writes ``branch_name`` (and optionally the
        new path after the directory rename). Raises ``WorktreeAllocationConflict``
        if another active row already claims ``(session_id, branch_name)``.
        """
        ts = now_iso()
        try:
            if new_path is not None:
                await self._conn.execute(
                    """
                    UPDATE worktrees
                       SET branch_name = ?, pre_branch_key = NULL,
                           worktree_path = ?, last_used_at = ?
                     WHERE worktree_id = ?
                    """,
                    (branch_name, new_path, ts, worktree_id),
                )
            else:
                await self._conn.execute(
                    """
                    UPDATE worktrees
                       SET branch_name = ?, pre_branch_key = NULL, last_used_at = ?
                     WHERE worktree_id = ?
                    """,
                    (branch_name, ts, worktree_id),
                )
            await self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise WorktreeAllocationConflict(f"rekey collides on unique index: {exc}") from exc
        row = await self.lookup_worktree_by_id(worktree_id=worktree_id)
        if row is None:
            msg = f"worktree row {worktree_id} disappeared during rekey"
            raise RuntimeError(msg)
        return row

    async def mark_worktree_status(
        self,
        *,
        worktree_id: int,
        status: WorktreeStatus,
        failure_reason: str | None = None,
        head_sha: str | None = None,
        worktree_path: str | None = None,
    ) -> None:
        """Transition a row's status (idempotent — fine to call repeatedly).

        ``worktree_path`` is intentionally a separate kwarg from the others: it
        lets the rekey path point a now-stale row at the new on-disk location
        after a successful directory rename but failed DB rekey, so the next
        reap pass can find the worktree to remove.
        """
        if status not in _VALID_WORKTREE_STATUSES:
            msg = f"invalid worktree status: {status!r}"
            raise ValueError(msg)
        ts = now_iso()
        reaped_at = ts if status == "reaped" else None
        await self._conn.execute(
            """
            UPDATE worktrees
               SET status = ?,
                   failure_reason = COALESCE(?, failure_reason),
                   head_sha = COALESCE(?, head_sha),
                   worktree_path = COALESCE(?, worktree_path),
                   reaped_at = COALESCE(reaped_at, ?),
                   last_used_at = ?
             WHERE worktree_id = ?
            """,
            (status, failure_reason, head_sha, worktree_path, reaped_at, ts, worktree_id),
        )
        await self._conn.commit()

    async def touch_worktree(self, *, worktree_id: int, head_sha: str | None = None) -> None:
        """Bump ``last_used_at`` (and optionally ``head_sha``) for an active row."""
        await self._conn.execute(
            """
            UPDATE worktrees
               SET last_used_at = ?,
                   head_sha = COALESCE(?, head_sha)
             WHERE worktree_id = ?
            """,
            (now_iso(), head_sha, worktree_id),
        )
        await self._conn.commit()
