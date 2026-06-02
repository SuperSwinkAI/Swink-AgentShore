"""Tests for the trunk-wedge idle auto-stop (desktop-kqo5, 2c).

When the main-repo dispatch pause latches and RECONCILE_STATE cannot clear it,
the loop would idle-with-work forever. The watchdog in
``_continue_if_selector_idle_work_remains`` escalates to a clean drain-based
stop after a grace window — gated strictly on (pause latched + nothing in
flight) so healthy capacity-idle never trips it.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from agentshore.core.main_repo_guard import MainRepoGuard
from agentshore.core.mixins.loop import _WEDGED_IDLE_STOP_TICKS
from agentshore.core.orchestrator import Orchestrator
from agentshore.state import OrchestratorState, SessionState


def _harness(*, paused: bool, in_flight: bool, ticks: int) -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch._session_id = "t"
    orch._main_repo = MainRepoGuard()
    orch._main_repo.dispatch_paused = paused
    orch._in_flight = {"a": object()} if in_flight else {}  # type: ignore[dict-item]
    orch._wedged_idle_ticks = ticks
    orch._draining = False
    orch._drain_reason = None
    orch._pause_event = asyncio.Event()
    return orch


def _state() -> OrchestratorState:
    return OrchestratorState(
        session_id="t",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
    )


@pytest.mark.asyncio
async def test_latched_pause_auto_stops_after_grace() -> None:
    orch = _harness(paused=True, in_flight=False, ticks=_WEDGED_IDLE_STOP_TICKS - 1)
    cont = await orch._continue_if_selector_idle_work_remains(_state(), reason="unchanged_digest")
    assert cont is False
    assert orch._draining is True
    assert orch._drain_reason == "main_repo_wedged"
    assert orch._pause_event.is_set()


@pytest.mark.asyncio
async def test_in_flight_work_does_not_trip_watchdog() -> None:
    # Pause latched but a play is in flight → not the wedge signature; the
    # counter must reset and no auto-stop fires from this guard.
    orch = _harness(paused=True, in_flight=True, ticks=_WEDGED_IDLE_STOP_TICKS - 1)
    # The downstream candidate-plan path needs collaborators we don't stub here,
    # so we only assert the watchdog branch did not fire before that point.
    orch._registry = None
    # downstream candidate-plan path may need more wiring; we only assert the
    # watchdog guard branch did not fire.
    with contextlib.suppress(Exception):
        await orch._continue_if_selector_idle_work_remains(_state(), reason="x")
    assert orch._draining is False
    assert orch._wedged_idle_ticks == 0
