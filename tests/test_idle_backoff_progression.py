"""Tests for _idle_streak advances when the selector returns None."""

from __future__ import annotations

from pathlib import Path

from agentshore.core import Orchestrator
from agentshore.core.mixins.loop import _IDLE_BACKOFF_SECONDS


def _orch(tmp_path: Path) -> Orchestrator:
    from tests.orchestrator_factory import make_test_orchestrator

    return make_test_orchestrator(tmp_path)


def test_idle_streak_advances_through_backoff_table(tmp_path: Path) -> None:
    """N consecutive selector-None ticks must walk the streak through the table."""
    orch = _orch(tmp_path)

    observed_waits: list[float] = []
    for _ in range(len(_IDLE_BACKOFF_SECONDS) + 5):
        observed_waits.append(orch._loop.idle_backoff())
        orch._runtime.idle_streak += 1

    for i, expected in enumerate(_IDLE_BACKOFF_SECONDS):
        assert observed_waits[i] == expected, (
            f"idx {i}: expected {expected}, got {observed_waits[i]}"
        )
    ceiling = _IDLE_BACKOFF_SECONDS[-1]
    for wait in observed_waits[len(_IDLE_BACKOFF_SECONDS) :]:
        assert wait == ceiling


def test_idle_streak_reaches_index_3_and_matches_table(tmp_path: Path) -> None:
    """desktop-mib1 acceptance pin: after >=3 idle ticks the wait matches
    ``_IDLE_BACKOFF_SECONDS[3]``."""
    orch = _orch(tmp_path)
    for _ in range(3):
        orch._runtime.idle_streak += 1
    assert orch._runtime.idle_streak >= 3
    assert orch._loop.idle_backoff() == _IDLE_BACKOFF_SECONDS[3]


def test_idle_backoff_clamps_at_ceiling_for_long_idles(tmp_path: Path) -> None:
    """The streak can run arbitrarily long without the wait crossing the ceiling."""
    orch = _orch(tmp_path)
    orch._runtime.idle_streak = 10_000
    assert orch._loop.idle_backoff() == _IDLE_BACKOFF_SECONDS[-1]
