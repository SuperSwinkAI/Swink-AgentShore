"""Tests for PPO's ability to pick INSTANTIATE_AGENT under sustained pressure.

The orchestrator used to short-circuit ``_select_play`` whenever every agent
was BUSY and a play was in flight, which made it impossible for the policy to
choose INSTANTIATE_AGENT during high-load windows. The gate was removed in
favour of letting the existing mask machinery decide:

- ``compute_agent_eligibility_mask`` already masks off *worker* plays (those
  with a non-None capability) when no IDLE agent satisfies their eligibility.
- ``compute_config_mask`` still bounds INSTANTIATE_AGENT by each tier's ``max`` (default 1).

These tests pin the safety property the orchestrator change relies on:
during all-busy, INSTANTIATE_AGENT remains pickable while worker plays do not.
"""

from __future__ import annotations

from agentshore.config.models import (
    AgentConfig,
    ModelTierConfig,
    RuntimeConfig,
)
from agentshore.plays.registry import build_default_registry
from agentshore.rl.action_space import PLAY_TO_INDEX
from agentshore.rl.mask import (
    compute_action_mask,
    compute_agent_eligibility_mask,
    compute_config_mask,
)
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    OrchestratorState,
    PlayType,
    SessionState,
)


def _busy_agent(agent_id: str, agent_type: AgentType = AgentType.CLAUDE_CODE) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=agent_type,
        status=AgentStatus.BUSY,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
        model_tier="medium",
    )


def _cfg(*, max_per_config: int = 5) -> RuntimeConfig:
    return RuntimeConfig(
        agents={
            "claude_code": AgentConfig(
                enabled=True,
                model_tiers={
                    "medium": ModelTierConfig(model="m", enabled=True, max=max_per_config)
                },
            ),
            "codex": AgentConfig(
                enabled=True,
                model_tiers={
                    "medium": ModelTierConfig(model="m", enabled=True, max=max_per_config)
                },
            ),
        },
    )


def _pressure_state(
    *,
    busy_agents: int = 3,
    in_flight: tuple[PlayType, ...] = (PlayType.ISSUE_PICKUP,),
    plays_since_last_instantiate: int | None = 5,
) -> OrchestratorState:
    agents = [_busy_agent(f"a{i}") for i in range(busy_agents)]
    return OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=10,
        total_cost=0.0,
        agents=agents,
        in_flight_plays=list(in_flight),
        plays_since_last_play_type={PlayType.SEED_PROJECT: 0, PlayType.DESIGN_AUDIT: 0},
        last_play_success_by_type={PlayType.SEED_PROJECT: True, PlayType.DESIGN_AUDIT: True},
        plays_since_last_instantiate=plays_since_last_instantiate,
    )


# ---------------------------------------------------------------------------
# Eligibility-mask invariants (the linchpin of the orchestrator change)
# ---------------------------------------------------------------------------


def test_eligibility_mask_keeps_instantiate_open_when_all_agents_busy() -> None:
    """INSTANTIATE_AGENT has capability=None → bypasses the eligibility mask.

    This is the property that lets PPO grow the fleet under pressure: even
    when every existing agent is BUSY, the eligibility mask leaves
    INSTANTIATE_AGENT True so PPO can pick it.
    """
    state = _pressure_state()
    cfg = _cfg()

    mask = compute_agent_eligibility_mask(state, build_default_registry(), cfg=cfg)

    assert mask[PLAY_TO_INDEX[PlayType.INSTANTIATE_AGENT]]


def test_eligibility_mask_masks_worker_plays_when_all_agents_busy() -> None:
    """Worker plays (capability != None) require an IDLE eligible agent.

    Confirms the safety property: removing the orchestrator's busy gate does
    not accidentally let PPO dispatch worker plays into a saturated fleet.
    """
    state = _pressure_state()
    cfg = _cfg()

    mask = compute_agent_eligibility_mask(state, build_default_registry(), cfg=cfg)

    # Sample of worker plays — none should be eligible while every agent is BUSY.
    for pt in (
        PlayType.ISSUE_PICKUP,
        PlayType.RUN_QA,
        PlayType.WRITE_IMPLEMENTATION_PLAN,
        PlayType.GROOM_BACKLOG,
    ):
        assert not mask[PLAY_TO_INDEX[pt]], f"{pt.value} should be masked when no IDLE agent"


# ---------------------------------------------------------------------------
# Config-mask still bounds spawn rate
# ---------------------------------------------------------------------------


