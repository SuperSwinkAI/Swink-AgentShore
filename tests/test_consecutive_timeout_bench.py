"""#161: a hung agent that times out repeatedly is benched after consecutive
stream-idle timeouts, instead of being re-selected for reconcile_state forever.

reconcile_state is deliberately never play-benched (self-heal must stay
available), and a stream-idle timeout returns the agent to IDLE — so a
previously-productive agent that hangs producing no stdout (gemini) is otherwise
re-picked and hangs for the full 1800s window again, repeatedly. The
consecutive-timeout counter benches the AGENT (not the play) so dispatch routes
elsewhere, and keeps END_AGENT available so the wedged agent can be reaped.
"""

from __future__ import annotations

from agentshore.state import (
    CONSECUTIVE_TIMEOUT_BENCH_LIMIT,
    AgentSnapshot,
    AgentStatus,
    AgentType,
    OrchestratorState,
    SessionState,
    is_agent_circuit_broken,
)


def _agent(consecutive_timeouts: int = 0, tasks_completed: int = 5) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id="gemini0",
        agent_type=AgentType.GEMINI,
        status=AgentStatus.IDLE,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=tasks_completed,
        tasks_failed=0,
        consecutive_timeouts=consecutive_timeouts,
    )


def test_productive_agent_benched_after_consecutive_timeouts() -> None:
    """A productive agent (completions > 0) is normally not benched, but two
    consecutive timeouts bench it regardless of prior completions."""
    assert not is_agent_circuit_broken(
        tasks_completed=5, tasks_failed=0, timeout_count=9, consecutive_timeouts=1
    )
    assert is_agent_circuit_broken(
        tasks_completed=5,
        tasks_failed=0,
        timeout_count=9,
        consecutive_timeouts=CONSECUTIVE_TIMEOUT_BENCH_LIMIT,
    )


def test_single_timeout_does_not_bench_a_productive_agent() -> None:
    """One stall costs at most a single retry — the counter resets on success."""
    assert CONSECUTIVE_TIMEOUT_BENCH_LIMIT == 2
    assert not is_agent_circuit_broken(
        tasks_completed=5, tasks_failed=0, timeout_count=1, consecutive_timeouts=1
    )


def test_zero_completion_breaker_still_applies() -> None:
    """The original #22 breaker (0 completions + a timeout) is unchanged."""
    assert is_agent_circuit_broken(
        tasks_completed=0, tasks_failed=0, timeout_count=1, consecutive_timeouts=0
    )


def _state(agents: list[AgentSnapshot]) -> OrchestratorState:
    return OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=agents,
    )


def test_benched_agent_counts_as_needs_reaping() -> None:
    """A consecutive-timeout-benched agent keeps END_AGENT available under churn
    so the PPO can reap and replace it rather than wedging on an idle hung agent."""
    from agentshore.rl.mask import _agent_needs_reaping

    assert _agent_needs_reaping(_state([_agent(consecutive_timeouts=2)]))
    assert not _agent_needs_reaping(_state([_agent(consecutive_timeouts=1)]))
