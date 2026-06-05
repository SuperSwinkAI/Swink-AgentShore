"""Tests for internal plays."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agentshore.plays.base import PlayParams
from agentshore.plays.internal.end_agent import EndAgentPlay
from agentshore.plays.internal.end_session import EndSessionPlay
from agentshore.plays.internal.reserved_action import (
    FutureEightPlay,
    FutureFourPlay,
    FutureSevenPlay,
)
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    BudgetSnapshot,
    OrchestratorState,
    SessionState,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _snap(
    agent_id: str = "a1",
    agent_type: AgentType = AgentType.CLAUDE_CODE,
    status: AgentStatus = AgentStatus.IDLE,
    context_size: int = 10_000,
    tasks_completed: int = 5,
    tasks_failed: int = 1,
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=agent_type,
        status=status,
        context_size=context_size,
        total_cost=0.1,
        total_tokens=0,
        tasks_completed=tasks_completed,
        tasks_failed=tasks_failed,
    )


def _state(
    agents: list[AgentSnapshot] | None = None,
    total_plays: int = 3,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="sess-test",
        session_state=SessionState.RUNNING,
        total_plays=total_plays,
        total_cost=0.5,
        agents=[_snap()] if agents is None else agents,
        budget=BudgetSnapshot(5.0, 0.5, 4.5, 0.1),
    )


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.manager = MagicMock()
    ctx.store = AsyncMock()
    ctx.cfg = MagicMock()
    ctx.session_id = "sess-test"
    ctx.play_id = 1
    return ctx


# ---------------------------------------------------------------------------
# Reserved action slots
# ---------------------------------------------------------------------------


def test_reserved_slots_are_unavailable() -> None:
    state = _state()
    assert FutureFourPlay().preconditions(state) != []
    assert FutureSevenPlay().preconditions(state) != []
    assert FutureEightPlay().preconditions(state) != []


@pytest.mark.asyncio
async def test_reserved_slot_execute_mutates_nothing() -> None:
    play = FutureSevenPlay()
    ctx = _ctx()
    handle = MagicMock()
    handle.context_size = 80_000
    ctx.manager.get_handle = MagicMock(return_value=handle)

    outcome = await play.execute(_state(), PlayParams(agent_id="a1"), ctx=ctx)

    assert outcome.success is False
    assert outcome.error == "reserved action slot"
    assert handle.context_size == 80_000
    ctx.manager.get_handle.assert_not_called()


# ---------------------------------------------------------------------------
# EndAgentPlay (terminates agent)
# ---------------------------------------------------------------------------


def test_end_agent_precondition_requires_two_agents() -> None:
    play = EndAgentPlay()
    # 1 agent with plenty of plays — still masked (need 2+ agents)
    assert play.preconditions(_state(agents=[_snap("a1", tasks_completed=20)])) != []
    # 2 agents, at least one above the play-count threshold (>10)
    assert play.preconditions(_state(agents=[_snap("a1", tasks_completed=20), _snap("a2")])) == []


def test_end_agent_precondition_requires_one_veteran() -> None:
    """At least one agent must have >_MIN_PLAYS_PER_AGENT (10) plays before end_agent fires.

    Threshold history:
      - desktop-lyfb (PR a92ce1ae, 2026-05-21) lowered 10 → 5 for faster PPO signal.
      - 2026-05-22 restored to 10 after premature Codex termination in
        example-project session c78d7074 (Codex end_agent'd at exactly 5 plays,
        before the bootstrap cleanup had finished its first run).
    """
    play = EndAgentPlay()
    # 2 agents, neither above 10 plays (total = tasks_completed + tasks_failed) — masked.
    # _snap defaults tasks_failed=1, so tasks_completed=3 → total=4, tasks_completed=8 → total=9.
    assert (
        play.preconditions(
            _state(agents=[_snap("a1", tasks_completed=3), _snap("a2", tasks_completed=8)])
        )
        != []
    )


@pytest.mark.asyncio
async def test_end_agent_calls_manager_clear() -> None:
    play = EndAgentPlay()
    ctx = _ctx()
    ctx.manager.clear = AsyncMock()

    outcome = await play.execute(
        _state(agents=[_snap("a1"), _snap("a2")]),
        PlayParams(agent_id="a1"),
        ctx=ctx,
    )

    assert outcome.success is True
    ctx.manager.clear.assert_awaited_once_with("a1")


@pytest.mark.asyncio
async def test_end_agent_returns_failure_on_exception() -> None:
    play = EndAgentPlay()
    ctx = _ctx()
    ctx.manager.clear = AsyncMock(side_effect=RuntimeError("already cleared"))

    outcome = await play.execute(
        _state(agents=[_snap("a1"), _snap("a2")]),
        PlayParams(agent_id="a1"),
        ctx=ctx,
    )
    assert outcome.success is False


# ---------------------------------------------------------------------------
# Reserved future slots (4, 7, 8) — kept for policy shape compatibility.
# Always fail preconditions; execute returns success=False.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_future_four_is_reserved_no_op() -> None:
    play = FutureFourPlay()
    reasons = play.preconditions(_state())
    assert len(reasons) == 1
    assert reasons[0].text == "Reserved action slot"
    outcome = await play.execute(_state(), PlayParams(), ctx=_ctx())
    assert outcome.success is False


@pytest.mark.asyncio
async def test_future_seven_is_reserved_no_op() -> None:
    play = FutureSevenPlay()
    reasons = play.preconditions(_state())
    assert len(reasons) == 1
    assert reasons[0].text == "Reserved action slot"
    outcome = await play.execute(_state(), PlayParams(), ctx=_ctx())
    assert outcome.success is False


@pytest.mark.asyncio
async def test_future_eight_is_reserved_no_op() -> None:
    play = FutureEightPlay()
    reasons = play.preconditions(_state())
    assert len(reasons) == 1
    assert reasons[0].text == "Reserved action slot"
    outcome = await play.execute(_state(), PlayParams(), ctx=_ctx())
    assert outcome.success is False


# ---------------------------------------------------------------------------
# EndSessionPlay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_session_succeeds_and_emits_draining_artifact() -> None:
    play = EndSessionPlay()
    state = _state()
    outcome = await play.execute(
        state,
        PlayParams(reason="goals_complete", extras={"shutdown_source": "auto_goals_complete"}),
        ctx=_ctx(),
    )

    assert outcome.success is True
    events = [a for a in outcome.artifacts if isinstance(a, dict)]
    assert any(
        a.get("event") == "drain_requested"
        and a.get("reason") == "goals_complete"
        and a.get("source") == "auto_goals_complete"
        for a in events
    )


@pytest.mark.asyncio
async def test_end_session_is_idempotent() -> None:
    play = EndSessionPlay()
    state = _state()
    await play.execute(state, PlayParams(), ctx=_ctx())
    outcome = await play.execute(state, PlayParams(), ctx=_ctx())
    assert outcome.success is True


# ---------------------------------------------------------------------------
# EndAgentPlay edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_agent_returns_failure_when_no_agent_id() -> None:
    play = EndAgentPlay()
    outcome = await play.execute(_state(), PlayParams(), ctx=_ctx())
    assert outcome.success is False
    assert "agent_id not resolved" in (outcome.error or "")
