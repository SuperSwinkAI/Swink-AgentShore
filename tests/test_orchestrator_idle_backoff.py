"""Tests for the orchestrator's selection-digest gate + Fibonacci idle backoff.

Regression for the 2026-05-07 ``ppo_selector.retry_exhausted`` storm: the
main loop was re-running the selector once per second against unchanged
state during long-running plays, firing 570 events in 13 minutes. The
gate skips ``_select_play`` when the digest matches, and the backoff
stretches the watchdog wait progressively the longer the digest stays put.
"""

from __future__ import annotations

from agentshore.core import Orchestrator
from agentshore.core.mixins.loop import _IDLE_BACKOFF_SECONDS
from agentshore.core.override_queue import OverrideQueue
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    OrchestratorState,
    SessionState,
)


def _state(
    *,
    agents: tuple[AgentSnapshot, ...] = (),
    action_mask: tuple[bool, ...] = (),
    open_issues: int = 0,
    pull_requests: int = 0,
    session_state: SessionState = SessionState.RUNNING,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="s",
        session_state=session_state,
        total_plays=0,
        total_cost=0.0,
        agents=list(agents),
        open_issues=[None] * open_issues,  # type: ignore[list-item]
        pull_requests=[None] * pull_requests,  # type: ignore[list-item]
        action_mask=action_mask,
    )


def _agent(agent_id: str, status: AgentStatus = AgentStatus.IDLE) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CLAUDE_CODE,
        status=status,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
    )


def _orch() -> Orchestrator:
    """Bare Orchestrator with just the fields the digest + backoff logic needs."""
    orch = Orchestrator.__new__(Orchestrator)
    orch._in_flight = {}
    orch._overrides = OverrideQueue()
    orch._idle_streak = 0
    orch._last_selection_digest = None
    return orch


# ---------------------------------------------------------------------------
# _idle_backoff()
# ---------------------------------------------------------------------------


def test_idle_backoff_starts_at_one_second() -> None:
    orch = _orch()
    assert orch._idle_backoff() == 1.0


def test_idle_backoff_advances_through_fibonacci() -> None:
    orch = _orch()
    observed = []
    for _ in _IDLE_BACKOFF_SECONDS:
        observed.append(orch._idle_backoff())
        orch._idle_streak += 1
    assert tuple(observed) == _IDLE_BACKOFF_SECONDS


def test_idle_backoff_clamps_at_ceiling() -> None:
    orch = _orch()
    orch._idle_streak = 999
    assert orch._idle_backoff() == _IDLE_BACKOFF_SECONDS[-1]


# ---------------------------------------------------------------------------
# _selection_state_digest()
# ---------------------------------------------------------------------------


def test_digest_is_stable_for_equivalent_state() -> None:
    orch = _orch()
    state = _state(agents=(_agent("a"), _agent("b")), action_mask=(True, False, True))
    idle = list(state.agents)
    d1 = orch._selection_state_digest(state, idle)
    d2 = orch._selection_state_digest(state, idle)
    assert d1 == d2
    assert isinstance(d1, bytes)
    assert len(d1) == 16


def test_digest_changes_when_idle_agent_set_changes() -> None:
    orch = _orch()
    state = _state(agents=(_agent("a"), _agent("b")), action_mask=(True,))
    d_both = orch._selection_state_digest(state, list(state.agents))
    d_one = orch._selection_state_digest(state, [state.agents[0]])
    assert d_both != d_one


def test_digest_is_order_independent_for_idle_agents() -> None:
    orch = _orch()
    state = _state(agents=(_agent("a"), _agent("b")), action_mask=(True,))
    d_ab = orch._selection_state_digest(state, [state.agents[0], state.agents[1]])
    d_ba = orch._selection_state_digest(state, [state.agents[1], state.agents[0]])
    assert d_ab == d_ba


def test_digest_changes_when_in_flight_count_changes() -> None:
    orch = _orch()
    state = _state(agents=(_agent("a"),), action_mask=(True,))
    d_empty = orch._selection_state_digest(state, list(state.agents))
    orch._in_flight["d1"] = object()  # type: ignore[assignment]
    d_one = orch._selection_state_digest(state, list(state.agents))
    assert d_empty != d_one


def test_digest_changes_when_action_mask_changes() -> None:
    orch = _orch()
    a = _agent("a")
    s1 = _state(agents=(a,), action_mask=(True, False, True))
    s2 = _state(agents=(a,), action_mask=(False, False, True))
    d1 = orch._selection_state_digest(s1, list(s1.agents))
    d2 = orch._selection_state_digest(s2, list(s2.agents))
    assert d1 != d2


def test_digest_changes_when_override_queued() -> None:
    orch = _orch()
    state = _state(agents=(_agent("a"),), action_mask=(True,))
    d_no = orch._selection_state_digest(state, list(state.agents))

    orch._overrides.put_nowait(("dummy", "params"))  # type: ignore[arg-type]
    d_yes = orch._selection_state_digest(state, list(state.agents))
    assert d_no != d_yes


def test_digest_changes_when_session_state_changes() -> None:
    orch = _orch()
    a = _agent("a")
    s_run = _state(agents=(a,), action_mask=(True,), session_state=SessionState.RUNNING)
    s_pause = _state(agents=(a,), action_mask=(True,), session_state=SessionState.PAUSED)
    d_run = orch._selection_state_digest(s_run, list(s_run.agents))
    d_pause = orch._selection_state_digest(s_pause, list(s_pause.agents))
    assert d_run != d_pause


def test_digest_changes_with_total_plays() -> None:
    """Every completed play bumps state.total_plays, which must change the
    digest even if in_flight / idle agents cycled back to the same shape.

    Without this, after a single-agent dispatch+complete cycle the post-harvest
    state digest-matches the pre-dispatch state and the loop would `break`
    instead of re-selecting.
    """
    orch = _orch()
    a = _agent("a")
    s_before = OrchestratorState(
        session_id="s",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[a],
        action_mask=(True,),
    )
    s_after = OrchestratorState(
        session_id="s",
        session_state=SessionState.RUNNING,
        total_plays=1,
        total_cost=0.0,
        agents=[a],
        action_mask=(True,),
    )
    d_before = orch._selection_state_digest(s_before, list(s_before.agents))
    d_after = orch._selection_state_digest(s_after, list(s_after.agents))
    assert d_before != d_after


def test_digest_changes_with_github_state() -> None:
    """Issue / PR count deltas are a coarse proxy for new GitHub work."""
    orch = _orch()
    a = _agent("a")
    s_before = _state(agents=(a,), action_mask=(True,), open_issues=2, pull_requests=1)
    s_after = _state(agents=(a,), action_mask=(True,), open_issues=3, pull_requests=1)
    d_before = orch._selection_state_digest(s_before, list(s_before.agents))
    d_after = orch._selection_state_digest(s_after, list(s_after.agents))
    assert d_before != d_after
