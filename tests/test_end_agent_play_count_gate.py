"""Tests for the end_agent play-count gate (≥5 plays per agent).

Regression for the micro-agent churn pattern observed in a prior
run, where tier-mismatched spawns were terminated within
<1s of creation. The gate prevents end_agent from being eligible until at
least one agent has earned its keep, and prevents the resolver from picking
agents below the threshold. Floor lowered 10 → 5 in desktop-lyfb so PPO can
retire weak agents on real-but-trim signal instead of waiting for 10 plays.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agentshore.config import RuntimeConfig
from agentshore.plays.internal.end_agent import _MIN_PLAYS_PER_AGENT, EndAgentPlay
from agentshore.plays.resolver import ParameterResolver
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    OrchestratorState,
    SessionState,
)


def _agent(
    agent_id: str,
    completed: int = 0,
    failed: int = 0,
    status: AgentStatus = AgentStatus.IDLE,
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CLAUDE_CODE,
        status=status,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=completed,
        tasks_failed=failed,
    )


def _state(agents: list[AgentSnapshot]) -> OrchestratorState:
    return OrchestratorState(
        session_id="test",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=agents,
    )


def test_end_agent_masked_when_only_one_agent():
    play = EndAgentPlay()
    state = _state([_agent("a", completed=20)])
    reasons = play.preconditions(state)
    assert any("at least 2 agents" in r.text for r in reasons)


def test_end_agent_masked_when_no_agent_has_threshold_plays():
    """All agents below the floor → masked."""
    play = EndAgentPlay()
    state = _state(
        [
            _agent("a", completed=2, failed=1),
            _agent("b", completed=6, failed=0),
            _agent("c", completed=10, failed=0),  # exactly at threshold, not >
        ]
    )
    reasons = play.preconditions(state)
    assert any(f"more than {_MIN_PLAYS_PER_AGENT} plays" in r.text for r in reasons)


def test_end_agent_floor_is_ten():
    """2026-05-22 restored the floor to 10 after example-project session
    c78d7074 showed Codex getting end_agent'd at exactly 5 plays (the prior
    desktop-lyfb floor), before the bootstrap cleanup had even finished its
    first run. 10 amortizes the instantiate cost over enough work for the
    agent to earn its slot."""
    assert _MIN_PLAYS_PER_AGENT == 10


def test_end_agent_eligible_when_one_agent_passes_threshold():
    play = EndAgentPlay()
    state = _state(
        [
            _agent("a", completed=3),
            _agent("veteran", completed=11),  # total 11 > 10
        ]
    )
    assert play.preconditions(state) == []


def test_end_agent_eligible_with_failed_plays_counting():
    """tasks_completed + tasks_failed counts toward threshold."""
    play = EndAgentPlay()
    state = _state(
        [
            _agent("a", completed=3),
            _agent("veteran", completed=8, failed=3),  # total 11 > 10
        ]
    )
    assert play.preconditions(state) == []


def _resolver() -> ParameterResolver:
    return ParameterResolver(
        store=MagicMock(),
        manager=MagicMock(),
        cfg=RuntimeConfig(),
        github=None,
    )


def test_resolver_excludes_young_agents():
    """Resolver picks the veteran, never a rookie."""
    resolver = _resolver()
    state = _state(
        [
            _agent("rookie1", completed=2),
            _agent("rookie2", completed=5),
            _agent("rookie3", completed=8),
            _agent("veteran", completed=12, failed=0),
        ]
    )
    params = resolver._resolve_end_agent(state)
    assert params is not None
    assert params.agent_id == "veteran"


def test_resolver_returns_none_when_no_eligible_target():
    resolver = _resolver()
    state = _state(
        [
            _agent("a", completed=2),
            _agent("b", completed=3),
        ]
    )
    assert resolver._resolve_end_agent(state) is None


def test_resolver_skips_busy_veteran():
    """Resolver only picks IDLE agents (busy veteran is excluded)."""
    resolver = _resolver()
    state = _state(
        [
            _agent("rookie", completed=2),
            _agent("busy_vet", completed=10, status=AgentStatus.BUSY),
        ]
    )
    assert resolver._resolve_end_agent(state) is None
