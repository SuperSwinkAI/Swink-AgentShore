"""DataStore mixin for the ``branch_activity`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.utils import now_iso

if TYPE_CHECKING:
    import aiosqlite


class _BranchActivityMixin:
    """Methods that operate on the ``branch_activity`` table."""

    _db: aiosqlite.Connection | None
    _conn: aiosqlite.Connection

    async def rebuild_branch_activity(self, session_id: str, branch_pr_map: dict[str, int]) -> None:
        """Reconstitute skeleton branch_activity rows from open PRs after a session reset.

        Inserts one row per branch with agent_id=NULL (attribution unknown).
        Existing rows are left untouched so any same-session agent writes win.
        """
        now = now_iso()
        await self._conn.executemany(
            """
            INSERT INTO branch_activity
                (branch, session_id, last_implementer_agent_id, last_commit_sha, updated_at)
            VALUES (?, ?, NULL, NULL, ?)
            ON CONFLICT(branch, session_id) DO NOTHING
            """,
            [(branch, session_id, now) for branch in branch_pr_map],
        )
        await self._conn.commit()

    async def update_branch_activity(
        self,
        branch: str,
        session_id: str,
        agent_id: str,
        sha: str | None = None,
    ) -> None:
        """Record or update which agent last committed to *branch*."""
        now = now_iso()
        await self._conn.execute(
            """
            INSERT INTO branch_activity
                (branch, session_id, last_implementer_agent_id, last_commit_sha, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(branch, session_id) DO UPDATE SET
                last_implementer_agent_id = excluded.last_implementer_agent_id,
                last_commit_sha = excluded.last_commit_sha,
                updated_at = excluded.updated_at
            """,
            (branch, session_id, agent_id, sha, now),
        )
        await self._conn.commit()

    async def get_last_implementer(self, branch: str, session_id: str) -> str | None:
        """Return the agent_id that last committed to *branch*, or None."""
        async with self._conn.execute(
            """
            SELECT last_implementer_agent_id FROM branch_activity
            WHERE branch = ? AND session_id = ?
            """,
            (branch, session_id),
        ) as cursor:
            row = await cursor.fetchone()
            return row["last_implementer_agent_id"] if row else None
