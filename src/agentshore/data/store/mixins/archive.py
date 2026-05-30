"""DataStore mixin for the ``session_archives`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.data.store.rows import _row_to_archive_record

if TYPE_CHECKING:
    import aiosqlite

    from agentshore.data.models import ArchiveRecord


class _ArchiveMixin:
    """Methods that operate on the ``session_archives`` table."""

    _db: aiosqlite.Connection | None
    _conn: aiosqlite.Connection

    async def create_archive(self, record: ArchiveRecord) -> None:
        """Insert an archive record."""
        await self._conn.execute(
            """
            INSERT INTO session_archives
                (archive_id, session_id, archive_path,
                 total_cost, final_alignment, total_plays,
                 issues_closed, issues_created, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.archive_id,
                record.session_id,
                record.archive_path,
                record.total_cost,
                record.final_alignment,
                record.total_plays,
                record.issues_closed,
                record.issues_created,
                record.created_at,
            ),
        )
        await self._conn.commit()

    async def list_archives(self) -> list[ArchiveRecord]:
        """Return all archives, ordered by ``created_at`` descending."""
        cursor = await self._conn.execute(
            """
            SELECT archive_id, session_id, archive_path,
                   total_cost, final_alignment, total_plays,
                   issues_closed, issues_created, created_at
            FROM session_archives
            ORDER BY created_at DESC
            """
        )
        rows = await cursor.fetchall()
        return [_row_to_archive_record(row) for row in rows]

    async def get_archive(self, archive_id: str) -> ArchiveRecord | None:
        """Return a single archive by ID, or ``None`` if not found."""
        async with self._conn.execute(
            """
            SELECT archive_id, session_id, archive_path,
                   total_cost, final_alignment, total_plays,
                   issues_closed, issues_created, created_at
            FROM session_archives
            WHERE archive_id = ?
            """,
            (archive_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_archive_record(row)
