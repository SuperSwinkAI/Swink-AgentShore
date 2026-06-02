"""Unit tests for the per-play-type non-productive streak feeding the breaker.

``_compute_play_recency`` builds ``consecutive_nonproductive_by_type`` — the tail
run of consecutive ``not success`` outcomes (fail OR skip, since skips record
``success=False``) per play type, ignoring interleaved other-type plays. This is
the signal the 3-strikes circuit breaker masks on.
"""

from __future__ import annotations

from agentshore.core.mixins.snapshots import SnapshotProjector
from agentshore.data.models import PlayRecord
from agentshore.state import PlayType


def _rec(play_type: str, success: bool, failure_category: str | None = None) -> PlayRecord:
    return PlayRecord(
        session_id="s",
        play_type=play_type,
        started_at="2026-05-31T00:00:00+00:00",
        ended_at="2026-05-31T00:00:01+00:00",
        success=success,
        failure_category=failure_category,
    )


def _consecutive(history: list[PlayRecord]) -> dict[PlayType, int]:
    return SnapshotProjector.compute_play_recency(history)[6]


def test_skips_and_fails_accumulate_across_interleaving() -> None:
    # write_impl skips interleaved with a successful refine — the per-type streak
    # ignores the interleaving (the exact 2529961d spin shape).
    history = [
        _rec("issue_pickup", True),
        _rec("write_implementation_plan", False, "skip:masked"),
        _rec("refine_task_breakdown", True),
        _rec("write_implementation_plan", False, "skip:masked"),
        _rec("write_implementation_plan", False, "skip:masked"),
    ]
    consecutive = _consecutive(history)
    assert consecutive[PlayType.WRITE_IMPLEMENTATION_PLAN] == 3
    # refine's most-recent outcome is a success → no strikes.
    assert consecutive.get(PlayType.REFINE_TASK_BREAKDOWN, 0) == 0


def test_streak_resets_on_success() -> None:
    history = [
        _rec("issue_pickup", False),
        _rec("issue_pickup", False),
        _rec("issue_pickup", True),  # newest success terminates the tail run
    ]
    assert _consecutive(history).get(PlayType.ISSUE_PICKUP, 0) == 0


def test_real_failures_count_too() -> None:
    history = [
        _rec("code_review", False, "agent_error"),
        _rec("code_review", False, "agent_error"),
    ]
    assert _consecutive(history)[PlayType.CODE_REVIEW] == 2
