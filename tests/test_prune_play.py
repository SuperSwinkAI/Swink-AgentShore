"""PrunePlay preconditions.

Prune is gated only on the standard skill-backed stack: an
implement-capable agent must be idle, no Prune may be in flight, and the
post-run cooldown must have elapsed. There is deliberately no debt
threshold — worktree/branch/bead debt is discovered and cleared at execute
time, so the play must be reachable whenever the base gates pass.
"""

from __future__ import annotations

from agentshore.plays.skill_backed.prune import PrunePlay
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    OrchestratorState,
    PlayType,
    SessionState,
)


def _capable_agent(*, status: AgentStatus = AgentStatus.IDLE) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id="agent-1",
        agent_type=AgentType.CLAUDE_CODE,
        status=status,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
    )


def _state(
    *,
    plays_since_last_play_type: dict[PlayType, int] | None = None,
    in_flight: list[PlayType] | None = None,
    agents: list[AgentSnapshot] | None = None,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="test",
        session_state=SessionState.RUNNING,
        total_plays=50,
        total_cost=0.0,
        open_issues=[],
        agents=agents if agents is not None else [_capable_agent()],
        graph=None,
        plays_since_last_play_type=plays_since_last_play_type or {},
        in_flight_plays=in_flight or [],
    )


def test_prune_eligible_when_base_gates_pass() -> None:
    """With an idle capable agent, no in-flight Prune, and cooldown elapsed,
    Prune is eligible — independent of bead/worktree state and even with no
    beads graph (the removed debt gate keyed off that graph)."""
    play = PrunePlay()
    assert play.preconditions(_state()) == []


def test_prune_masked_when_no_capable_agent() -> None:
    """No idle implement-capable agent -> capability gate masks Prune."""
    play = PrunePlay()
    reasons = play.preconditions(_state(agents=[_capable_agent(status=AgentStatus.BUSY)]))
    assert any("can_implement" in r.text for r in reasons)


def test_prune_masked_when_in_flight() -> None:
    """In-flight Prune blocks a concurrent dispatch."""
    play = PrunePlay()
    reasons = play.preconditions(_state(in_flight=[PlayType.PRUNE]))
    assert any("prune already in flight" in r.text for r in reasons)


def test_prune_masked_within_cooldown() -> None:
    """Standard cooldown after the last completion."""
    play = PrunePlay()
    reasons = play.preconditions(_state(plays_since_last_play_type={PlayType.PRUNE: 5}))
    assert any("prune cooldown (5/42" in r.text for r in reasons)


def test_prune_play_metadata() -> None:
    """Skill name and capability are the public contract — assert them."""
    play = PrunePlay()
    assert play.play_type == PlayType.PRUNE
    assert play.skill_name == "agentshore-prune"
    assert play.capability == "can_implement"
