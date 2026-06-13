"""Forward-progress assessment — the single autonomous-stop signal.

The old approach stacked three overlapping detectors (same-type-streak
loop-detection, a strict no-op-spin detector, and a time-based stagnation
ladder) and still missed a live session that burned ~253 plays at 94%
``write_implementation_plan`` skip:masked with alignment frozen — because every
detector watched play *activity* (streaks, skip rate, completion velocity)
rather than project *advancement*. An interleaved write_impl↔refine churn keeps
all of those signals "healthy" while the graph never moves.

``ForwardProgressMonitor`` replaces them with one rule the owner specified: a
tick makes **forward progress** iff a play was dispatched to an agent, an agent
is busy, or the beads/GitHub graph fingerprint changed (an issue/PR/task was
created, closed, or advanced). N consecutive no-progress ticks → the session
should drain. The host computes the per-tick inputs (it owns state); the monitor
owns only the counter, so it is trivially unit-testable.

This is a deterministic backstop, not a policy director: it never influences
*which* play the PPO selects — it only stops a session that has stopped making
progress.
"""

from __future__ import annotations

from typing import Final

# Consecutive no-progress ticks before the session drains. A tick with any busy
# agent, any agent dispatch, or a graph-fingerprint change resets the counter,
# so this is a sustained full stall independent of normal play cooldown pacing.
_DEFAULT_NO_PROGRESS_TICKS: Final[int] = 20

# Fingerprint of project advancement. Comparing this tuple across ticks detects
# an issue/PR/beads-task being created, closed, or advanced without an extra DB
# read (all fields come from next_state + the already-built candidate plan).
GraphFingerprint = tuple[float, int, int, int, int]


class ForwardProgressMonitor:
    """Counts consecutive no-forward-progress ticks and trips a stop."""

    def __init__(self, *, no_progress_ticks: int = _DEFAULT_NO_PROGRESS_TICKS) -> None:
        self._limit = no_progress_ticks
        self._last_fingerprint: GraphFingerprint | None = None
        self._no_progress_ticks = 0

    @property
    def no_progress_ticks(self) -> int:
        return self._no_progress_ticks

    @property
    def limit(self) -> int:
        return self._limit

    def record_tick(
        self,
        *,
        dispatched_to_agent: bool,
        any_agent_busy: bool,
        fingerprint: GraphFingerprint,
    ) -> bool:
        """Record one loop tick; return True when the no-progress threshold trips.

        Forward progress = a play dispatched to an agent, an agent currently
        busy, or the graph fingerprint changed vs the prior tick. Any of these
        resets the counter to 0. The first tick establishes the fingerprint
        baseline and never trips. Once ``limit`` consecutive no-progress ticks
        accumulate, returns True so the caller can drain.
        """
        first_tick = self._last_fingerprint is None
        graph_changed = not first_tick and fingerprint != self._last_fingerprint
        self._last_fingerprint = fingerprint
        if first_tick or dispatched_to_agent or any_agent_busy or graph_changed:
            self._no_progress_ticks = 0
            return False
        self._no_progress_ticks += 1
        return self._no_progress_ticks >= self._limit
