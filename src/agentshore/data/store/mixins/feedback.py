"""DataStore mixin for the ``human_feedback`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.data.store.rows import _row_to_human_feedback

if TYPE_CHECKING:
    import aiosqlite

    from agentshore.data.models import HumanFeedbackRecord


class _FeedbackMixin:
    """Methods that operate on the ``human_feedback`` table."""

    _db: aiosqlite.Connection | None
    _conn: aiosqlite.Connection

    async def record_human_feedback(self, record: HumanFeedbackRecord) -> int:
        """Insert a human-feedback checkpoint row and return its ``feedback_id``."""
        cursor = await self._conn.execute(
            """
            INSERT INTO human_feedback
                (session_id, play_id, trigger, feedback_text, action_taken, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record.session_id,
                record.play_id,
                record.trigger,
                record.feedback_text,
                record.action_taken,
                record.created_at,
            ),
        )
        await self._conn.commit()
        if cursor.lastrowid is None:
            msg = "INSERT did not return a row ID"
            raise RuntimeError(msg)
        return cursor.lastrowid

    async def list_human_feedback(self, session_id: str) -> list[HumanFeedbackRecord]:
        """Return all human-feedback rows for *session_id*."""
        cursor = await self._conn.execute(
            """
            SELECT feedback_id, session_id, play_id, trigger,
                   feedback_text, action_taken, created_at
            FROM human_feedback
            WHERE session_id = ?
            ORDER BY feedback_id ASC
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_human_feedback(row) for row in rows]

    async def count_human_feedback(self, session_id: str) -> int:
        """Return the total count of human-feedback rows for *session_id*."""
        async with self._conn.execute(
            "SELECT COUNT(*) FROM human_feedback WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0
