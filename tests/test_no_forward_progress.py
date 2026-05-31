"""Integration test for _check_no_forward_progress (completion-mixin wiring).

Verifies the orchestrator hook computes the per-tick inputs from state/outcome,
feeds the ForwardProgressMonitor, and drains directly once the threshold trips.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest

from agentshore.core import Orchestrator
from agentshore.core.progress_monitor import ForwardProgressMonitor
from agentshore.state import (
    OrchestratorState,
    PlayOutcome,
    PlayType,
    SessionState,
)


def _host(limit: int = 2) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        _progress_monitor=ForwardProgressMonitor(no_progress_ticks=limit),
        _draining=False,
        _stop_requested=False,
        _session_id="s1",
        _natural_exit_reason=None,
        _drain_reason=None,
        begin_drain=AsyncMock(),
    )


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
    host = _host(limit=2)
    state, outcome = _state(), _skip()
    # baseline tick (no trip), then two dead ticks → trip on the second.
    for _ in range(3):
        await Orchestrator._check_no_forward_progress(host, state, outcome)
    host.begin_drain.assert_awaited_once_with("no_forward_progress")
    assert host._drain_reason == "no_forward_progress"
    assert host._natural_exit_reason == "no_forward_progress"


@pytest.mark.asyncio
async def test_agent_dispatch_resets_and_prevents_drain() -> None:
    host = _host(limit=2)
    state = _state()
    await Orchestrator._check_no_forward_progress(host, state, _skip())  # baseline
    await Orchestrator._check_no_forward_progress(host, state, _skip())  # dead 1
    # A real dispatch resets the counter, so the next skip is only dead-tick #1.
    await Orchestrator._check_no_forward_progress(host, state, _dispatched())
    await Orchestrator._check_no_forward_progress(host, state, _skip())
    host.begin_drain.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_op_when_already_draining() -> None:
    host = _host(limit=1)
    host._draining = True
    await Orchestrator._check_no_forward_progress(host, _state(), _skip())
    await Orchestrator._check_no_forward_progress(host, _state(), _skip())
    host.begin_drain.assert_not_awaited()
