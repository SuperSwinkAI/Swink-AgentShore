"""DataStore mixin for the ``scope_drift_log`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.data.store.rows import _row_to_scope_drift

if TYPE_CHECKING:
    import aiosqlite

    from agentshore.data.models import ScopeDriftRecord


class _ScopeMixin:
    """Methods that operate on the ``scope_drift_log`` table."""

    _db: aiosqlite.Connection | None
    _conn: aiosqlite.Connection

    if TYPE_CHECKING:
        # Provided by _DataStoreBase; visible to mypy via the MRO at runtime.
        async def _insert(self, table: str, **cols: object) -> int: ...

    async def log_scope_drift(self, record: ScopeDriftRecord) -> None:
        """Insert a scope-drift log entry."""
        await self._insert(
            "scope_drift_log",
            session_id=record.session_id,
            play_id=record.play_id,
            artifact=record.artifact,
            reason=record.reason,
            logged_at=record.logged_at,
        )

    async def list_scope_drift(self, session_id: str) -> list[ScopeDriftRecord]:
        """Return all scope-drift entries for a session, ordered by ``logged_at`` ascending."""
        cursor = await self._conn.execute(
            """
            SELECT drift_id, session_id, play_id, artifact, reason, logged_at
            FROM scope_drift_log
            WHERE session_id = ?
            ORDER BY logged_at ASC
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_scope_drift(row) for row in rows]
