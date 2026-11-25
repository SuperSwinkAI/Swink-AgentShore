"""Forward-progress assessment for autonomous stopping.

A tick makes forward progress when a play is dispatched, an agent is busy,
or the beads/GitHub graph fingerprint changes. After N consecutive ticks
without progress the session should drain. The host supplies per-tick inputs;
the monitor owns the counter. It does not choose PPO plays."""

from __future__ import annotations

from typing import Final

_DEFAULT_NO_PROGRESS_TICKS: Final[int] = 20

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
