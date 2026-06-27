"""Tests for the trunk-wedge idle auto-stop (desktop-kqo5, 2c).

When the main-repo dispatch pause latches and RECONCILE_STATE cannot clear it,
the loop would idle-with-work forever. The watchdog in
``LoopRunner.continue_if_selector_idle_work_remains`` escalates to a clean
drain-based stop after a grace window — gated strictly on (pause latched +
nothing in flight) so healthy capacity-idle never trips it.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from agentshore.core.mixins.loop import _WEDGED_IDLE_STOP_TICKS
from agentshore.core.orchestrator import Orchestrator
from agentshore.state import OrchestratorState, SessionState


def _harness(tmp_path: Path, *, paused: bool, in_flight: bool, ticks: int) -> Orchestrator:
    from tests.orchestrator_factory import make_test_orchestrator

    orch = make_test_orchestrator(tmp_path)
    orch._session_id = "t"
    orch._main_repo.dispatch_paused = paused
    orch._in_flight = {"a": object()} if in_flight else {}  # type: ignore[dict-item]
    orch._loop._wedged_idle_ticks = ticks
    return orch


def _state() -> OrchestratorState:
    return OrchestratorState(
        session_id="t",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
    )


@pytest.mark.asyncio
async def test_latched_pause_auto_stops_after_grace(tmp_path: Path) -> None:
    orch = _harness(tmp_path, paused=True, in_flight=False, ticks=_WEDGED_IDLE_STOP_TICKS - 1)
    cont = await orch._loop.continue_if_selector_idle_work_remains(
        _state(), reason="unchanged_digest"
    )
    assert cont is False
    assert orch._draining is True
    assert orch._drain_reason == "main_repo_wedged"
    assert orch._pause_event.is_set()


@pytest.mark.asyncio
async def test_in_flight_work_does_not_trip_watchdog(tmp_path: Path) -> None:
    # Pause latched but a play is in flight → not the wedge signature; counter resets, no stop.
    orch = _harness(tmp_path, paused=True, in_flight=True, ticks=_WEDGED_IDLE_STOP_TICKS - 1)
    # Downstream candidate-plan path is unstubbed; only assert the watchdog branch didn't fire.
    orch._registry = None
    with contextlib.suppress(Exception):
        await orch._loop.continue_if_selector_idle_work_remains(_state(), reason="x")
    assert orch._draining is False
    assert orch._loop._wedged_idle_ticks == 0
