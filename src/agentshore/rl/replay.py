"""Offline replay loader for agentshore train.

Hydrates RolloutBuffers from stored rl_experience rows so PPOUpdater
can run offline training passes over historical session data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import structlog

from agentshore.rl.action_space import ACTION_SPACE_VERSION, NUM_ACTIONS
from agentshore.rl.experience import RolloutBuffer, Step
from agentshore.rl.observation import OBSERVATION_DIM

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agentshore.data.store import DataStore


_logger = structlog.get_logger(__name__)


class ReplayLoader:
    """Loads stored experience rows and hydrates RolloutBuffers per session.

    Only yields rows with matching action_space_version (and optionally
    config_hash) whose state_vector / next_state blobs are exactly
    OBSERVATION_DIM * 4 bytes (float32).
    """

    def __init__(
        self,
        store: DataStore,
        action_space_version: int = ACTION_SPACE_VERSION,
        config_hash: str | None = None,
    ) -> None:
        self._store = store
        self._action_space_version = action_space_version
        self._config_hash = config_hash

    async def load_session(self, session_id: str) -> RolloutBuffer:
        """Return a RolloutBuffer with all valid steps for *session_id*.

        Steps whose blob dimensions don't match are skipped with a warning.
        """
        buf = RolloutBuffer()
        expected_bytes = OBSERVATION_DIM * 4  # float32
        mask_bytes = NUM_ACTIONS  # bool stored as uint8

        n_loaded = 0
        n_skipped = 0
        async for rec in self._store.iter_experience_for_replay(
            session_id,
            self._action_space_version,
            self._config_hash,
        ):
            # Validate blob sizes
            if len(rec.state_vector) != expected_bytes:
                n_skipped += 1
                continue
            if rec.next_state is None or len(rec.next_state) != expected_bytes:
                n_skipped += 1
                continue

            state = np.frombuffer(rec.state_vector, dtype=np.float32).copy()
            next_state = np.frombuffer(rec.next_state, dtype=np.float32).copy()

            if rec.action_mask is not None and len(rec.action_mask) == mask_bytes:
                mask = np.frombuffer(rec.action_mask, dtype=np.uint8).astype(bool).copy()
            else:
                mask = np.ones(NUM_ACTIONS, dtype=bool)

            buf.add(
                Step(
                    state=state,
                    action=rec.action,
                    reward=rec.reward,
                    next_state=next_state,
                    done=bool(rec.done),
                    log_prob=rec.old_log_prob if rec.old_log_prob is not None else 0.0,
                    value=rec.value_estimate if rec.value_estimate is not None else 0.0,
                    mask=mask,
                )
            )
            n_loaded += 1

        if n_skipped:
            _logger.warning(
                "replay.skipped_rows",
                session_id=session_id,
                n_skipped=n_skipped,
                n_loaded=n_loaded,
            )

        return buf

    async def iter_compatible_sessions(self) -> AsyncIterator[str]:
        """Yield session IDs that have at least one compatible experience row."""
        async with self._store._conn.execute(
            """
            SELECT DISTINCT session_id
            FROM rl_experience
            WHERE action_space_version = ?
            ORDER BY session_id
            """,
            (self._action_space_version,),
        ) as cursor:
            rows = await cursor.fetchall()

        for row in rows:
            yield row[0]
