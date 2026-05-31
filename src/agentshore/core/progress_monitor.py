"""Loop progress assessment — no-op-spin detection and reprieve gating.

A live session burned ~84% of 939 plays alternating two plays that both
deterministically *skip* at $0 (``write_implementation_plan`` skip:masked ↔
``reconcile_state`` skip:no_target), making zero progress while merge-ready PRs
sat unmerged — and **no detector fired**. The existing detectors are blind to it:
loop-detection keys on *same-type* streaks (an A↔B alternation never builds one),
stagnation/liveness reset on every $0 completion, and the masked-only
``_executor_skip_window`` records ``False`` for ``no_target`` skips so a
masked-only rate sits near 0.5.

``LoopProgressMonitor`` consolidates the progress signals into a single pure
assessor (no side effects — callers decide what to do). It reads an all-category
skip window plus the rolling-velocity signal already maintained on the
orchestrator. Two deliberately *non-complementary* predicates:

* ``detect_noop_spin`` — strict; drives the backstop pause. A borderline session
  is **not** flagged.
* ``is_making_progress`` — permissive; gates the WS3 unanswered-pause reprieve. A
  borderline session is still treated as progressing so a legitimately-slow run
  is never starved of its reprieve.

This is a deterministic backstop, not a policy director: it never influences
*which* play the PPO selects — it only detects a degenerate state so the loop can
stop, and tells the reprieve guard whether the loop is actually advancing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import collections


# A play with no real work done — counts toward the spin signal. Defaults chosen
# against the incident: the spin held a ~1.0 all-skip rate over hundreds of plays.
_DEFAULT_SKIP_RATE_THRESHOLD = 0.80
# Minimum completed plays in the window before any spin verdict — avoids flagging
# a cold start or a brief lull as a spin.
_DEFAULT_MIN_WINDOW = 20


class _ProgressStateHost(Protocol):
    """The slice of orchestrator state the monitor reads."""

    # Rolling window of recent completed plays: (was_skip, play_type_value).
    _recent_play_outcomes: collections.deque[tuple[bool, str]]

    def _compute_rolling_velocity(self, current_play_id: int) -> float: ...


class LoopProgressMonitor:
    """Pure assessor of whether the loop is progressing or spinning on no-ops."""

    def __init__(
        self,
        *,
        host: _ProgressStateHost,
        skip_rate_threshold: float = _DEFAULT_SKIP_RATE_THRESHOLD,
        min_window: int = _DEFAULT_MIN_WINDOW,
    ) -> None:
        self._host = host
        self._skip_rate_threshold = skip_rate_threshold
        self._min_window = min_window

    # ------------------------------------------------------------------
    # Window readers
    # ------------------------------------------------------------------

    def _window(self) -> collections.deque[tuple[bool, str]]:
        return self._host._recent_play_outcomes

    def all_skip_rate(self) -> float:
        """Fraction of recent completed plays that were no-op skips (any category)."""
        window = self._window()
        if not window:
            return 0.0
        return sum(1 for (was_skip, _pt) in window if was_skip) / len(window)

    def distinct_productive_types(self) -> int:
        """Distinct play types among recent *non-skip* (real-work) completions."""
        return len({pt for (was_skip, pt) in self._window() if not was_skip})

    # ------------------------------------------------------------------
    # Verdicts
    # ------------------------------------------------------------------

    def detect_noop_spin(self, *, total_plays: int) -> bool:
        """True only when the loop is clearly spinning on no-op skips (strict).

        All four must hold: a full-enough window, **zero** rolling velocity (the
        false-positive guard — any merged PR / closed issue clears it), an
        all-category skip rate at/above the threshold, and at most one distinct
        productive play type. A session landing any real work is never flagged.
        """
        window = self._window()
        if len(window) < self._min_window:
            return False
        if self._host._compute_rolling_velocity(total_plays) > 0.0:
            return False
        if self.all_skip_rate() < self._skip_rate_threshold:
            return False
        return self.distinct_productive_types() <= 1

    def is_making_progress(self, *, total_plays: int) -> bool:
        """True unless the loop looks stalled on no-ops (permissive).

        Used to gate the unanswered-pause reprieve: a session with real velocity,
        too short a window to judge, or a sub-threshold skip rate is treated as
        progressing so it keeps its reprieve. Only a window dominated by skips is
        called not-progressing.
        """
        if self._host._compute_rolling_velocity(total_plays) > 0.0:
            return True
        window = self._window()
        if len(window) < self._min_window:
            return True
        return self.all_skip_rate() < self._skip_rate_threshold
