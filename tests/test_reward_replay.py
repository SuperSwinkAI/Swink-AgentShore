"""scripts.reward_replay: decode stays in step with the observation layout and reward."""

from __future__ import annotations

import dataclasses

import pytest
from scripts.reward_replay import current_concurrency, decode_counts

from agentshore.config import RewardConfig
from agentshore.rl.observation import encode_observation
from agentshore.rl.reward import RewardSignals, compute_reward
from agentshore.state import AgentStatus, PlayType
from tests.test_rl_observation import _NULL_CTX, _agent, _state


@pytest.mark.parametrize(("busy", "idle"), [(0, 0), (3, 2), (9, 1), (14, 4)])
def test_decode_counts_round_trips(busy: int, idle: int) -> None:
    # Spread across tiers so 18 agents fit under the per-tier saturation point.
    tiers = ("small", "medium", "large")
    agents = [
        dataclasses.replace(_agent(f"b{i}", AgentStatus.BUSY), model_tier=tiers[i % 3])
        for i in range(busy)
    ] + [
        dataclasses.replace(_agent(f"i{i}", AgentStatus.IDLE), model_tier=tiers[i % 3])
        for i in range(idle)
    ]
    obs = encode_observation(_state(agents=agents), _NULL_CTX)
    got_busy, got_live, saturated = decode_counts(obs.tobytes())
    assert (got_busy, got_live, saturated) == (busy, busy + idle, False)


@pytest.mark.parametrize(("busy", "live"), [(1, 1), (5, 8), (7, 9), (18, 18)])
def test_current_concurrency_matches_reward(busy: int, live: int) -> None:
    cfg = RewardConfig()
    _, bd = compute_reward(
        RewardSignals(
            play_type=PlayType.ISSUE_PICKUP,
            success=True,
            busy_agent_count=busy,
            live_agent_count=live,
        ),
        cfg,
    )
    assert current_concurrency(busy, live, cfg) == pytest.approx(bd.concurrent_agent_bonus)
