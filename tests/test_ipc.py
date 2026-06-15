"""Tests for orchestrator -> IPC state-provider hooks.

Covers cross-cutting events the orchestrator emits via the StateProvider
protocol that aren't tested in tests/ipc/test_provider.py (which only
exercises the IpcStateProvider directly).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentshore.config import RuntimeConfig
from agentshore.core import Orchestrator
from agentshore.core.context import _DispatchContext
from agentshore.plays.base import PlayParams
from agentshore.state import (
    AgentStatus,
    PlayOutcome,
    PlayType,
)

if TYPE_CHECKING:
    from pathlib import Path


def _outcome(*, agent_id: str | None = "agent-1", success: bool = True) -> PlayOutcome:
    return PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id=agent_id,
        success=success,
        partial=False,
        duration_seconds=1.0,
        token_cost=100,
        dollar_cost=0.01,
        artifacts=[],
        alignment_delta=0.0,
        play_id=42,
    )


async def _drive_completion(
    orch: Orchestrator,
    outcome: PlayOutcome,
) -> None:
    """Push a fake outcome through ``_process_completion`` end-to-end."""
    state = await orch._state_builder.build_state()
    ctx = _DispatchContext(
        dispatch_id="d-test",
        play_type=outcome.play_type,
        params=PlayParams(agent_id=outcome.agent_id),
        state_at_dispatch=state,
        pending_step=None,
        dispatched_at=time.perf_counter(),
    )
    orch._dispatch_ctx["d-test"] = ctx

    async def _result() -> PlayOutcome:
        return outcome

    task: asyncio.Task[PlayOutcome] = asyncio.create_task(_result())
    await task
    await orch._completion.process_completion("d-test", task)


@pytest.mark.asyncio
async def test_play_completion_emits_agent_idle(tmp_path: Path) -> None:
    """After a successful play completes, the orchestrator must emit
    ``on_agent_changed(agent_id, IDLE)`` via the state provider so the
    dashboard's agents panel flips Busy->Idle without waiting for the
    next full state_update snapshot."""
    orch = await Orchestrator.bootstrap(cfg=RuntimeConfig(), repo_root=tmp_path)

    state_provider = MagicMock()
    state_provider.on_play_completed = AsyncMock()
    state_provider.on_agent_changed = AsyncMock()
    state_provider.on_state_update = AsyncMock()
    state_provider.on_play_started = AsyncMock()
    state_provider.on_feedback_requested = AsyncMock()
    state_provider.on_session_paused = AsyncMock()
    orch._state_provider = state_provider

    # Pretend "agent-1" is currently registered with the agent manager.
    # Pin the circuit-breaker counters to real ints — build_state copies these
    # onto the AgentSnapshot, and the eligibility reaper compares them with >=.
    handle = MagicMock()
    handle.timeout_count = 0
    handle.consecutive_timeouts = 0
    orch._manager.handles["agent-1"] = handle

    async with orch:
        await _drive_completion(orch, _outcome(agent_id="agent-1"))

    # The IDLE notification was emitted at least once with the right args,
    # in addition to the on_play_completed and post on_state_update calls.
    state_provider.on_play_completed.assert_awaited()
    idle_calls = [
        call
        for call in state_provider.on_agent_changed.await_args_list
        if call.args == ("agent-1", AgentStatus.IDLE)
    ]
    assert idle_calls, (
        f"expected at least one on_agent_changed('agent-1', IDLE) call; "
        f"got {state_provider.on_agent_changed.await_args_list}"
    )


@pytest.mark.asyncio
async def test_play_completion_skips_idle_emit_when_agent_unknown(tmp_path: Path) -> None:
    """If the agent_id isn't in self._manager.handles (e.g. the agent crashed
    and was cleared between dispatch and completion), don't emit a stale
    IDLE flip — there's nothing to flip."""
    orch = await Orchestrator.bootstrap(cfg=RuntimeConfig(), repo_root=tmp_path)

    state_provider = MagicMock()
    state_provider.on_play_completed = AsyncMock()
    state_provider.on_agent_changed = AsyncMock()
    state_provider.on_state_update = AsyncMock()
    state_provider.on_play_started = AsyncMock()
    state_provider.on_feedback_requested = AsyncMock()
    state_provider.on_session_paused = AsyncMock()
    orch._state_provider = state_provider

    # No "agent-1" handle registered.
    orch._manager.handles.pop("agent-1", None)

    async with orch:
        await _drive_completion(orch, _outcome(agent_id="agent-1"))

    # No IDLE call for the unknown agent.
    idle_calls = [
        call
        for call in state_provider.on_agent_changed.await_args_list
        if call.args == ("agent-1", AgentStatus.IDLE)
    ]
    assert not idle_calls, f"unexpected IDLE emit: {idle_calls}"


@pytest.mark.asyncio
async def test_play_completion_skips_idle_emit_when_no_agent_id(tmp_path: Path) -> None:
    """Internal/synthetic plays may complete with ``agent_id=None``; the
    eager IDLE emit must be skipped (there's no agent to notify about)."""
    orch = await Orchestrator.bootstrap(cfg=RuntimeConfig(), repo_root=tmp_path)

    state_provider = MagicMock()
    state_provider.on_play_completed = AsyncMock()
    state_provider.on_agent_changed = AsyncMock()
    state_provider.on_state_update = AsyncMock()
    state_provider.on_play_started = AsyncMock()
    state_provider.on_feedback_requested = AsyncMock()
    state_provider.on_session_paused = AsyncMock()
    orch._state_provider = state_provider

    async with orch:
        await _drive_completion(orch, _outcome(agent_id=None))

    state_provider.on_agent_changed.assert_not_awaited()
