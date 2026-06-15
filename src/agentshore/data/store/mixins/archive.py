"""DataStore mixin for the ``session_archives`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.data.store.base import _DataStoreBase
from agentshore.data.store.rows import _row_to_archive_record

if TYPE_CHECKING:
    from agentshore.data.models import ArchiveRecord


class _ArchiveMixin(_DataStoreBase):
    """Methods that operate on the ``session_archives`` table."""

    async def create_archive(self, record: ArchiveRecord) -> None:
        """Insert an archive record."""
        await self._insert(
            "session_archives",
            archive_id=record.archive_id,
            session_id=record.session_id,
            archive_path=record.archive_path,
            total_cost=record.total_cost,
            final_alignment=record.final_alignment,
            total_plays=record.total_plays,
            issues_closed=record.issues_closed,
            issues_created=record.issues_created,
            created_at=record.created_at,
        )

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
