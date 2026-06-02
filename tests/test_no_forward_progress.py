"""Integration test for CompletionProcessor.check_no_forward_progress.

Verifies the completion-component hook computes the per-tick inputs from
state/outcome, feeds the ForwardProgressMonitor, and drains directly once the
threshold trips.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest

from agentshore.core import Orchestrator
from agentshore.core.mixins.completion import CompletionProcessor
from agentshore.core.mixins.loop import LoopRunner
from agentshore.core.progress_monitor import ForwardProgressMonitor
from agentshore.state import (
    OrchestratorState,
    PlayOutcome,
    PlayType,
    SessionState,
)


def _completion(limit: int = 2) -> types.SimpleNamespace:
    host = types.SimpleNamespace(
        _progress_monitor=ForwardProgressMonitor(no_progress_ticks=limit),
        _draining=False,
        _stop_requested=False,
        _session_id="s1",
        _natural_exit_reason=None,
        _drain_reason=None,
        _pause_deadline=None,
        _pause_event=None,
        _drain=types.SimpleNamespace(begin_drain=AsyncMock()),
    )
    # check_no_forward_progress routes the stop through the host
    # _initiate_autonomous_stop delegator, which forwards to
    # self._loop.initiate_autonomous_stop → self._drain.begin_drain.
    loop = types.SimpleNamespace(_host=host, _drain=host._drain)
    loop.initiate_autonomous_stop = types.MethodType(LoopRunner.initiate_autonomous_stop, loop)
    host._loop = loop
    host._initiate_autonomous_stop = types.MethodType(Orchestrator._initiate_autonomous_stop, host)
    # The processor reads runtime state via self._host and its own _session_id
    # (a constructor dep). Build a stand-in with just those two surfaces.
    return types.SimpleNamespace(_host=host, _session_id="s1")


def _state() -> OrchestratorState:
    return OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[],
        pull_requests=[],
        open_issues=[],
    )


def _skip() -> PlayOutcome:
    return PlayOutcome.skipped_outcome(PlayType.WRITE_IMPLEMENTATION_PLAN, "masked")


def _dispatched() -> PlayOutcome:
    return PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id="agent-1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=0,
        dollar_cost=0.1,
        artifacts=[],
        alignment_delta=0.0,
    )


@pytest.mark.asyncio
async def test_drains_after_threshold_dead_ticks() -> None:
    completion = _completion(limit=2)
    state, outcome = _state(), _skip()
    # baseline tick (no trip), then two dead ticks → trip on the second.
    for _ in range(3):
        await CompletionProcessor.check_no_forward_progress(completion, state, outcome)
    completion._host._drain.begin_drain.assert_awaited_once_with("no_forward_progress")
    assert completion._host._drain_reason == "no_forward_progress"
    assert completion._host._natural_exit_reason == "no_forward_progress"


@pytest.mark.asyncio
async def test_agent_dispatch_resets_and_prevents_drain() -> None:
    completion = _completion(limit=2)
    state = _state()
    await CompletionProcessor.check_no_forward_progress(completion, state, _skip())  # baseline
    await CompletionProcessor.check_no_forward_progress(completion, state, _skip())  # dead 1
    # A real dispatch resets the counter, so the next skip is only dead-tick #1.
    await CompletionProcessor.check_no_forward_progress(completion, state, _dispatched())
    await CompletionProcessor.check_no_forward_progress(completion, state, _skip())
    completion._host._drain.begin_drain.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_op_when_already_draining() -> None:
    completion = _completion(limit=1)
    completion._host._draining = True
    await CompletionProcessor.check_no_forward_progress(completion, _state(), _skip())
    await CompletionProcessor.check_no_forward_progress(completion, _state(), _skip())
    completion._host._drain.begin_drain.assert_not_awaited()
