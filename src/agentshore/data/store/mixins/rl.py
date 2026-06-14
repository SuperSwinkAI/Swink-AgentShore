"""DataStore mixin for the ``rl_experience`` and ``policy_checkpoints`` tables."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.data.store.base import _DataStoreBase
from agentshore.data.store.helpers import _RL_EXPERIENCE_SELECT
from agentshore.data.store.rows import _row_to_checkpoint, _row_to_experience

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agentshore.data.models import CheckpointRecord, ExperienceRecord


class _RLMixin(_DataStoreBase):
    """Methods that operate on ``rl_experience`` and ``policy_checkpoints``."""

    async def record_experience(self, record: ExperienceRecord) -> int:
        """Insert a PPO experience row and return the auto-assigned experience_id."""
        return await self._insert(
            "rl_experience",
            session_id=record.session_id,
            play_id=record.play_id,
            state_vector=record.state_vector,
            action=record.action,
            reward=record.reward,
            next_state=record.next_state,
            done=record.done,
            old_log_prob=record.old_log_prob,
            value_estimate=record.value_estimate,
            action_mask=record.action_mask,
            mask_reason=record.mask_reason,
            policy_version=record.policy_version,
            action_space_version=record.action_space_version,
            config_hash=record.config_hash,
            step_index=record.step_index,
        )

    async def save_checkpoint(self, record: CheckpointRecord) -> int:
        """Insert a policy checkpoint row and return the auto-assigned checkpoint_id."""
        return await self._insert(
            "policy_checkpoints",
            session_id=record.session_id,
            created_at=record.created_at,
            play_count=record.play_count,
            weights_path=record.weights_path,
            avg_reward=record.avg_reward,
        )

    async def load_latest_checkpoint(
        self, session_id: str | None = None
    ) -> CheckpointRecord | None:
        """Return the most recent checkpoint, optionally filtered by session_id."""
        if session_id is not None:
            async with self._conn.execute(
                """
                SELECT checkpoint_id, session_id, created_at, play_count,
                       weights_path, avg_reward
                FROM policy_checkpoints
                WHERE session_id = ?
                ORDER BY play_count DESC
                LIMIT 1
                """,
                (session_id,),
            ) as cursor:
                row = await cursor.fetchone()
        else:
            async with self._conn.execute(
                """
                SELECT checkpoint_id, session_id, created_at, play_count,
                       weights_path, avg_reward
                FROM policy_checkpoints
                ORDER BY play_count DESC
                LIMIT 1
                """
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_checkpoint(row)

    async def iter_experience_for_replay(
        self,
        session_id: str,
        action_space_version: int,
        config_hash: str | None = None,
    ) -> AsyncIterator[ExperienceRecord]:
        """Yield experience rows for a session in step_index order.

        Filters by action_space_version; optionally also by config_hash.
        Rows whose state_vector blob length doesn't match are still yielded
        (the caller is responsible for schema validation).
        """
        params: tuple[object, ...] = (session_id, action_space_version)
        config_hash_clause = ""
        if config_hash is not None:
            config_hash_clause = "AND config_hash = ?"
            params = (*params, config_hash)
        async with self._conn.execute(
            f"""
            {_RL_EXPERIENCE_SELECT}
            WHERE session_id = ?
              AND action_space_version = ?
              {config_hash_clause}
            ORDER BY step_index ASC
            """,
            params,
        ) as cursor:
            async for row in cursor:
                yield _row_to_experience(row)

    async def distinct_experience_session_ids(self, action_space_version: int) -> list[str]:
        """Return session IDs with at least one experience row at this version.

        Ordered by session_id so replay enumeration is deterministic.
        """
        async with self._conn.execute(
            """
            SELECT DISTINCT session_id
            FROM rl_experience
            WHERE action_space_version = ?
            ORDER BY session_id
            """,
            (action_space_version,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [row[0] for row in rows]
