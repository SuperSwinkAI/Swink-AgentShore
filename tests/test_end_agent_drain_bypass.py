"""Tests for EndAgentPlay + resolver drain-mode bypasses."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from agentshore.plays.internal.end_agent import EndAgentPlay
from agentshore.plays.resolver import ParameterResolver
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    OrchestratorState,
    SessionState,
)


def _snap(
    agent_id: str = "a1",
    status: AgentStatus = AgentStatus.IDLE,
    tasks_completed: int = 5,
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CLAUDE_CODE,
        status=status,
        context_size=10_000,
        total_cost=0.1,
        total_tokens=50_000,
        tasks_completed=tasks_completed,
        tasks_failed=0,
    )


def _state(
    session_state: SessionState,
    agents: list[AgentSnapshot] | None = None,
    total_plays: int = 3,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="sess",
        session_state=session_state,
        total_plays=total_plays,
        total_cost=0.2,
        agents=[_snap()] if agents is None else agents,
    )


def test_end_agent_preconditions_in_drain_bypass_min_agents() -> None:
    """Drain mode bypasses the ≥2-agents gate — 1 agent is enough."""
    play = EndAgentPlay()
    state = _state(SessionState.DRAINING, agents=[_snap("a1", tasks_completed=2)])
    assert play.preconditions(state) == []


def test_end_agent_preconditions_in_drain_bypass_min_plays() -> None:
    """Drain mode bypasses the >10-plays-per-agent gate."""
    play = EndAgentPlay()
    # Two agents, neither has >10 plays — would normally be blocked in RUNNING
    state = _state(
        SessionState.DRAINING,
        agents=[_snap("a1", tasks_completed=3), _snap("a2", tasks_completed=1)],
    )
    assert play.preconditions(state) == []


def test_end_agent_preconditions_drain_fails_with_no_agents() -> None:
    """Even in drain mode, no agents means preconditions fail."""
    play = EndAgentPlay()
    state = _state(SessionState.DRAINING, agents=[])
    assert play.preconditions(state) != []


def test_end_agent_preconditions_normal_mode_blocks_single_veteran() -> None:
    """In RUNNING mode, do not terminate the last active agent."""
    play = EndAgentPlay()
    state = _state(SessionState.RUNNING, agents=[_snap("a1", tasks_completed=20)])
    reasons = play.preconditions(state)
    assert [r.text for r in reasons] == ["at least 2 agents required before ending one"]


def test_end_agent_preconditions_normal_mode_fails_with_no_agents() -> None:
    """In RUNNING mode, no agents still blocks the play."""
    play = EndAgentPlay()
    state = _state(SessionState.RUNNING, agents=[])
    reasons = play.preconditions(state)
    assert [r.text for r in reasons] == ["no agents to end"]


def test_end_agent_preconditions_normal_mode_two_agents_veteran() -> None:
    """In RUNNING mode, 2+ agents with 1 veteran unlocks the play."""
    play = EndAgentPlay()
    state = _state(
        SessionState.RUNNING,
        agents=[_snap("a1", tasks_completed=20), _snap("a2", tasks_completed=3)],
    )
    assert play.preconditions(state) == []


def _make_resolver() -> ParameterResolver:
    return ParameterResolver(
        store=AsyncMock(),
        manager=MagicMock(),
        cfg=MagicMock(),
    )


def test_resolver_end_agent_drain_picks_any_idle() -> None:
    """In drain mode, resolver picks any idle agent regardless of play count."""
    resolver = _make_resolver()
    # Agent with only 2 plays — would be skipped in RUNNING mode
    state = _state(
        SessionState.DRAINING,
        agents=[_snap("a1", status=AgentStatus.IDLE, tasks_completed=2)],
    )
    params = resolver._resolve_end_agent(state)
    assert params is not None
    assert params.agent_id == "a1"


def test_resolver_end_agent_drain_skips_busy_agents() -> None:
    """In drain mode, resolver still skips BUSY agents (can't kill mid-play)."""
    resolver = _make_resolver()
    state = _state(
        SessionState.DRAINING,
        agents=[_snap("a1", status=AgentStatus.BUSY, tasks_completed=2)],
    )
    params = resolver._resolve_end_agent(state)
    assert params is None


def test_resolver_end_agent_normal_mode_min_plays_enforced() -> None:
    """In RUNNING mode, agents with ≤10 plays are excluded from end_agent."""
    resolver = _make_resolver()
    state = _state(
        SessionState.RUNNING,
        agents=[
            _snap("a1", status=AgentStatus.IDLE, tasks_completed=3),
            _snap("a2", status=AgentStatus.IDLE, tasks_completed=5),
        ],
    )
    # Neither agent has >10 plays — resolver should return None
    params = resolver._resolve_end_agent(state)
    assert params is None


def test_resolver_end_agent_drain_picks_highest_failure_rate() -> None:
    """In drain mode, resolver picks the idle agent with the highest failure rate."""
    resolver = _make_resolver()

    def _snap_with_failures(agent_id: str, completed: int, failed: int) -> AgentSnapshot:
        return AgentSnapshot(
            agent_id=agent_id,
            agent_type=AgentType.CLAUDE_CODE,
            status=AgentStatus.IDLE,
            context_size=10_000,
            total_cost=0.1,
            total_tokens=50_000,
            tasks_completed=completed,
            tasks_failed=failed,
        )

    state = _state(
        SessionState.DRAINING,
        agents=[
            _snap_with_failures("a1", completed=5, failed=1),  # rate 0.17
            _snap_with_failures("a2", completed=3, failed=3),  # rate 0.50 — worst
            _snap_with_failures("a3", completed=8, failed=0),  # rate 0.00
        ],
    )
    params = resolver._resolve_end_agent(state)
    assert params is not None
    assert params.agent_id == "a2"
