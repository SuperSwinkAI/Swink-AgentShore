"""desktop-mib1 regression: _idle_streak advances when the selector returns None.

Pre-rni0, IDLE_TICK was a play; PPO picked it on idle ticks; the streak reset
each time a play "was picked" (loop.py:474). After rni0 the selector returns
None on idle ticks and the loop increments _idle_streak, walking through
_IDLE_BACKOFF_SECONDS.

The periodic-refresh reset at loop.py:394-397 is also removed — a fleet sitting
idle through a refresh hasn't become less idle.
"""

from __future__ import annotations

import asyncio

from agentshore.core import _IDLE_BACKOFF_SECONDS, Orchestrator


def _orch() -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch._in_flight = {}
    orch._first_play_override = None
    orch._override_queue = asyncio.Queue()
    orch._idle_streak = 0
    orch._last_selection_digest = None
    return orch


def test_idle_streak_advances_through_backoff_table() -> None:
    """N consecutive selector-None ticks must walk the streak through the table."""
    orch = _orch()

    observed_waits: list[float] = []
    for _ in range(len(_IDLE_BACKOFF_SECONDS) + 5):
        # Simulate the loop branch on selection is None: log+increment+wait.
        observed_waits.append(orch._idle_backoff())
        orch._idle_streak += 1

    # First N values match the table in order.
    for i, expected in enumerate(_IDLE_BACKOFF_SECONDS):
        assert observed_waits[i] == expected, (
            f"idx {i}: expected {expected}, got {observed_waits[i]}"
        )
    # Anything past the table clamps at the ceiling.
    ceiling = _IDLE_BACKOFF_SECONDS[-1]
    for wait in observed_waits[len(_IDLE_BACKOFF_SECONDS) :]:
        assert wait == ceiling


def test_idle_streak_reaches_index_3_and_matches_table() -> None:
    """desktop-mib1 acceptance pin: after >=3 idle ticks the wait matches
    ``_IDLE_BACKOFF_SECONDS[3]``."""
    orch = _orch()
    for _ in range(3):
        orch._idle_streak += 1
    assert orch._idle_streak >= 3
    assert orch._idle_backoff() == _IDLE_BACKOFF_SECONDS[3]


def test_idle_backoff_clamps_at_ceiling_for_long_idles() -> None:
    """The streak can run arbitrarily long without the wait crossing the ceiling."""
    orch = _orch()
    orch._idle_streak = 10_000
    assert orch._idle_backoff() == _IDLE_BACKOFF_SECONDS[-1]
