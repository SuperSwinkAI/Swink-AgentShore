"""DataStore mixin for the ``agents`` and ``agent_handoffs`` tables."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.data.store.rows import _row_to_agent_record, _row_to_handoff_record

if TYPE_CHECKING:
    import aiosqlite

    from agentshore.data.models import AgentRecord, HandoffRecord


class _AgentsMixin:
    """Methods that operate on the ``agents`` and ``agent_handoffs`` tables."""

    _db: aiosqlite.Connection | None
    _conn: aiosqlite.Connection

    async def register_agent(self, agent: AgentRecord) -> None:
        """Insert a new agent row."""
        await self._conn.execute(
            """
            INSERT INTO agents
                (agent_id, session_id, agent_type, created_at, terminated_at,
                 total_tokens, total_cost, tasks_completed, tasks_failed,
                 model_tier, display_name, dispatch_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent.agent_id,
                agent.session_id,
                agent.agent_type,
                agent.created_at,
                agent.terminated_at,
                agent.total_tokens,
                agent.total_cost,
                agent.tasks_completed,
                agent.tasks_failed,
                agent.model_tier,
                agent.display_name,
                agent.dispatch_count,
            ),
        )
        await self._conn.commit()

    async def update_agent_stats(self, agent_id: str, tokens: int, cost: float) -> None:
        """Increment an agent's cumulative token and cost counters."""
        await self._conn.execute(
            """
            UPDATE agents
            SET total_tokens = total_tokens + ?,
                total_cost = total_cost + ?
            WHERE agent_id = ?
            """,
            (tokens, cost, agent_id),
        )
        await self._conn.commit()

    async def update_agent_terminated(self, agent_id: str, terminated_at: str) -> None:
        """Set the termination timestamp for an agent."""
        await self._conn.execute(
            "UPDATE agents SET terminated_at = ? WHERE agent_id = ?",
            (terminated_at, agent_id),
        )
        await self._conn.commit()

    async def increment_agent_tasks(
        self, agent_id: str, *, completed: int = 0, failed: int = 0
    ) -> None:
        """Increment task completion/failure counters for an agent."""
        await self._conn.execute(
            """
            UPDATE agents
            SET tasks_completed = tasks_completed + ?,
                tasks_failed = tasks_failed + ?
            WHERE agent_id = ?
            """,
            (completed, failed, agent_id),
        )
        await self._conn.commit()

    async def increment_agent_dispatch_count(self, agent_id: str) -> None:
        """Increment the cumulative dispatch counter for an agent (desktop-31h2).

        Called at dispatch-claim time regardless of outcome — distinct from
        ``increment_agent_tasks`` which gates on the play's success/failure
        verdict. ``dispatch_share`` (computed from this counter in the
        reports collector) lets operators spot fleet-utilisation imbalance
        where some agents get 0 plays for long stretches.
        """
        await self._conn.execute(
            """
            UPDATE agents
            SET dispatch_count = dispatch_count + 1
            WHERE agent_id = ?
            """,
            (agent_id,),
        )
        await self._conn.commit()

    async def get_agents(self, session_id: str) -> list[AgentRecord]:
        """Return all agents for a session, ordered by ``created_at`` ascending."""
        cursor = await self._conn.execute(
            """
            SELECT agent_id, session_id, agent_type, created_at, terminated_at,
                   total_tokens, total_cost, tasks_completed, tasks_failed,
                   model_tier, display_name, dispatch_count
            FROM agents
            WHERE session_id = ?
            ORDER BY created_at ASC
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_agent_record(row) for row in rows]

    async def record_handoff(self, handoff: HandoffRecord) -> None:
        """Insert a Switch or Fresh-Start handoff record."""
        await self._conn.execute(
            """
            INSERT INTO agent_handoffs
                (session_id, play_id, source_agent_id, target_agent_id,
                 context_tokens_transferred, ramp_up_duration_ms, context_loss_estimate)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                handoff.session_id,
                handoff.play_id,
                handoff.source_agent_id,
                handoff.target_agent_id,
                handoff.context_tokens_transferred,
                handoff.ramp_up_duration_ms,
                handoff.context_loss_estimate,
            ),
        )
        await self._conn.commit()

    async def list_handoffs(self, session_id: str, *, limit: int = 100) -> list[HandoffRecord]:
        """Return most-recent handoff rows for a session, oldest-first."""
        fetch_limit = max(1, limit)
        cursor = await self._conn.execute(
            """
            SELECT session_id, play_id, source_agent_id, target_agent_id,
                   context_tokens_transferred, ramp_up_duration_ms, context_loss_estimate
            FROM agent_handoffs
            WHERE session_id = ?
            ORDER BY handoff_id DESC
            LIMIT ?
            """,
            (session_id, fetch_limit),
        )
        rows = list(await cursor.fetchall())
        rows.reverse()
        return [_row_to_handoff_record(row) for row in rows]