def test_config_mask_blocks_instantiate_when_every_cell_saturated() -> None:
    """When every (type, tier) cell in config_index is at max_per_config, the mask is empty.

    Replaces the old ``max_total`` saturation test — that global ceiling was
    removed in desktop-ty04. The equivalent "no room anywhere" scenario is
    now expressed as "every cell at the per-cell cap".
    """
    cfg = _cfg(max_per_config=2)
    agents = [
        _busy_agent("a0", AgentType.CLAUDE_CODE),
        _busy_agent("a1", AgentType.CLAUDE_CODE),
        _busy_agent("a2", AgentType.CODEX),
        _busy_agent("a3", AgentType.CODEX),
    ]
    state = OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=10,
        total_cost=0.0,
        agents=agents,
        plays_since_last_play_type={PlayType.SEED_PROJECT: 0, PlayType.DESIGN_AUDIT: 0},
        last_play_success_by_type={PlayType.SEED_PROJECT: True, PlayType.DESIGN_AUDIT: True},
        plays_since_last_instantiate=5,
    )
    config_index = (("claude_code", "medium"), ("codex", "medium"))

    config_mask = compute_config_mask(state, cfg, config_index)

    assert not config_mask.any()


def test_config_mask_blocks_instantiate_at_max_per_config() -> None:
    """Per-(type, tier) cap blocks further spawns of an already-saturated config."""
    agents = [_busy_agent(f"a{i}", AgentType.CLAUDE_CODE) for i in range(5)]
    state = OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=10,
        total_cost=0.0,
        agents=agents,
        plays_since_last_play_type={PlayType.SEED_PROJECT: 0, PlayType.DESIGN_AUDIT: 0},
        last_play_success_by_type={PlayType.SEED_PROJECT: True, PlayType.DESIGN_AUDIT: True},
        plays_since_last_instantiate=5,
    )
    cfg = _cfg(max_per_config=5)
    config_index = (("claude_code", "medium"), ("codex", "medium"))

    config_mask = compute_config_mask(state, cfg, config_index)

    # claude_code/medium is at the cap; codex/medium still has room.
    assert not config_mask[0]
    assert config_mask[1]


# ---------------------------------------------------------------------------
# End-to-end: action mask under pressure
# ---------------------------------------------------------------------------


def test_action_mask_under_pressure_keeps_instantiate_pickable() -> None:
    """Full ``compute_action_mask`` exposes INSTANTIATE_AGENT during all-busy.

    With a non-empty fleet, BUSY-only agents, and an in-flight worker play,
    PPO should still see INSTANTIATE_AGENT True (room remains under
    ``max_per_config``) while every worker play is False.
    """
    state = _pressure_state(busy_agents=3)
    cfg = _cfg(max_per_config=5)
    config_index = (("claude_code", "medium"), ("codex", "medium"))

    mask = compute_action_mask(
        state,
        build_default_registry(),
        cfg=cfg,
        config_index=config_index,
    )

    assert mask[PLAY_TO_INDEX[PlayType.INSTANTIATE_AGENT]], (
        "PPO must be able to pick INSTANTIATE_AGENT when the fleet is saturated "
        "but room remains under max_per_config"
    )
    assert not mask[PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]]
    assert not mask[PLAY_TO_INDEX[PlayType.RUN_QA]]


def test_action_mask_at_per_cell_cap_blocks_instantiate() -> None:
    """At ``max_per_config`` in every relevant cell, the policy has no fleet-growth option.

    Replaces the old at-max_total test — with no global ceiling, the only way
    to block INSTANTIATE_AGENT is to saturate every cell in config_index.
    """
    agents = [
        _busy_agent("c0", AgentType.CLAUDE_CODE),
        _busy_agent("c1", AgentType.CLAUDE_CODE),
        _busy_agent("x0", AgentType.CODEX),
        _busy_agent("x1", AgentType.CODEX),
    ]
    state = OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=10,
        total_cost=0.0,
        agents=agents,
        in_flight_plays=[PlayType.ISSUE_PICKUP],
        plays_since_last_play_type={PlayType.SEED_PROJECT: 0, PlayType.DESIGN_AUDIT: 0},
        last_play_success_by_type={PlayType.SEED_PROJECT: True, PlayType.DESIGN_AUDIT: True},
        plays_since_last_instantiate=5,
    )
    cfg = _cfg(max_per_config=2)
    config_index = (("claude_code", "medium"), ("codex", "medium"))

    mask = compute_action_mask(
        state,
        build_default_registry(),
        cfg=cfg,
        config_index=config_index,
    )

    assert not mask[PLAY_TO_INDEX[PlayType.INSTANTIATE_AGENT]]
