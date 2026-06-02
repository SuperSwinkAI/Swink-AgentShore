"""Tests for TakeBreakPlay."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.plays.base import PlayExecutionContext, PlayParams
from agentshore.plays.internal.take_break import TakeBreakPlay
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    OrchestratorState,
    PlayType,
    SessionState,
)


def _make_idle_agent() -> AgentSnapshot:
    return AgentSnapshot(
        agent_id="a1",
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.IDLE,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
    )


def _make_error_agent(agent_id: str) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.ERROR,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=1,
        last_error_class="unknown",
    )


def _make_state() -> OrchestratorState:
    return OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[_make_idle_agent()],
    )


def _make_ctx(break_duration_minutes: int = 30) -> PlayExecutionContext:
    cfg = MagicMock()
    cfg.session.break_duration_minutes = break_duration_minutes
    return PlayExecutionContext(
        session_id="s1",
        play_id=1,
        manager=MagicMock(),
        store=MagicMock(),
        cfg=cfg,
        project_path=MagicMock(),
    )


def test_play_type():
    assert TakeBreakPlay().play_type == PlayType.TAKE_BREAK


def test_preconditions_passes_when_idle():
    play = TakeBreakPlay()
    state = _make_state()
    assert play.preconditions(state) == []


def test_preconditions_do_not_block_other_work_when_break_in_flight():
    """An in-flight TAKE_BREAK is per-agent and must not globally block the play."""
    play = TakeBreakPlay()
    state = _make_state()
    state.in_flight_plays = [PlayType.TAKE_BREAK]
    assert play.preconditions(state) == []


def test_preconditions_block_duplicate_break_for_agent_already_cooling():
    play = TakeBreakPlay()
    state = OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[
            AgentSnapshot(
                agent_id="err1",
                agent_type=AgentType.GEMINI,
                status=AgentStatus.ERROR,
                context_size=0,
                total_cost=0.0,
                total_tokens=0,
                tasks_completed=0,
                tasks_failed=1,
                last_error_class="rate_limit",
                current_play_type=PlayType.TAKE_BREAK,
            )
        ],
    )

    assert play.preconditions(state) != []


def test_estimated_cost_is_nonzero():
    """take_break carries a small cost so PPO doesn't free-ride on it."""
    play = TakeBreakPlay()
    state = _make_state()
    assert play.estimated_cost(state) > 0.0


def test_preconditions_no_cooldown():
    """TAKE_BREAK has no plays-based cooldown — re-fires immediately if trigger persists."""
    play = TakeBreakPlay()
    state = _make_state()
    state.plays_since_last_play_type = {PlayType.TAKE_BREAK: 0}
    assert play.preconditions(state) == []


def test_skill_name_and_capability_are_none():
    play = TakeBreakPlay()
    assert play.skill_name is None
    assert play.capability is None


@pytest.mark.asyncio
async def test_execute_returns_success():
    play = TakeBreakPlay()
    state = _make_state()
    ctx = _make_ctx(break_duration_minutes=1)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        outcome = await play.execute(state, PlayParams(), ctx=ctx)

    mock_sleep.assert_awaited_once_with(60)
    assert outcome.success is True
    assert outcome.play_type == PlayType.TAKE_BREAK
    assert outcome.agent_id is None
    assert outcome.dollar_cost > 0.0
    assert outcome.token_cost == 0
    assert outcome.partial is False
    assert outcome.alignment_delta == 0.0


@pytest.mark.asyncio
async def test_execute_recovers_error_agents():
    """After the sleep, the targeted ERROR agent is recovered so its trigger clears."""
    play = TakeBreakPlay()

    state = OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[_make_idle_agent(), _make_error_agent("err1")],
    )

    ctx = _make_ctx(break_duration_minutes=1)
    ctx.manager.attempt_recovery = AsyncMock(return_value=True)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        outcome = await play.execute(
            state,
            PlayParams(
                agent_id="err1",
                extras={
                    "trigger_agent_id": "err1",
                    "trigger_error_class": "unknown",
                },
            ),
            ctx=ctx,
        )

    ctx.manager.attempt_recovery.assert_awaited_once_with("err1")
    assert outcome.agent_id == "err1"
    assert outcome.artifacts[0]["recovered_agents"] == ["err1"]


@pytest.mark.asyncio
async def test_execute_recovers_only_target_error_agent():
    """A break for one agent must not recover or pause unrelated errored agents."""
    play = TakeBreakPlay()
    state = OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[
            _make_idle_agent(),
            _make_error_agent("err1"),
            _make_error_agent("err2"),
            _make_error_agent("err3"),
        ],
    )

    ctx = _make_ctx(break_duration_minutes=1)
    ctx.manager.attempt_recovery = AsyncMock(return_value=True)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        outcome = await play.execute(
            state,
            PlayParams(
                agent_id="err2",
                extras={
                    "trigger_agent_id": "err2",
                    "trigger_error_class": "unknown",
                },
            ),
            ctx=ctx,
        )

    ctx.manager.attempt_recovery.assert_awaited_once_with("err2")
    assert outcome.agent_id == "err2"
    assert outcome.artifacts[0]["recovered_agents"] == ["err2"]


@pytest.mark.asyncio
async def test_execute_uses_configured_duration():
    play = TakeBreakPlay()
    state = _make_state()
    ctx = _make_ctx(break_duration_minutes=45)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await play.execute(state, PlayParams(), ctx=ctx)

    mock_sleep.assert_awaited_once_with(45 * 60)


@pytest.mark.asyncio
async def test_execute_artifact_contains_duration():
    play = TakeBreakPlay()
    state = _make_state()
    ctx = _make_ctx(break_duration_minutes=1)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        outcome = await play.execute(state, PlayParams(), ctx=ctx)

    assert len(outcome.artifacts) == 1
    artifact = outcome.artifacts[0]
    assert artifact["type"] == "session_event"
    assert artifact["event"] == "break_completed"
    assert "duration_s" in artifact
