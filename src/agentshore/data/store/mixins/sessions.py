"""DataStore mixin for the ``sessions`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.data.store.rows import _row_to_session_record
from agentshore.utils import now_iso

if TYPE_CHECKING:
    import aiosqlite

    from agentshore.data.models import SessionRecord


class _SessionsMixin:
    """Methods that operate on the ``sessions`` table."""

    _db: aiosqlite.Connection | None
    # ``_conn`` is a property on ``_DataStoreBase`` that returns a non-None
    # connection or raises. Mixins access it through ``self._conn`` and Python
    # resolves the call through the MRO at runtime; the annotation tells mypy
    # what it returns.
    _conn: aiosqlite.Connection

    async def create_session(self, session: SessionRecord) -> None:
        """Insert a new session row."""
        async with self._conn.execute(
            """
            INSERT INTO sessions
                (session_id, project_path, started_at, ended_at, status,
                 seed_path, initial_issue_count,
                 total_cost, total_plays, scope_estimate, scope_remaining,
                 final_alignment)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.session_id,
                session.project_path,
                session.started_at,
                session.ended_at,
                session.status,
                session.seed_path,
                session.initial_issue_count,
                session.total_cost,
                session.total_plays,
                session.scope_estimate,
                session.scope_remaining,
                session.final_alignment,
            ),
        ):
            pass
        await self._conn.commit()

    async def get_session(self, session_id: str) -> SessionRecord | None:
        """Return a single session by ID, or ``None`` if not found."""
        async with self._conn.execute(
            """
            SELECT session_id, project_path, started_at, ended_at, status,
                   seed_path, initial_issue_count, total_cost, total_plays,
                   scope_estimate, scope_remaining, final_alignment
            FROM sessions
            WHERE session_id = ?
            """,
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_session_record(row)

    async def list_sessions(self) -> list[SessionRecord]:
        """Return all sessions, ordered by ``created_at`` descending."""
        cursor = await self._conn.execute(
            """
            SELECT session_id, project_path, started_at, ended_at, status,
                   seed_path, initial_issue_count, total_cost, total_plays,
                   scope_estimate, scope_remaining, final_alignment
            FROM sessions
            ORDER BY started_at DESC
            """
        )
        rows = await cursor.fetchall()
        return [_row_to_session_record(row) for row in rows]

    async def update_session_state(self, session_id: str, status: str) -> None:
        """Update the lifecycle status of a session (e.g. 'paused', 'running')."""
        await self._conn.execute(
            "UPDATE sessions SET status = ? WHERE session_id = ?",
            (status, session_id),
        )
        await self._conn.commit()

    async def complete_session(self, session_id: str, final_alignment: float) -> None:
        """Mark a session as completed."""
        await self._conn.execute(
            """
            UPDATE sessions
            SET status = 'completed',
                ended_at = ?,
                final_alignment = ?
            WHERE session_id = ?
            """,
            (now_iso(), final_alignment, session_id),
        )
        await self._conn.commit()

    async def fail_session(self, session_id: str, reason: str) -> None:
        """Finalize a session that crashed or was force-terminated.

        Writes a terminal ``failed`` status + ``ended_at`` so a crashed session
        does not linger as ``running`` forever (the orchestrator run loop dying
        skips the graceful ``complete_session`` path). ``reason`` is logged by
        the caller rather than stored — there is no ``failure_reason`` column and
        this is deliberately schema-free.

        Idempotent and race-safe: the ``ended_at IS NULL`` guard means a session
        already finalized by the normal stop path (``complete_session``) is left
        untouched, so a done-callback firing after a clean stop is a no-op.
        """
        await self._conn.execute(
            """
            UPDATE sessions
            SET status = 'failed', ended_at = ?
            WHERE session_id = ? AND ended_at IS NULL
            """,
            (now_iso(), session_id),
        )
        await self._conn.commit()
