"""DataStore mixin for the ``plays`` table."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agentshore.data.store.base import _ACTIVE_WORK_CLAIM_STATUSES, _status_in_clause
from agentshore.data.store.rows import _row_to_play_record
from agentshore.utils import now_iso

if TYPE_CHECKING:
    import aiosqlite

    from agentshore.data.models import PlayRecord
    from agentshore.state import JsonArtifact


class _PlaysMixin:
    """Methods that operate on the ``plays`` table."""

    _db: aiosqlite.Connection | None
    _conn: aiosqlite.Connection

    if TYPE_CHECKING:
        # Provided by _DataStoreBase; visible to mypy via the MRO at runtime.
        async def _insert(self, table: str, **cols: object) -> int: ...

    async def record_play(self, play: PlayRecord) -> int:
        """Insert a play record and return the auto-assigned ``play_id``."""
        return await self._insert(
            "plays",
            session_id=play.session_id,
            play_type=play.play_type,
            agent_id=play.agent_id,
            started_at=play.started_at,
            ended_at=play.ended_at,
            duration_ms=play.duration_ms,
            success=int(play.success),
            partial=int(play.partial),
            token_cost=play.token_cost,
            dollar_cost=play.dollar_cost,
            alignment_before=play.alignment_before,
            alignment_after=play.alignment_after,
            alignment_delta=play.alignment_delta,
            reward=play.reward,
            failure_category=play.failure_category,
            error=play.error,
            artifacts=json.dumps(play.artifacts) if play.artifacts else None,
        )

    async def update_play(
        self,
        play_id: int,
        *,
        success: bool,
        ended_at: str,
        duration_ms: int | None = None,
        partial: bool = False,
        token_cost: int = 0,
        dollar_cost: float = 0.0,
        alignment_before: float | None = None,
        alignment_after: float | None = None,
        alignment_delta: float | None = None,
        reward: float | None = None,
        failure_category: str | None = None,
        error: str | None = None,
        artifacts: list[JsonArtifact] | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Update the outcome fields of an already-inserted play row."""
        await self._conn.execute(
            """
            UPDATE plays
            SET success = ?, ended_at = ?, duration_ms = ?, partial = ?,
                token_cost = ?, dollar_cost = ?,
                alignment_before = COALESCE(?, alignment_before),
                alignment_after = ?, alignment_delta = ?, reward = ?,
                failure_category = ?, error = ?, artifacts = ?, agent_id = ?
            WHERE play_id = ?
            """,
            (
                int(success),
                ended_at,
                duration_ms,
                int(partial),
                token_cost,
                dollar_cost,
                alignment_before,
                alignment_after,
                alignment_delta,
                reward,
                failure_category,
                error,
                json.dumps(artifacts) if artifacts else None,
                agent_id,
                play_id,
            ),
        )
        await self._conn.commit()

    async def abandon_unfinished_plays(
        self,
        session_id: str,
        *,
        reason: str = "orphaned active play abandoned during recovery",
    ) -> None:
        """Close play rows left unfinished by a crashed or stopped session."""
        ended_at = now_iso()
        await self._conn.execute(
            """
            UPDATE plays
               SET ended_at = ?,
                   duration_ms = COALESCE(
                       duration_ms,
                       CAST((julianday(?) - julianday(started_at)) * 86400000 AS INTEGER)
                   ),
                   failure_category = COALESCE(failure_category, 'abandoned'),
                   error = COALESCE(error, ?)
             WHERE session_id = ?
               AND ended_at IS NULL
            """,
            (ended_at, ended_at, reason, session_id),
        )
        await self._conn.commit()

    async def abandon_work_for_missing_agents(
        self,
        session_id: str,
        active_agent_ids: list[str] | tuple[str, ...] | set[str] | frozenset[str],
        *,
        reason: str = "orphaned work abandoned because owning agent is gone",
    ) -> tuple[int, int]:
        """Abandon active claims and open play rows owned by agents no longer present."""
        agent_ids = sorted({str(agent_id) for agent_id in active_agent_ids if agent_id})
        ended_at = now_iso()
        status_clause, status_params = _status_in_clause(_ACTIVE_WORK_CLAIM_STATUSES)
        if agent_ids:
            agent_placeholders = ",".join("?" for _ in agent_ids)
            agent_filter = f"AND agent_id NOT IN ({agent_placeholders})"
            agent_params: tuple[str, ...] = tuple(agent_ids)
        else:
            agent_filter = ""
            agent_params = ()

        claim_cursor = await self._conn.execute(
            f"""
            UPDATE work_claims
               SET status = 'abandoned',
                   finished_at = COALESCE(finished_at, ?)
             WHERE session_id = ?
               AND agent_id IS NOT NULL
               AND {status_clause}
               {agent_filter}
            """,
            (ended_at, session_id, *status_params, *agent_params),
        )
        play_cursor = await self._conn.execute(
            f"""
            UPDATE plays
               SET ended_at = ?,
                   duration_ms = COALESCE(
                       duration_ms,
                       CAST((julianday(?) - julianday(started_at)) * 86400000 AS INTEGER)
                   ),
                   failure_category = COALESCE(failure_category, 'abandoned'),
                   error = COALESCE(error, ?)
             WHERE session_id = ?
               AND ended_at IS NULL
               AND agent_id IS NOT NULL
               {agent_filter}
            """,
            (ended_at, ended_at, reason, session_id, *agent_params),
        )
        await self._conn.commit()
        return (claim_cursor.rowcount, play_cursor.rowcount)

    async def get_play_history(self, session_id: str) -> list[PlayRecord]:
        """Return all plays for a session, ordered by play_id."""
        async with self._conn.execute(
            """
            SELECT play_id, session_id, play_type, agent_id, started_at,
                   ended_at, duration_ms, success, partial, token_cost,
                   dollar_cost, alignment_before, alignment_after,
                   alignment_delta, reward, failure_category, error, artifacts
            FROM plays
            WHERE session_id = ?
            ORDER BY play_id ASC
            """,
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_play_record(row) for row in rows]
