"""Tests for TakeBreakPlay."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.errors import ErrorClass
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
        last_error_class=ErrorClass.UNKNOWN,
    )


def _make_state() -> OrchestratorState:
    return OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[_make_idle_agent()],
    )


def _make_manager() -> MagicMock:
    """Manager mock whose break-recovery registry hands out real events (#367)."""
    manager = MagicMock()
    manager.register_break_recovery.side_effect = lambda _agent_id: asyncio.Event()
    return manager


def _make_ctx(break_duration_minutes: int = 30) -> PlayExecutionContext:
    cfg = MagicMock()
    cfg.session.break_duration_minutes = break_duration_minutes
    return PlayExecutionContext(
        session_id="s1",
        play_id=1,
        manager=_make_manager(),
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
                agent_type=AgentType.GROK,
                status=AgentStatus.ERROR,
                context_size=0,
                total_cost=0.0,
                total_tokens=0,
                tasks_completed=0,
                tasks_failed=1,
                last_error_class=ErrorClass.RATE_LIMIT,
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
async def test_execute_target_agent_cleared_during_break_returns_clean_outcome():
    """#332: if the target agent was cleared from the registry (e.g. by a
    concurrent end_agent/reap) while this play slept, the target no longer
    appears in ``state.agents``. Execute must NOT call attempt_recovery for a
    stale id and must NOT raise/crash — it should return a clean, successful
    outcome with no recovery attempted."""
    play = TakeBreakPlay()

    # "err1" is the trigger but is absent from state.agents (already cleared).
    state = OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[_make_idle_agent()],
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

    ctx.manager.attempt_recovery.assert_not_awaited()
    assert outcome.success is True
    assert outcome.agent_id == "err1"
    assert outcome.artifacts[0]["recovered_agents"] == []
    assert outcome.artifacts[0]["event"] == "break_completed"


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


# ---------------------------------------------------------------------------
# Drain-aware break interruption (#30)
# ---------------------------------------------------------------------------


def _make_draining_ctx(is_draining, break_duration_minutes: int = 30) -> PlayExecutionContext:
    ctx = _make_ctx(break_duration_minutes=break_duration_minutes)
    ctx.manager.attempt_recovery = AsyncMock(return_value=True)
    # Rebuild with the drain signal (PlayExecutionContext is a slotted dataclass).
    import dataclasses

    return dataclasses.replace(ctx, is_draining=is_draining)


@pytest.mark.asyncio
async def test_execute_aborts_break_immediately_when_already_draining():
    """If drain is active at entry, the break never sleeps and never recovers."""
    play = TakeBreakPlay()
    state = OrchestratorState(
        session_id="s1",
        session_state=SessionState.DRAINING,
        total_plays=0,
        total_cost=0.0,
        agents=[_make_error_agent("err1")],
    )
    ctx = _make_draining_ctx(lambda: True, break_duration_minutes=30)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        outcome = await play.execute(
            state,
            PlayParams(agent_id="err1", extras={"trigger_agent_id": "err1"}),
            ctx=ctx,
        )

    mock_sleep.assert_not_awaited()  # no 30-min sleep
    ctx.manager.attempt_recovery.assert_not_awaited()  # no recovery during drain
    assert outcome.success is True  # intentional skip, not a failed retry
    assert outcome.partial is True
    assert outcome.agent_id == "err1"
    assert outcome.artifacts[0]["event"] == "break_skipped_draining"


@pytest.mark.asyncio
async def test_execute_aborts_break_when_drain_begins_mid_sleep():
    """A drain that flips on partway through aborts within one poll, not 30 min."""
    play = TakeBreakPlay()
    state = OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[_make_error_agent("err1")],
    )
    # False on the first poll, True on the second → exactly one chunk slept.
    draining_flags = iter([False, True])
    ctx = _make_draining_ctx(lambda: next(draining_flags), break_duration_minutes=30)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        outcome = await play.execute(
            state,
            PlayParams(agent_id="err1", extras={"trigger_agent_id": "err1"}),
            ctx=ctx,
        )

    # One short poll chunk, NOT the full 30-minute duration.
    assert mock_sleep.await_count == 1
    ctx.manager.attempt_recovery.assert_not_awaited()
    assert outcome.artifacts[0]["event"] == "break_skipped_draining"


@pytest.mark.asyncio
async def test_execute_without_drain_signal_uses_single_sleep():
    """When no drain signal is wired (is_draining=None), behavior is unchanged."""
    play = TakeBreakPlay()
    state = OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[_make_idle_agent(), _make_error_agent("err1")],
    )
    ctx = _make_ctx(break_duration_minutes=1)  # is_draining defaults to None
    ctx.manager.attempt_recovery = AsyncMock(return_value=True)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await play.execute(
            state,
            PlayParams(agent_id="err1", extras={"trigger_agent_id": "err1"}),
            ctx=ctx,
        )

    mock_sleep.assert_awaited_once_with(60)


