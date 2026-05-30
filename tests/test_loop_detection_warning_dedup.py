"""Loop-detector log emission deduplication.

Regression for an observed log storm where `loop_detected` re-fired once per
orchestrator tick (~1/sec) while a streak held at the warn threshold, instead
of firing once on streak entry, once per deepening level, and once on
re-entry after the streak resets.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import agentshore.core
from agentshore.config import RuntimeConfig
from agentshore.state import NullStateProvider, OrchestratorState, PlayType, SessionState


def _make_orch(tmp_path: Path) -> Any:
    from agentshore.core import Orchestrator

    mock_store = AsyncMock()
    mock_selector = MagicMock()

    orch = Orchestrator.__new__(Orchestrator)
    orch._cfg = RuntimeConfig()
    orch._repo_root = tmp_path
    orch._session_id = "test-session"
    orch._store = mock_store
    orch._manager = MagicMock()
    orch._executor = MagicMock()
    orch._selector = mock_selector
    orch._state_provider = NullStateProvider()
    orch._stop_requested = False
    orch._exit_stack = MagicMock()
    orch._health = None
    orch._integrity = None
    orch._power_assertion = None
    orch._in_flight = {}
    orch._dispatch_ctx = {}
    orch._first_play_override = None
    orch._override_queue = asyncio.Queue()
    orch._pause_event = asyncio.Event()
    orch._pause_event.set()
    orch._draining = False
    orch._last_warned_failure_streak = None
    orch._last_warned_any_streak = None
    orch._forced_mask_play_types = ()
    return orch


def _state(
    failure_streak: int = 0,
    any_streak: int = 0,
    session_state: SessionState = SessionState.RUNNING,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="test",
        session_state=session_state,
        total_plays=0,
        total_cost=0.0,
        same_type_failure_streak=failure_streak,
        same_type_streak=any_streak,
        last_play_type=PlayType.ISSUE_PICKUP,
    )


@pytest.fixture
def warning_calls(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch core._logger and yield the warning() mock for inspection."""
    mock_logger = MagicMock()
    monkeypatch.setattr(agentshore.core, "_logger", mock_logger)
    return mock_logger.warning


def _streaks_for_kind(warning_mock: MagicMock, kind: str) -> list[int]:
    streaks: list[int] = []
    for call in warning_mock.call_args_list:
        args, kwargs = call
        if not args or args[0] != "loop_detected":
            continue
        if kwargs.get("kind") != kind:
            continue
        streaks.append(kwargs["streak"])
    return streaks


def test_warn_fires_once_when_streak_holds_at_threshold(
    tmp_path: Path, warning_calls: MagicMock
) -> None:
    """Streak stuck at warn_after across many ticks: emit exactly once."""
    orch = _make_orch(tmp_path)
    warn = orch._cfg.rl.loop_detection.warn_after  # 3
    state = _state(failure_streak=warn)

    for _ in range(20):
        orch._check_loop_detection(state)

    assert _streaks_for_kind(warning_calls, "failure") == [warn]


def test_warn_re_fires_when_streak_deepens(tmp_path: Path, warning_calls: MagicMock) -> None:
    """Each geometric bucket gets its own warning; values between buckets are silent."""
    orch = _make_orch(tmp_path)
    warn = orch._cfg.rl.loop_detection.warn_after  # 3

    # Walk through every streak 3..16, only the bucket-aligned streaks should fire
    # (with warn=3: 3=1x, 6=2x, 15=5x; 4, 5, 7..14, 16 are silent).
    for s in range(warn, warn * 5 + 2):
        orch._check_loop_detection(_state(failure_streak=s))

    assert _streaks_for_kind(warning_calls, "failure") == [warn, 2 * warn, 5 * warn]


def test_warn_re_fires_after_streak_resets(tmp_path: Path, warning_calls: MagicMock) -> None:
    """A new streak after a reset gets a fresh warning."""
    orch = _make_orch(tmp_path)
    warn = orch._cfg.rl.loop_detection.warn_after  # 3

    for _ in range(5):
        orch._check_loop_detection(_state(failure_streak=warn))
    orch._check_loop_detection(_state(failure_streak=0))
    for _ in range(5):
        orch._check_loop_detection(_state(failure_streak=warn))

    assert _streaks_for_kind(warning_calls, "failure") == [warn, warn]


def test_no_warn_below_threshold(tmp_path: Path, warning_calls: MagicMock) -> None:
    """Streaks under warn_after never emit."""
    orch = _make_orch(tmp_path)
    warn = orch._cfg.rl.loop_detection.warn_after  # 3

    for s in range(warn):
        orch._check_loop_detection(_state(failure_streak=s))

    assert _streaks_for_kind(warning_calls, "failure") == []


