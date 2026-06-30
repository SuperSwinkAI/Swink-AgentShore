"""DataStore mixin for the ``review_queue`` table."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from agentshore.data.store.base import _DataStoreBase, _serialized
from agentshore.data.store.rows import _row_to_review_queue
from agentshore.utils import now_iso

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentshore.data.models import ReviewQueueRecord


class _ReviewsMixin(_DataStoreBase):
    """Methods that operate on the ``review_queue`` table."""

    @_serialized
    async def enqueue_review(self, record: ReviewQueueRecord) -> int:
        """Insert a pending review into the queue (idempotent per PR+session).

        The partial unique index on ``(pr_number, session_id) WHERE status='pending'``
        handles dedup — a second enqueue for the same PR is silently ignored.
        Returns the ``queue_id`` of the inserted row (0 if ignored due to conflict).
        """
        async with self._conn.execute(
            """
            INSERT OR IGNORE INTO review_queue
                (pr_number, session_id, author_label, enqueued_at, status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (
                record.pr_number,
                record.session_id,
                record.author_label,
                record.enqueued_at,
            ),
        ) as cursor:
            await self._conn.commit()
            if cursor.rowcount == 0:
                return 0
            return cursor.lastrowid or 0

    @_serialized
    async def list_pending_reviews(self, session_id: str) -> list[ReviewQueueRecord]:
        """Return all pending reviews for a session, ordered by enqueue time."""
        cursor = await self._conn.execute(
            """
            SELECT queue_id, pr_number, session_id, author_label,
                   enqueued_at, status, claimed_by, claimed_at, completed_at
            FROM review_queue
            WHERE session_id = ? AND status = 'pending'
            ORDER BY enqueued_at ASC
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_review_queue(row) for row in rows]

    @_serialized
    async def claim_review(self, queue_id: int, agent_id: str) -> bool:
        """Atomically transition a pending review to 'claimed'.

        Returns True if the row was updated, False if already claimed/done.
        """
        async with self._conn.execute(
            """
            UPDATE review_queue
            SET status = 'claimed', claimed_by = ?, claimed_at = ?
            WHERE queue_id = ? AND status = 'pending'
            """,
            (agent_id, now_iso(), queue_id),
        ) as cursor:
            await self._conn.commit()
            return cursor.rowcount == 1

    @_serialized
    async def claim_pending_review_for_pr(
        self, session_id: str, pr_number: int, agent_id: str
    ) -> ReviewQueueRecord | None:
        """Claim the oldest pending review row for a PR, returning the claimed row."""
        now = now_iso()
        try:
            await self._conn.execute("BEGIN IMMEDIATE")
            async with self._conn.execute(
                """
                SELECT queue_id FROM review_queue
                WHERE session_id = ? AND pr_number = ? AND status = 'pending'
                ORDER BY enqueued_at ASC
                LIMIT 1
                """,
                (session_id, pr_number),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await self._conn.commit()
                return None

            queue_id = int(row["queue_id"])
            async with self._conn.execute(
                """
                UPDATE review_queue
                SET status = 'claimed', claimed_by = ?, claimed_at = ?
                WHERE queue_id = ? AND status = 'pending'
                """,
                (agent_id, now, queue_id),
            ) as cursor:
                if cursor.rowcount != 1:
                    await self._conn.rollback()
                    return None
            async with self._conn.execute(
                """
                SELECT queue_id, pr_number, session_id, author_label,
                       enqueued_at, status, claimed_by, claimed_at, completed_at
                FROM review_queue
                WHERE queue_id = ?
                """,
                (queue_id,),
            ) as cursor:
                claimed = await cursor.fetchone()
            await self._conn.commit()
            return _row_to_review_queue(claimed) if claimed is not None else None
        except sqlite3.IntegrityError:
            await self._conn.rollback()
            return None
        except Exception:
            await self._conn.rollback()
            raise

    @_serialized
    async def complete_review(self, queue_id: int) -> None:
        """Mark a claimed review as done."""
        await self._conn.execute(
            """
            UPDATE review_queue
            SET status = 'done', completed_at = ?
            WHERE queue_id = ?
            """,
            (now_iso(), queue_id),
        )
        await self._conn.commit()

    @_serialized
    async def drain_review_queue_for_prs(self, session_id: str, pr_numbers: Sequence[int]) -> int:
        """Mark pending/claimed review rows ``done`` for the given PRs in one pass.

        Used by the refresh-time reaper to drain rows for PRs that are no longer
        an AgentShore reviewer's responsibility — primarily PRs parked
        ``manual-required`` (handed to a human). Draining keeps ``review_queue``
        reflecting actually-reviewable work so the resolver/mask don't iterate
        dead rows each tick. Mirrors the queue drain in
        :meth:`mark_pull_request_absent`. Returns the number of rows drained.
        """
        pr_list = list(dict.fromkeys(pr_numbers))
        if not pr_list:
            return 0
        placeholders = ",".join("?" for _ in pr_list)
        async with self._conn.execute(
            f"UPDATE review_queue SET status = 'done', completed_at = ? "  # noqa: S608 (params bound)
            f"WHERE session_id = ? AND status IN ('pending', 'claimed') "
            f"AND pr_number IN ({placeholders})",
            (now_iso(), session_id, *pr_list),
        ) as cursor:
            await self._conn.commit()
            return cursor.rowcount or 0

    @_serialized
    async def complete_reviews_for_pr(self, session_id: str, pr_number: int) -> None:
        """Mark all pending/claimed review rows for a PR done."""
        await self._conn.execute(
            """
            UPDATE review_queue
            SET status = 'done', completed_at = ?
            WHERE session_id = ?
              AND pr_number = ?
              AND status IN ('pending', 'claimed')
            """,
            (now_iso(), session_id, pr_number),
        )
        await self._conn.commit()
