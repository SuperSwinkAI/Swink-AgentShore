"""Tests for drain-mode action mask: only END_AGENT should be selectable."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from agentshore.rl.action_space import NUM_ACTIONS, V1_ACTION_ORDER
from agentshore.rl.mask import compute_action_mask, compute_mask_reasons
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    OrchestratorState,
    PlayType,
    SessionState,
)


def _snap(agent_id: str = "a1", status: AgentStatus = AgentStatus.IDLE) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CLAUDE_CODE,
        status=status,
        context_size=10_000,
        total_cost=0.1,
        total_tokens=50_000,
        tasks_completed=5,
        tasks_failed=0,
    )


def _draining_state(
    agents: list[AgentSnapshot] | None = None,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="sess",
        session_state=SessionState.DRAINING,
        drain_reason="user_request",
        total_plays=10,
        total_cost=0.5,
        agents=[_snap()] if agents is None else agents,
    )


def _cfg_mock() -> MagicMock:
    cfg = MagicMock()
    cfg.agents = []
    cfg.session.max_agents = 4
    return cfg


def _registry_mock(all_pass: bool = True) -> MagicMock:
    registry = MagicMock()
    if all_pass:
        # Make every play's preconditions return [] (all pass) so the mask
        # reflects agent/session filtering rather than returning all-zeros.
        play_mock = MagicMock()
        play_mock.preconditions.return_value = []
        registry.get.return_value = play_mock
    else:
        # All fail — preconditions return a non-empty list.
        play_mock = MagicMock()
        play_mock.preconditions.return_value = ["blocked"]
        registry.get.return_value = play_mock
    return registry


def test_drain_mask_only_end_agent_set() -> None:
    """In DRAINING state, only the END_AGENT slot should be True."""
    state = _draining_state()
    cfg = _cfg_mock()
    registry = _registry_mock()

    mask = compute_action_mask(state, cfg=cfg, registry=registry)

    assert mask.shape == (NUM_ACTIONS,)
    assert mask.dtype == bool

    end_agent_idx = V1_ACTION_ORDER.index(PlayType.END_AGENT)
    assert mask[end_agent_idx] is np.bool_(True), "END_AGENT must be True in drain mode"

    for i, pt in enumerate(V1_ACTION_ORDER):
        if pt != PlayType.END_AGENT:
            assert mask[i] is np.bool_(False), f"{pt} must be False in drain mode"


def test_drain_mask_all_other_plays_masked() -> None:
    """Drain mask zeroes every play except END_AGENT, regardless of preconditions."""
    state = _draining_state(agents=[_snap("a1"), _snap("a2")])
    cfg = _cfg_mock()
    registry = _registry_mock()

    mask = compute_action_mask(state, cfg=cfg, registry=registry)

    true_indices = [i for i in range(NUM_ACTIONS) if mask[i]]
    assert len(true_indices) == 1
    assert V1_ACTION_ORDER[true_indices[0]] == PlayType.END_AGENT


def test_drain_mask_reasons_cover_all_other_plays() -> None:
    """compute_mask_reasons in drain mode returns a draining reason for every non-END_AGENT play."""
    state = _draining_state()
    cfg = _cfg_mock()
    registry = _registry_mock()

    reasons = compute_mask_reasons(state, cfg=cfg, registry=registry)

    for pt in V1_ACTION_ORDER:
        if pt != PlayType.END_AGENT:
            assert pt in reasons, f"Expected mask reason for {pt} in drain mode"
            assert "drain" in reasons[pt].lower() or "draining" in reasons[pt].lower()


def test_non_draining_state_not_restricted_to_end_agent() -> None:
    """RUNNING state should NOT collapse the mask to just END_AGENT.

    Calls compute_action_mask without cfg so the agent-eligibility gate is
    skipped; this isolates the drain-filter logic from tier/capability checks.
    """
    # All plays pass preconditions; with no cfg the eligibility gate is skipped.
    registry = _registry_mock(all_pass=True)

    running_state = OrchestratorState(
        session_id="sess",
        session_state=SessionState.RUNNING,
        total_plays=5,
        total_cost=0.1,
        agents=[_snap()],
    )
    draining_state = OrchestratorState(
        session_id="sess",
        session_state=SessionState.DRAINING,
        total_plays=5,
        total_cost=0.1,
        agents=[_snap()],
    )

    running_mask = compute_action_mask(running_state, registry=registry)
    draining_mask = compute_action_mask(draining_state, registry=registry)

    # The drain filter must produce a different (more restrictive) result.
    assert not np.array_equal(running_mask, draining_mask), (
        "RUNNING state mask should differ from DRAINING state mask"
    )

    # In RUNNING mode, more than just END_AGENT should be True.
    end_agent_idx = V1_ACTION_ORDER.index(PlayType.END_AGENT)
    non_end_agent_true = sum(1 for i, v in enumerate(running_mask) if v and i != end_agent_idx)
    assert non_end_agent_true > 0, "RUNNING mask must allow plays beyond END_AGENT"
