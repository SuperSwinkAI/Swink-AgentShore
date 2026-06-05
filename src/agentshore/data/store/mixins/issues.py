"""DataStore mixin for the ``github_issues`` table."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from agentshore.data.store.rows import _row_to_github_issue
from agentshore.utils import now_iso

if TYPE_CHECKING:
    import aiosqlite

    from agentshore.data.models import GitHubIssueRecord


class _IssuesMixin:
    """Methods that operate on the ``github_issues`` table."""

    _db: aiosqlite.Connection | None
    _conn: aiosqlite.Connection

    if TYPE_CHECKING:
        # Forward-declared cross-mixin method (impl in _WorkClaimsMixin).
        # Only visible to mypy; Python resolves the real method via MRO.
        async def supersede_work_claims(
            self,
            session_id: str,
            resource_keys: list[str] | tuple[str, ...],
        ) -> None: ...

    async def get_last_issue_sync_at(self, session_id: str) -> str | None:
        """Return the ISO8601 cursor for incremental issue sync, or None if unset.

        NULL means no sync has happened yet — the refresh path treats that as
        "do a full paginated sweep."
        """
        async with self._conn.execute(
            "SELECT last_issue_sync_at FROM sessions WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        value = row["last_issue_sync_at"]
        return str(value) if value is not None else None

    async def set_last_issue_sync_at(self, session_id: str, ts: str) -> None:
        """Advance the incremental sync cursor for ``session_id``."""
        await self._conn.execute(
            "UPDATE sessions SET last_issue_sync_at = ? WHERE session_id = ?",
            (ts, session_id),
        )
        await self._conn.commit()

    async def cache_github_issues(self, session_id: str, issues: list[GitHubIssueRecord]) -> None:
        """Bulk insert or update cached GitHub issues for a session.

        Uses an explicit ON CONFLICT upsert so that:
        - A reopened issue (incoming state='open') has its closed_at set to NULL rather
          than retaining the old closed timestamp from a prior close event.
        - All columns that could be enriched by AgentShore-side code in the future are
          explicitly listed, making it safe to add fields without changing the DELETE +
          INSERT behaviour that INSERT OR REPLACE would cause.
        """
        await self._conn.executemany(
            """
            INSERT INTO github_issues
                (issue_number, session_id, title, state, priority,
                 labels, source, url, created_at, closed_at, github_author)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(issue_number, session_id) DO UPDATE SET
                title      = excluded.title,
                state      = excluded.state,
                priority   = excluded.priority,
                labels     = excluded.labels,
                source     = COALESCE(excluded.source, github_issues.source),
                url        = COALESCE(excluded.url, github_issues.url),
                created_at = excluded.created_at,
                closed_at  = CASE WHEN excluded.state = 'open'
                                  THEN NULL
                                  ELSE COALESCE(excluded.closed_at, github_issues.closed_at)
                             END,
                github_author = COALESCE(excluded.github_author, github_issues.github_author)
            """,
            [
                (
                    issue.issue_number,
                    session_id,
                    issue.title,
                    issue.state,
                    issue.priority,
                    json.dumps(issue.labels) if issue.labels else None,
                    issue.source,
                    issue.url,
                    issue.created_at,
                    issue.closed_at,
                    issue.github_author,
                )
                for issue in issues
            ],
        )
        await self._conn.commit()

    async def get_open_issues(self, session_id: str) -> list[GitHubIssueRecord]:
        """Return all open issues for a session, ordered by priority ASC (nulls last)."""
        cursor = await self._conn.execute(
            """
            SELECT issue_number, session_id, title, state, priority,
                   labels, source, url, created_at, closed_at, github_author
            FROM github_issues
            WHERE session_id = ? AND LOWER(state) = 'open'
            ORDER BY
                CASE WHEN priority IS NULL THEN 1 ELSE 0 END,
                priority ASC
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_github_issue(row) for row in rows]

    async def get_github_issue(
        self, issue_number: int, session_id: str
    ) -> GitHubIssueRecord | None:
        """Return one cached GitHub issue by number for this session."""
        async with self._conn.execute(
            """
            SELECT issue_number, session_id, title, state, priority,
                   labels, source, url, created_at, closed_at, github_author
            FROM github_issues
            WHERE issue_number = ? AND session_id = ?
            """,
            (issue_number, session_id),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_github_issue(row) if row is not None else None

    async def list_all_issues(self, session_id: str) -> list[GitHubIssueRecord]:
        """Return all issues for a session (open and closed), ordered by issue number."""
        cursor = await self._conn.execute(
            """
            SELECT issue_number, session_id, title, state, priority,
                   labels, source, url, created_at, closed_at, github_author
            FROM github_issues
            WHERE session_id = ?
            ORDER BY issue_number ASC
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_github_issue(row) for row in rows]

    async def list_recently_closed_issues(
        self, session_id: str, *, hours: int = 24
    ) -> list[GitHubIssueRecord]:
        """Return closed issues whose ``closed_at`` is within the last *hours*.

        The cutoff is computed in Python and passed as an ISO 8601 string so
        it sorts lexically against ``closed_at`` values written by
        ``update_issue_state`` (also ``now_iso()``-formatted). SQLite's
        ``datetime('now')`` uses a different format and would not compare
        correctly.

        Returned rows carry ``state='closed'`` so the dashboard's existing
        kanban routing drops them straight into the Done column without
        needing a frontend change.
        """
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        cursor = await self._conn.execute(
            """
            SELECT issue_number, session_id, title, state, priority,
                   labels, source, url, created_at, closed_at, github_author
            FROM github_issues
            WHERE session_id = ?
              AND LOWER(state) = 'closed'
              AND closed_at IS NOT NULL
              AND closed_at > ?
            ORDER BY closed_at DESC
            """,
            (session_id, cutoff),
        )
        rows = await cursor.fetchall()
        return [_row_to_github_issue(row) for row in rows]

    async def update_issue_state(self, issue_number: int, session_id: str, state: str) -> None:
        """Update the state (and ``closed_at`` if closing) of a cached issue."""
        now = now_iso()
        await self._conn.execute(
            """
            UPDATE github_issues
            SET state = ?,
                closed_at = CASE WHEN ? = 'closed' THEN ? ELSE closed_at END
            WHERE issue_number = ? AND session_id = ?
            """,
            (state, state, now, issue_number, session_id),
        )
        await self._conn.commit()
        if state == "closed":
            await self.supersede_work_claims(session_id, [f"issue:{issue_number}"])

    async def update_issues_state_batch(
        self, issue_numbers: list[int], session_id: str, state: str
    ) -> None:
        """Update many cached issue rows in one transaction."""
        if not issue_numbers:
            return
        now = now_iso()
        await self._conn.executemany(
            """
            UPDATE github_issues
            SET state = ?,
                closed_at = CASE WHEN ? = 'closed' THEN ? ELSE closed_at END
            WHERE issue_number = ? AND session_id = ?
            """,
            [(state, state, now, issue_number, session_id) for issue_number in issue_numbers],
        )
        await self._conn.commit()
        if state == "closed":
            await self.supersede_work_claims(
                session_id,
                [f"issue:{issue_number}" for issue_number in issue_numbers],
            )

    async def add_issue_labels(
        self,
        issue_number: int,
        session_id: str,
        labels: list[str] | tuple[str, ...],
    ) -> None:
        """Add labels to one cached GitHub issue without waiting for refresh."""
        clean = [str(label) for label in labels if str(label)]
        if not clean:
            return
        issue = await self.get_github_issue(issue_number, session_id)
        if issue is None:
            return
        merged = sorted({*issue.labels, *clean})
        await self._conn.execute(
            """
            UPDATE github_issues
               SET labels = ?
             WHERE issue_number = ? AND session_id = ?
            """,
            (json.dumps(merged), issue_number, session_id),
        )
        await self._conn.commit()
