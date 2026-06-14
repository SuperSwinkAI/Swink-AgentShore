"""DataStore mixin for the ``plays`` table."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agentshore.data.store.base import (
    _ACTIVE_WORK_CLAIM_STATUSES,
    _DataStoreBase,
    _status_in_clause,
)
from agentshore.data.store.rows import _row_to_play_record
from agentshore.utils import now_iso

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentshore.data.models import PlayRecord
    from agentshore.state import JsonArtifact


class _PlaysMixin(_DataStoreBase):
    """Methods that operate on the ``plays`` table."""

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

    async def count_running_trunk_plays(
        self, session_id: str, *, exclude_play_id: int, play_types: Sequence[str]
    ) -> int:
        """Count still-running plays of the given types, excluding one play.

        "Running" == ``ended_at IS NULL`` (the row is inserted at dispatch with a
        NULL ``ended_at`` and stamped on completion). The per-play trunk-artifact
        reclaim hook calls this to detect a *concurrent* trunk-scoped play: when
        one exists, a newly-appeared root file's ownership is ambiguous across the
        overlapping plays (#162), so reclaim is deferred to the session-start
        sweep rather than risk pulling a sibling's in-flight artifact.
        """
        if not play_types:
            return 0
        placeholders = ",".join("?" for _ in play_types)
        async with self._conn.execute(
            f"""
            SELECT COUNT(*) FROM plays
             WHERE session_id = ?
               AND ended_at IS NULL
               AND play_id != ?
               AND play_type IN ({placeholders})
            """,
            (session_id, exclude_play_id, *play_types),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def list_trunk_play_windows(
        self, *, play_types: Sequence[str]
    ) -> list[tuple[int, str, str | None]]:
        """Return ``(play_id, started_at, ended_at)`` for plays of the given types.

        Spans **every** session — the session-start trunk-artifact sweep uses
        these windows to attribute leftover untracked root files to a closed
        trunk-scoped play by mtime, including a play killed in a prior session
        that never stamped ``ended_at`` (#164). ``ended_at`` is ``None`` for such
        rows.
        """
        if not play_types:
            return []
        placeholders = ",".join("?" for _ in play_types)
        async with self._conn.execute(
            f"""
            SELECT play_id, started_at, ended_at FROM plays
             WHERE play_type IN ({placeholders})
            """,
            tuple(play_types),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(int(r[0]), str(r[1]), (str(r[2]) if r[2] is not None else None)) for r in rows]

    async def session_play_totals(self, session_id: str) -> tuple[int, float]:
        """Return ``(play_count, total_dollar_cost)`` aggregated over a session.

        Single source for the session aggregate that ``complete_session``
        persists back onto the ``sessions`` row (#170), so any consumer trusting
        ``sessions.total_plays``/``total_cost`` (e.g. the archiver / manifest)
        gets the real values instead of the ``0``/``0.0`` creation defaults.
        ``SUM`` is ``COALESCE``-d so a session with no plays returns
        ``(0, 0.0)`` rather than ``(0, None)``.
        """
        async with self._conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(dollar_cost), 0.0)
              FROM plays
             WHERE session_id = ?
            """,
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return (0, 0.0)
        return (int(row[0]), float(row[1]))

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
