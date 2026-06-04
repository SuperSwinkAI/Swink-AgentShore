"""DataStore mixin for the ``trajectory_snapshots`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.data.store.rows import _row_to_trajectory

if TYPE_CHECKING:
    import aiosqlite

    from agentshore.data.models import TrajectorySnapshotRecord


class _TrajectoryMixin:
    """Methods that operate on the ``trajectory_snapshots`` table."""

    _db: aiosqlite.Connection | None
    _conn: aiosqlite.Connection

    if TYPE_CHECKING:
        # Provided by _DataStoreBase; visible to mypy via the MRO at runtime.
        async def _insert(self, table: str, **cols: object) -> int: ...

    async def record_trajectory_snapshot(self, record: TrajectorySnapshotRecord) -> None:
        """Insert a trajectory snapshot."""
        await self._insert(
            "trajectory_snapshots",
            session_id=record.session_id,
            play_id=record.play_id,
            projected_alignment_at_budget_end=record.projected_alignment_at_budget_end,
            estimated_remaining_plays=record.estimated_remaining_plays,
            estimated_remaining_cost=record.estimated_remaining_cost,
            created_at=record.created_at,
        )

    async def get_latest_trajectory(self, session_id: str) -> TrajectorySnapshotRecord | None:
        """Return the most recent trajectory snapshot for *session_id*, or None."""
        async with self._conn.execute(
            """
            SELECT snapshot_id, session_id, play_id,
                   projected_alignment_at_budget_end, estimated_remaining_plays,
                   estimated_remaining_cost, created_at
            FROM trajectory_snapshots
            WHERE session_id = ?
            ORDER BY snapshot_id DESC
            LIMIT 1
            """,
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_trajectory(row) if row else None

    async def list_trajectory_snapshots(self, session_id: str) -> list[TrajectorySnapshotRecord]:
        """Return all trajectory snapshots for a session, ordered by ``play_id`` ascending."""
        cursor = await self._conn.execute(
            """
            SELECT snapshot_id, session_id, play_id,
                   projected_alignment_at_budget_end, estimated_remaining_plays,
                   estimated_remaining_cost, created_at
            FROM trajectory_snapshots
            WHERE session_id = ?
            ORDER BY play_id ASC
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_trajectory(row) for row in rows]