@pytest.mark.asyncio
async def test_execute_aborts_when_drain_flips_during_final_chunk():
    """Drain flipping on the LAST sleep chunk must still skip recovery (#30).

    The per-chunk check happens before each sleep; a drain that flips during the
    final chunk is caught by the post-sleep guard so no recovery is attempted and
    no spurious break-recovery failure is recorded.
    """
    play = TakeBreakPlay()
    state = OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[_make_error_agent("err1")],
    )
    # Drain stays False for every pre-sleep check (one per chunk), then flips
    # True for the post-sleep guard call — exercising the final-chunk guard, not
    # the per-chunk check. chunks = ceil(duration / poll) for a 1-minute break.
    import math

    from agentshore.plays.internal.take_break import _DRAIN_POLL_SECONDS

    chunks = math.ceil(60 / _DRAIN_POLL_SECONDS)
    flags = iter([False] * chunks + [True] * 5)
    ctx = _make_draining_ctx(lambda: next(flags), break_duration_minutes=1)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        outcome = await play.execute(
            state,
            PlayParams(agent_id="err1", extras={"trigger_agent_id": "err1"}),
            ctx=ctx,
        )

    ctx.manager.attempt_recovery.assert_not_awaited()
    assert outcome.artifacts[0]["event"] == "break_skipped_draining"


@pytest.mark.asyncio
async def test_execute_returns_early_when_break_recovery_is_cancelled():
    """#367: clearing the target agent cancels the pending break immediately.

    Before the fix the play slept out the full break and then logged
    ``break_recovery_failed`` ~31 min after ``agent_cleared``, polluting recovery
    telemetry (and, via the recovery latches, masking a live override).
    """
    play = TakeBreakPlay()
    state = OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[_make_error_agent("err1")],
    )
    # A 30-minute break with real sleeps: only the cancel signal can end it fast.
    ctx = _make_ctx(break_duration_minutes=30)
    ctx.manager.attempt_recovery = AsyncMock(return_value=False)
    # Hand out a single, inspectable event so the "clear" can fire it.
    event = asyncio.Event()
    ctx.manager.register_break_recovery.side_effect = lambda _agent_id: event

    async def _clear_agent() -> None:
        # Wait until the play has registered its signal, then fire it as
        # AgentManager.clear() does.
        for _ in range(500):
            if ctx.manager.register_break_recovery.call_args is not None:
                break
            await asyncio.sleep(0.001)
        event.set()

    clearer = asyncio.create_task(_clear_agent())
    outcome = await asyncio.wait_for(
        play.execute(
            state,
            PlayParams(agent_id="err1", extras={"trigger_agent_id": "err1"}),
            ctx=ctx,
        ),
        timeout=5.0,
    )
    await clearer

    ctx.manager.attempt_recovery.assert_not_awaited()
    assert outcome.success is True
    assert outcome.agent_id == "err1"
    assert outcome.artifacts[0]["event"] == "break_skipped_agent_cleared"
    # The signal is de-registered so a later break can register its own.
    ctx.manager.unregister_break_recovery.assert_called_once_with("err1", event)


@pytest.mark.asyncio
async def test_execute_registers_and_unregisters_break_recovery_signal():
    """The cancel signal is registered before the sleep and released after it."""
    play = TakeBreakPlay()
    state = OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[_make_error_agent("err1")],
    )
    ctx = _make_ctx(break_duration_minutes=1)
    ctx.manager.attempt_recovery = AsyncMock(return_value=True)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        outcome = await play.execute(
            state,
            PlayParams(agent_id="err1", extras={"trigger_agent_id": "err1"}),
            ctx=ctx,
        )

    ctx.manager.register_break_recovery.assert_called_once_with("err1")
    assert ctx.manager.unregister_break_recovery.call_count == 1
    assert outcome.artifacts[0]["event"] == "break_completed"