def test_any_outcome_warn_dedups_independently(tmp_path: Path, warning_calls: MagicMock) -> None:
    """The any_outcome kind has its own memo and dedups separately."""
    orch = _make_orch(tmp_path)
    warn = orch._cfg.rl.loop_detection.warn_after
    any_threshold = 2 * warn  # 6

    for _ in range(10):
        orch._check_loop_detection(_state(any_streak=any_threshold))

    assert _streaks_for_kind(warning_calls, "any_outcome") == [any_threshold]


def test_streak_between_buckets_is_silent(tmp_path: Path, warning_calls: MagicMock) -> None:
    """A monotonically growing streak fires only at bucket multipliers of warn_after."""
    orch = _make_orch(tmp_path)
    # warn_after is 3 by default; multipliers (1,2,5,10,20,50,100).
    # Simulate a runaway streak: 1..100 monotonically.
    for s in range(1, 101):
        orch._check_loop_detection(_state(failure_streak=s))

    # Expect: 3 (1x), 6 (2x), 15 (5x), 30 (10x), 60 (20x). 150 (50x) and 300 (100x)
    # exceed the streak ceiling.
    assert _streaks_for_kind(warning_calls, "failure") == [3, 6, 15, 30, 60]


def test_loop_detection_suppressed_during_drain(
    tmp_path: Path,
    warning_calls: MagicMock,
) -> None:
    orch = _make_orch(tmp_path)
    orch._draining = True
    orch._forced_mask_play_types = (PlayType.ISSUE_PICKUP,)

    should_pause = orch._check_loop_detection(
        _state(any_streak=100, session_state=SessionState.DRAINING)
    )

    assert should_pause is False
    assert _streaks_for_kind(warning_calls, "any_outcome") == []
    assert orch._forced_mask_play_types == ()


def test_failure_yoyo_does_not_re_emit_same_bucket(
    tmp_path: Path, warning_calls: MagicMock
) -> None:
    """Failure streak yo-yo across warn threshold must not re-emit the same bucket.

    Regression: memo was reset whenever streak dropped below warn_after, so
    re-crossing the threshold fired a duplicate warning.
    """
    orch = _make_orch(tmp_path)
    warn = orch._cfg.rl.loop_detection.warn_after  # 3

    # Streak hits warn threshold, dips just below, then crosses again — twice.
    for s in [0, 1, 2, warn, warn - 1, warn, warn - 1, warn]:
        orch._check_loop_detection(_state(failure_streak=s))

    assert _streaks_for_kind(warning_calls, "failure") == [warn]


def test_any_outcome_yoyo_does_not_re_emit_same_bucket(
    tmp_path: Path, warning_calls: MagicMock
) -> None:
    """Any-outcome streak yo-yo across threshold must not re-emit the same bucket.

    Regression: memo was reset when streak dipped below any_warn_threshold,
    causing re-emission on the next crossing.  Sequence from the issue report:
    0,1,2,3,4,5,6,5,6,5,6 — the 6-bucket must fire exactly once.
    """
    orch = _make_orch(tmp_path)
    warn = orch._cfg.rl.loop_detection.warn_after  # 3
    any_threshold = 2 * warn  # 6

    for s in [
        0,
        1,
        2,
        3,
        4,
        5,
        any_threshold,
        any_threshold - 1,
        any_threshold,
        any_threshold - 1,
        any_threshold,
    ]:
        orch._check_loop_detection(_state(any_streak=s))

    assert _streaks_for_kind(warning_calls, "any_outcome") == [any_threshold]


def test_failure_yoyo_resets_on_zero_then_re_emits(
    tmp_path: Path, warning_calls: MagicMock
) -> None:
    """A genuine zero-reset between yo-yo runs allows a fresh warning on re-entry."""
    orch = _make_orch(tmp_path)
    warn = orch._cfg.rl.loop_detection.warn_after  # 3

    # First streak run: yo-yo, bucket fires once.
    for s in [warn, warn - 1, warn]:
        orch._check_loop_detection(_state(failure_streak=s))
    # Genuine reset to zero.
    orch._check_loop_detection(_state(failure_streak=0))
    # Second streak run: yo-yo again, bucket should fire once more.
    for s in [warn, warn - 1, warn]:
        orch._check_loop_detection(_state(failure_streak=s))

    assert _streaks_for_kind(warning_calls, "failure") == [warn, warn]
