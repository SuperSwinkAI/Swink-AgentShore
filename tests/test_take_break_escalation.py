"""Consecutive take_break failures mark an agent recovery-exhausted (Item 6).

If ``attempt_recovery`` keeps returning False, the agent stays in ERROR and the
loop must not re-schedule break → break → break indefinitely. The contract:

* ``TakeBreakPlay.execute`` returns ``success=False`` when recovery fails.
* The completion mixin counts consecutive take_break failures per agent in
  ``_break_recovery_failures``. Once the count reaches
  ``BREAK_RECOVERY_FAILURE_LIMIT`` the handler NO LONGER force-enqueues an
  ``END_AGENT`` override (extreme-bypass / Item 6). Instead it leaves the
  counter ELEVATED at/above the limit and logs ``break_recovery_exhausted``.
  The core tick reads ``_break_recovery_failures[agent_id] >=
  BREAK_RECOVERY_FAILURE_LIMIT`` to unmask END_AGENT so the PPO decides.
* The counter is cleared on a successful recovery and when the agent is
  actually ended (END_AGENT play completes), so a re-instantiated agent reusing
  the id doesn't inherit a stale count.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.core.mixins.completion import (
    BREAK_RECOVERY_FAILURE_LIMIT,
    _CompletionMixin,
)
from agentshore.plays.base import PlayExecutionContext, PlayParams
from agentshore.plays.internal.take_break import TakeBreakPlay
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    OrchestratorState,
    PlayOutcome,
    PlayType,
    SessionState,
)


def _error_agent(agent_id: str = "err1") -> AgentSnapshot:
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


def _state(agents: list[AgentSnapshot]) -> OrchestratorState:
    return OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=agents,
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


@pytest.mark.asyncio
async def test_take_break_returns_failure_when_recovery_fails() -> None:
    """attempt_recovery returning False must surface as success=False."""
    play = TakeBreakPlay()
    state = _state([_error_agent("err1")])
    ctx = _make_ctx(break_duration_minutes=1)
    ctx.manager.attempt_recovery = AsyncMock(return_value=False)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        outcome = await play.execute(
            state,
            PlayParams(
                agent_id="err1",
                extras={"trigger_agent_id": "err1", "trigger_error_class": "unknown"},
            ),
            ctx=ctx,
        )

    assert outcome.success is False
    assert outcome.error == "attempt_recovery_failed"
    assert outcome.artifacts[0]["event"] == "break_recovery_failed"
    assert outcome.artifacts[0]["recovered_agents"] == []


@pytest.mark.asyncio
async def test_take_break_returns_success_when_recovery_succeeds() -> None:
    play = TakeBreakPlay()
    state = _state([_error_agent("err1")])
    ctx = _make_ctx(break_duration_minutes=1)
    ctx.manager.attempt_recovery = AsyncMock(return_value=True)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        outcome = await play.execute(
            state,
            PlayParams(
                agent_id="err1",
                extras={"trigger_agent_id": "err1", "trigger_error_class": "unknown"},
            ),
            ctx=ctx,
        )

    assert outcome.success is True
    assert outcome.artifacts[0]["event"] == "break_completed"
    assert outcome.artifacts[0]["recovered_agents"] == ["err1"]


class _Harness(_CompletionMixin):
    """Minimal completion mixin stand-in for testing _handle_take_break_outcome."""

    def __init__(self) -> None:
        self._session_id = "s1"
        self._override_queue = asyncio.Queue()
        self._break_recovery_failures = {}
        self._rate_limit_recovery_enqueued = set()


def _outcome(*, agent_id: str, success: bool) -> PlayOutcome:
    return PlayOutcome(
        play_type=PlayType.TAKE_BREAK,
        agent_id=agent_id,
        success=success,
        partial=False,
        duration_seconds=0.0,
        token_cost=0,
        dollar_cost=0.0,
        artifacts=[],
        alignment_delta=0.0,
    )


def test_two_consecutive_break_failures_do_not_enqueue_end_agent_override() -> None:
    """Item 6: reaching the limit must NOT force-enqueue an END_AGENT override.

    The PPO — not this handler — ends a wedged agent. The handler only tracks
    the counter and leaves it elevated for the core tick to read.
    """
    h = _Harness()

    h._handle_take_break_outcome(_outcome(agent_id="a1", success=False))
    assert h._override_queue.empty()
    assert h._break_recovery_failures["a1"] == 1

    h._handle_take_break_outcome(_outcome(agent_id="a1", success=False))

    # No override of any kind is produced.
    assert h._override_queue.empty()


def test_break_recovery_counter_persists_at_limit() -> None:
    """CRITICAL CONTRACT: at the limit the counter is NOT popped.

    The core tick reads ``_break_recovery_failures[agent_id] >=
    BREAK_RECOVERY_FAILURE_LIMIT`` each tick to unmask END_AGENT for the PPO, so
    the count must stay elevated until the agent is actually ended.
    """
    h = _Harness()

    for _ in range(BREAK_RECOVERY_FAILURE_LIMIT):
        h._handle_take_break_outcome(_outcome(agent_id="a1", success=False))

    assert h._break_recovery_failures["a1"] == BREAK_RECOVERY_FAILURE_LIMIT
    assert h._break_recovery_failures["a1"] >= BREAK_RECOVERY_FAILURE_LIMIT

    # Further failures keep the counter at/above the limit (never reset here).
    h._handle_take_break_outcome(_outcome(agent_id="a1", success=False))
    assert h._break_recovery_failures["a1"] == BREAK_RECOVERY_FAILURE_LIMIT + 1
    assert h._override_queue.empty()


def test_break_recovery_failure_limit_is_two() -> None:
    """The limit is exactly 2 — desktop-s1u7 specified a small finite count."""
    assert BREAK_RECOVERY_FAILURE_LIMIT == 2


def test_successful_break_clears_failure_counter() -> None:
    """A recovered break resets the per-agent counter so the next failure starts fresh."""
    h = _Harness()
    h._handle_take_break_outcome(_outcome(agent_id="a1", success=False))
    assert h._break_recovery_failures["a1"] == 1

    h._handle_take_break_outcome(_outcome(agent_id="a1", success=True))
    assert "a1" not in h._break_recovery_failures


def test_failures_on_different_agents_do_not_share_a_counter() -> None:
    h = _Harness()
    h._handle_take_break_outcome(_outcome(agent_id="a1", success=False))
    h._handle_take_break_outcome(_outcome(agent_id="a2", success=False))

    assert h._break_recovery_failures == {"a1": 1, "a2": 1}
    assert h._override_queue.empty()
