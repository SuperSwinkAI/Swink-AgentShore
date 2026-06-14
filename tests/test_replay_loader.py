from __future__ import annotations

import numpy as np
import pytest

from agentshore.data.models import ExperienceRecord
from agentshore.rl.action_space import ACTION_SPACE_VERSION, NUM_ACTIONS
from agentshore.rl.observation import OBSERVATION_DIM
from agentshore.rl.replay import ReplayLoader


class _ReplayStore:
    def __init__(self, records: list[ExperienceRecord]) -> None:
        self._records = records

    async def iter_experience_for_replay(
        self,
        session_id: str,
        action_space_version: int,
        config_hash: str | None,
    ):
        for record in self._records:
            if (
                record.session_id == session_id
                and record.action_space_version == action_space_version
                and (config_hash is None or record.config_hash == config_hash)
            ):
                yield record


def _record(play_id: int, *, action_mask: bytes | None) -> ExperienceRecord:
    vector = np.zeros(OBSERVATION_DIM, dtype=np.float32).tobytes()
    return ExperienceRecord(
        session_id="s1",
        play_id=play_id,
        state_vector=vector,
        action=0,
        reward=1.0,
        next_state=vector,
        done=0,
        action_space_version=ACTION_SPACE_VERSION,
        action_mask=action_mask,
    )


@pytest.mark.asyncio
async def test_replay_loader_skips_missing_or_short_action_masks() -> None:
    valid_mask = np.ones(NUM_ACTIONS, dtype=np.uint8).tobytes()
    store = _ReplayStore(
        [
            _record(1, action_mask=None),
            _record(2, action_mask=b"\x01"),
            _record(3, action_mask=valid_mask),
        ]
    )

    buffer = await ReplayLoader(store).load_session("s1")  # type: ignore[arg-type]

    assert len(buffer) == 1
