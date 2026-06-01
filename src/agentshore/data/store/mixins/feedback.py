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

    if TYPE_CHECKING:
        # Provided by _DataStoreBase; visible to mypy via the MRO at runtime.
        async def _insert(self, table: str, **cols: object) -> int: ...

    async def record_human_feedback(self, record: HumanFeedbackRecord) -> int:
        """Insert a human-feedback checkpoint row and return its ``feedback_id``."""
        return await self._insert(
            "human_feedback",
            session_id=record.session_id,
            play_id=record.play_id,
            trigger=record.trigger,
            feedback_text=record.feedback_text,
            action_taken=record.action_taken,
            created_at=record.created_at,
        )

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
