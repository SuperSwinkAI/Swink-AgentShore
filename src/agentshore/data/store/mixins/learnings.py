"""DataStore mixin for the ``session_learnings`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.data.store.rows import _row_to_learning
from agentshore.utils import now_iso

if TYPE_CHECKING:
    import aiosqlite

    from agentshore.data.models import SessionLearningRecord


class _LearningsMixin:
    """Methods that operate on the ``session_learnings`` table."""

    _db: aiosqlite.Connection | None
    _conn: aiosqlite.Connection

    if TYPE_CHECKING:
        # Provided by _DataStoreBase; visible to mypy via the MRO at runtime.
        async def _insert(self, table: str, **cols: object) -> int: ...

    async def record_learning(self, record: SessionLearningRecord) -> int:
        """Insert a session-learning record and return its ``learning_id``."""
        return await self._insert(
            "session_learnings",
            session_id=record.session_id,
            pattern=record.pattern,
            category=record.category,
            source_play_id=record.source_play_id,
            confidence=record.confidence,
            reinforcement_count=record.reinforcement_count,
            created_at=record.created_at,
            last_reinforced_at=record.last_reinforced_at,
        )

    async def reinforce_learning(self, learning_id: int) -> None:
        """Increment ``reinforcement_count`` and update ``last_reinforced_at``."""
        await self._conn.execute(
            """
            UPDATE session_learnings
            SET reinforcement_count = reinforcement_count + 1,
                last_reinforced_at = ?
            WHERE learning_id = ?
            """,
            (now_iso(), learning_id),
        )
        await self._conn.commit()

    async def count_learnings(self, session_id: str) -> int:
        """Return the total count of session_learnings rows for *session_id*."""
        async with self._conn.execute(
            "SELECT COUNT(*) FROM session_learnings WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def list_learnings(
        self,
        session_id: str,
        category: str | None = None,
        min_confidence: float = 0.0,
    ) -> list[SessionLearningRecord]:
        """Return session learnings, optionally filtered by category and confidence."""
        if category is not None:
            cursor = await self._conn.execute(
                """
                SELECT learning_id, session_id, pattern, category, source_play_id,
                       confidence, reinforcement_count, created_at, last_reinforced_at
                FROM session_learnings
                WHERE session_id = ? AND category = ? AND confidence >= ?
                ORDER BY reinforcement_count DESC, learning_id ASC
                """,
                (session_id, category, min_confidence),
            )
        else:
            cursor = await self._conn.execute(
                """
                SELECT learning_id, session_id, pattern, category, source_play_id,
                       confidence, reinforcement_count, created_at, last_reinforced_at
                FROM session_learnings
                WHERE session_id = ? AND confidence >= ?
                ORDER BY reinforcement_count DESC, learning_id ASC
                """,
                (session_id, min_confidence),
            )
        rows = await cursor.fetchall()
        return [_row_to_learning(row) for row in rows]
