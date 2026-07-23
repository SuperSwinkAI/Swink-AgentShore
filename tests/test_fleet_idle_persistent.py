"""desktop-85ex regression: ``fleet_idle_persistent`` emits per-transition only.

Memory ``project_loop_detector_warning_storm`` documents a real-life bug
where ``loop_detected`` re-emitted per tick instead of per streak
transition (55 events/sec during the 2026-05-07 run).
``fleet_idle_persistent`` is the sibling signal for "selector returned None
for N consecutive ticks", and it must NOT replay that pattern.

Coverage:
* Crossing the threshold from below emits exactly one ``transition=entered``
  event.
* Subsequent ticks above the threshold emit NOTHING (no per-tick storm).
* A successful dispatch — or, equivalently, in-flight work appearing —
  exits the window and emits exactly one ``transition=exited`` event.
* A new entry after exit re-arms cleanly (emits one entered event).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentshore.core import Orchestrator
from agentshore.state import SessionState


@dataclass
class _StateStub:
    """Minimal ``OrchestratorState``-shaped object the check function reads."""

    session_state: SessionState = SessionState.RUNNING
    in_flight_plays: tuple[Any, ...] = ()


def _orch(tmp_path: Path) -> Orchestrator:
    from tests.orchestrator_factory import make_test_orchestrator

    orch = make_test_orchestrator(tmp_path)
    orch._session_id = "sess-test-85ex"
    return orch


@pytest.fixture
def info_calls(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch ``loop.py``'s ``_logger`` and yield the info() mock.

    ``fleet_idle_persistent`` is emitted from ``agentshore.core.mixins.loop``,
    so replacing that module's ``_logger`` binding captures the calls.
    """
    mock_logger = MagicMock()
    monkeypatch.setattr("agentshore.core.mixins.loop._logger", mock_logger)
    return mock_logger.info


def _events_named(info_mock: MagicMock, name: str) -> list[dict[str, Any]]:
    """Return kwargs dicts for every ``info(name, **kwargs)`` call."""
    matches: list[dict[str, Any]] = []
    for call in info_mock.call_args_list:
        args, kwargs = call
        if args and args[0] == name:
            matches.append(dict(kwargs))
    return matches


async def _run_check(orch: Orchestrator, state: _StateStub, *, reason: str) -> None:
    await orch._loop.check_fleet_idle_persistent(  # type: ignore[arg-type]
        state, reason=reason, mask_reasons=[]
    )


@pytest.mark.asyncio
async def test_threshold_crossing_emits_one_entered_event(
    info_calls: MagicMock, tmp_path: Path
) -> None:
    """Idle streak crossing the threshold from below ⇒ exactly one event."""
    orch = _orch(tmp_path)
    state = _StateStub()

    # Below threshold — no event.
    for streak in range(orch._runtime.cfg.rl.loop_detection.fleet_idle_threshold):
        orch._runtime.idle_streak = streak
        await _run_check(orch, state, reason="selector_returned_none")
    assert _events_named(info_calls, "fleet_idle_persistent") == []
    assert orch._loop._fleet_idle_persistent_active is False

    # First tick at threshold — exactly one event, then activated.
    orch._runtime.idle_streak = orch._runtime.cfg.rl.loop_detection.fleet_idle_threshold
    await _run_check(orch, state, reason="selector_returned_none")
    events = _events_named(info_calls, "fleet_idle_persistent")
    assert len(events) == 1
    assert events[0]["transition"] == "entered"
    assert events[0]["dominant_reason"] == "selector_returned_none"
    assert orch._loop._fleet_idle_persistent_active is True


@pytest.mark.asyncio
async def test_no_per_tick_storm_inside_window(info_calls: MagicMock, tmp_path: Path) -> None:
    """Once active, no further emissions until the state transitions."""
    orch = _orch(tmp_path)
    state = _StateStub()
    orch._runtime.idle_streak = orch._runtime.cfg.rl.loop_detection.fleet_idle_threshold

    # 50 ticks inside the window — should produce ONE event (entry), not 50.
    for _ in range(50):
        orch._runtime.idle_streak += 1
        await _run_check(orch, state, reason="selector_returned_none")

    assert len(_events_named(info_calls, "fleet_idle_persistent")) == 1


@pytest.mark.asyncio
async def test_exit_transition_emits_one_event_when_in_flight_appears(
    info_calls: MagicMock, tmp_path: Path
) -> None:
    """Work appearing in-flight closes the window with exactly one event."""
    orch = _orch(tmp_path)
    state = _StateStub()
    orch._runtime.idle_streak = orch._runtime.cfg.rl.loop_detection.fleet_idle_threshold

    # Enter the window.
    await _run_check(orch, state, reason="selector_returned_none")
    assert orch._loop._fleet_idle_persistent_active is True

    # Simulate in-flight work arriving.
    orch._runtime.in_flight = {"dispatch-1": asyncio.Future()}  # type: ignore[dict-item]
    await _run_check(orch, state, reason="selector_returned_none")

    # Two events total: one entered, one exited. Per-tick storm would push 3+.
    events = _events_named(info_calls, "fleet_idle_persistent")
    assert len(events) == 2
    assert events[0]["transition"] == "entered"
    assert events[1]["transition"] == "exited"
    assert orch._loop._fleet_idle_persistent_active is False


@pytest.mark.asyncio
async def test_exit_transition_emits_one_event_when_streak_collapses(
    info_calls: MagicMock, tmp_path: Path
) -> None:
    """Streak dropping below threshold also closes the window."""
    orch = _orch(tmp_path)
    state = _StateStub()
    orch._runtime.idle_streak = orch._runtime.cfg.rl.loop_detection.fleet_idle_threshold

    await _run_check(orch, state, reason="selector_returned_none")
    assert orch._loop._fleet_idle_persistent_active is True

    # Selector picked a play → streak resets to 0.
    orch._runtime.idle_streak = 0
    await _run_check(orch, state, reason="selector_returned_none")
    assert orch._loop._fleet_idle_persistent_active is False
    events = _events_named(info_calls, "fleet_idle_persistent")
    assert len(events) == 2
    assert [e["transition"] for e in events] == ["entered", "exited"]


@pytest.mark.asyncio
async def test_window_can_rearm_after_exit(info_calls: MagicMock, tmp_path: Path) -> None:
    """After exiting, a fresh threshold cross emits a new entered event."""
    orch = _orch(tmp_path)
    state = _StateStub()

    # Enter
    orch._runtime.idle_streak = orch._runtime.cfg.rl.loop_detection.fleet_idle_threshold
    await _run_check(orch, state, reason="selector_returned_none")

    # Exit
    orch._runtime.idle_streak = 0
    await _run_check(orch, state, reason="selector_returned_none")

    # Re-enter
    orch._runtime.idle_streak = orch._runtime.cfg.rl.loop_detection.fleet_idle_threshold
    await _run_check(orch, state, reason="selector_returned_none")

    # entered, exited, entered = 3 events total.
    events = _events_named(info_calls, "fleet_idle_persistent")
    assert len(events) == 3
    assert [e["transition"] for e in events] == ["entered", "exited", "entered"]
    assert orch._loop._fleet_idle_persistent_active is True


def test_default_fleet_idle_threshold_is_30() -> None:
    """The plan pins the default to 30 ticks — keep this value visible in tests."""
    from agentshore.config.models import LoopDetectionConfig

    cfg = LoopDetectionConfig()
    assert cfg.fleet_idle_threshold == 30
