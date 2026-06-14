"""DataStore mixin for the ``sessions`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.data.store.base import _DataStoreBase
from agentshore.data.store.rows import _row_to_session_record
from agentshore.utils import now_iso

if TYPE_CHECKING:
    from agentshore.data.models import SessionRecord


class _SessionsMixin(_DataStoreBase):
    """Methods that operate on the ``sessions`` table."""

    if TYPE_CHECKING:
        # Provided by _PlaysMixin (a sibling mixin, not the base); resolved via
        # the DataStore MRO at runtime.
        async def session_play_totals(self, session_id: str) -> tuple[int, float]: ...

    async def create_session(self, session: SessionRecord) -> None:
        """Insert a new session row."""
        await self._insert(
            "sessions",
            session_id=session.session_id,
            project_path=session.project_path,
            started_at=session.started_at,
            ended_at=session.ended_at,
            status=session.status,
            seed_path=session.seed_path,
            initial_issue_count=session.initial_issue_count,
            total_cost=session.total_cost,
            total_plays=session.total_plays,
            scope_estimate=session.scope_estimate,
            scope_remaining=session.scope_remaining,
            final_alignment=session.final_alignment,
        )

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
        """Mark a session as completed and persist the finalized play aggregate.

        ``total_plays``/``total_cost`` are derived from the ``plays`` table in
        the same transaction, so a completed session no longer reports
        ``0``/``0.0`` despite a fully-populated play history (#170). The
        archiver and manifest read these columns straight off the row, so
        deriving here keeps every ``sessions.total_*`` consumer correct rather
        than only the report (which already recomputes from ``plays``).
        """
        total_plays, total_cost = await self.session_play_totals(session_id)
        await self._conn.execute(
            """
            UPDATE sessions
            SET status = 'completed',
                ended_at = ?,
                final_alignment = ?,
                total_plays = ?,
                total_cost = ?
            WHERE session_id = ?
            """,
            (now_iso(), final_alignment, total_plays, total_cost, session_id),
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
