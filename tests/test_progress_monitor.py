"""Tests for ForwardProgressMonitor — the single autonomous-stop signal.

A tick makes forward progress iff a play was dispatched to an agent, an agent is
busy, or the beads/GitHub graph fingerprint changed. N consecutive no-progress
ticks trip a drain. The counter resets on any sign of progress.
"""

from __future__ import annotations

from agentshore.core.progress_monitor import ForwardProgressMonitor

# A stable fingerprint used for "no graph change" ticks.
_FP = (0.25, 4, 10, 8, 5)


def _dead_tick(monitor: ForwardProgressMonitor) -> bool:
    """A tick with no dispatch, no busy agent, and the same graph fingerprint."""
    return monitor.record_tick(dispatched_to_agent=False, any_agent_busy=False, fingerprint=_FP)


def test_first_tick_never_trips() -> None:
    monitor = ForwardProgressMonitor(no_progress_ticks=3)
    # First tick establishes the fingerprint baseline → counts as progress.
    assert _dead_tick(monitor) is False
    assert monitor.no_progress_ticks == 0


def test_trips_after_n_consecutive_dead_ticks() -> None:
    monitor = ForwardProgressMonitor(no_progress_ticks=3)
    _dead_tick(monitor)  # baseline
    assert _dead_tick(monitor) is False  # 1
    assert _dead_tick(monitor) is False  # 2
    assert _dead_tick(monitor) is True  # 3 → trip
    assert monitor.no_progress_ticks == 3


def test_dispatch_resets_counter() -> None:
    monitor = ForwardProgressMonitor(no_progress_ticks=3)
    _dead_tick(monitor)  # baseline
    _dead_tick(monitor)  # 1
    assert (
        monitor.record_tick(dispatched_to_agent=True, any_agent_busy=False, fingerprint=_FP)
        is False
    )
    assert monitor.no_progress_ticks == 0


def test_busy_agent_resets_counter() -> None:
    monitor = ForwardProgressMonitor(no_progress_ticks=3)
    _dead_tick(monitor)
    _dead_tick(monitor)
    assert (
        monitor.record_tick(dispatched_to_agent=False, any_agent_busy=True, fingerprint=_FP)
        is False
    )
    assert monitor.no_progress_ticks == 0


def test_graph_change_resets_counter() -> None:
    monitor = ForwardProgressMonitor(no_progress_ticks=3)
    _dead_tick(monitor)
    _dead_tick(monitor)
    # An issue/PR/beads-task created/closed/advanced changes the fingerprint.
    changed = (0.30, 5, 11, 7, 6)
    assert (
        monitor.record_tick(dispatched_to_agent=False, any_agent_busy=False, fingerprint=changed)
        is False
    )
    assert monitor.no_progress_ticks == 0


def test_interleaved_progress_never_trips() -> None:
    """A session that lands work every few ticks never trips (the false-positive guard)."""
    monitor = ForwardProgressMonitor(no_progress_ticks=3)
    _dead_tick(monitor)
    for _ in range(50):
        assert _dead_tick(monitor) is False  # 1
        assert _dead_tick(monitor) is False  # 2
        # ...then real work resets it before the 3rd dead tick.
        assert (
            monitor.record_tick(dispatched_to_agent=True, any_agent_busy=False, fingerprint=_FP)
            is False
        )
