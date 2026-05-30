"""DataStore mixin for the ``review_feedback_patterns`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.data.store.rows import _row_to_review_feedback_pattern

if TYPE_CHECKING:
    import aiosqlite

    from agentshore.data.models import ReviewFeedbackPatternRecord


class _ReviewPatternsMixin:
    """Methods that operate on the ``review_feedback_patterns`` table."""

    _db: aiosqlite.Connection | None
    _conn: aiosqlite.Connection

    async def record_review_pattern(self, record: ReviewFeedbackPatternRecord) -> None:
        """Insert or accumulate a single review-feedback pattern row.

        If a row with the same (session_id, pattern, category) already exists,
        its frequency is incremented rather than creating a duplicate.

        Prefer :meth:`record_review_patterns` when persisting multiple rows
        from the same play — that batched path issues one ``executemany`` and
        one ``commit`` instead of N round-trips.
        """
        await self._conn.execute(
            """
            INSERT INTO review_feedback_patterns
                (session_id, play_id, pattern, category, frequency, injected, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, pattern, category)
            DO UPDATE SET frequency = frequency + excluded.frequency
            """,
            (
                record.session_id,
                record.play_id,
                record.pattern,
                record.category,
                record.frequency,
                int(record.injected),
                record.created_at,
            ),
        )
        await self._conn.commit()

    async def record_review_patterns(self, records: list[ReviewFeedbackPatternRecord]) -> None:
        """Bulk-insert review-feedback patterns in a single round-trip.

        One ``executemany`` plus one ``commit`` for the whole list, replacing
        the N inserts + N commits that the single-row path would issue.
        """
        if not records:
            return
        await self._conn.executemany(
            """
            INSERT INTO review_feedback_patterns
                (session_id, play_id, pattern, category, frequency, injected, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, pattern, category)
            DO UPDATE SET frequency = frequency + excluded.frequency
            """,
            [
                (
                    r.session_id,
                    r.play_id,
                    r.pattern,
                    r.category,
                    r.frequency,
                    int(r.injected),
                    r.created_at,
                )
                for r in records
            ],
        )
        await self._conn.commit()

    async def list_review_patterns(self, session_id: str) -> list[ReviewFeedbackPatternRecord]:
        """Return all review-feedback patterns for a session, ordered by frequency DESC."""
        cursor = await self._conn.execute(
            """
            SELECT pattern_id, session_id, play_id, pattern, category,
                   frequency, injected, created_at
            FROM review_feedback_patterns
            WHERE session_id = ?
            ORDER BY frequency DESC
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_review_feedback_pattern(row) for row in rows]

    async def mark_review_patterns_injected(
        self,
        session_id: str,
        pattern_ids: list[int],
    ) -> None:
        """Mark selected review-feedback patterns as injected into prompts."""
        if not pattern_ids:
            return
        placeholders = ",".join("?" for _ in pattern_ids)
        await self._conn.execute(
            f"""
            UPDATE review_feedback_patterns
            SET injected = 1
            WHERE session_id = ?
              AND pattern_id IN ({placeholders})
            """,
            (session_id, *pattern_ids),
        )
        await self._conn.commit()
