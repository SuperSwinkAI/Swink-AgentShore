"""Tests for LoopProgressMonitor — no-op-spin detection + reprieve gating."""

from __future__ import annotations

import collections

from agentshore.core.progress_monitor import LoopProgressMonitor


class _FakeHost:
    """Minimal host exposing the two signals the monitor reads."""

    def __init__(self, *, velocity: float = 0.0) -> None:
        self._recent_play_outcomes: collections.deque[tuple[bool, str]] = collections.deque(
            maxlen=50
        )
        self._velocity = velocity

    def _compute_rolling_velocity(self, current_play_id: int) -> float:  # noqa: ARG002
        return self._velocity


def _fill(host: _FakeHost, entries: list[tuple[bool, str]]) -> None:
    host._recent_play_outcomes.extend(entries)


def test_detects_alternating_noop_skip_spin() -> None:
    # The incident: write_impl (skip:masked) ↔ reconcile (skip:no_target), all
    # skips, zero velocity. The all-category window sees both as skips.
    host = _FakeHost(velocity=0.0)
    _fill(
        host,
        [(True, "write_implementation_plan"), (True, "reconcile_state")] * 15,
    )
    monitor = LoopProgressMonitor(host=host)
    assert monitor.detect_noop_spin(total_plays=200) is True
    assert monitor.is_making_progress(total_plays=200) is False


def test_does_not_flag_when_velocity_present() -> None:
    # Any real progress (a merge / closed issue → velocity > 0) clears the spin
    # verdict even if the recent window is skip-heavy.
    host = _FakeHost(velocity=0.1)
    _fill(host, [(True, "reconcile_state")] * 30)
    monitor = LoopProgressMonitor(host=host)
    assert monitor.detect_noop_spin(total_plays=200) is False
    assert monitor.is_making_progress(total_plays=200) is True


def test_does_not_flag_short_window() -> None:
    host = _FakeHost(velocity=0.0)
    _fill(host, [(True, "reconcile_state")] * 5)  # below min_window
    monitor = LoopProgressMonitor(host=host)
    assert monitor.detect_noop_spin(total_plays=10) is False
    # Too early to judge → treated as progressing (don't over-stop).
    assert monitor.is_making_progress(total_plays=10) is True


def test_does_not_flag_productive_work_window() -> None:
    # Real (non-skip) plays across multiple types → not a spin, progressing.
    host = _FakeHost(velocity=0.0)
    _fill(
        host,
        [(False, "code_review"), (False, "unblock_pr"), (False, "issue_pickup")] * 10,
    )
    monitor = LoopProgressMonitor(host=host)
    assert monitor.detect_noop_spin(total_plays=200) is False
    assert monitor.is_making_progress(total_plays=200) is True


def test_single_productive_type_amid_skips_below_threshold_not_flagged() -> None:
    # A window that's only ~50% skips is not a spin (skip_rate < 0.80).
    host = _FakeHost(velocity=0.0)
    _fill(host, [(True, "reconcile_state"), (False, "code_review")] * 15)
    monitor = LoopProgressMonitor(host=host)
    assert monitor.detect_noop_spin(total_plays=200) is False
    assert monitor.is_making_progress(total_plays=200) is True
