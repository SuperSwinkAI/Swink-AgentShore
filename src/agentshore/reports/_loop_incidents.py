"""Loop-incident detection state machine.

Extracted from ``reports/collector.py`` (TNQA 10 H1) — the densest logic
unit in the collector; separated so it can be read, tested, and evolved
independently of the surrounding aggregation helpers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.reports.types import LoopIncidentEntry

if TYPE_CHECKING:
    from agentshore.data.store import PlayRecord


def compute_loop_incidents(plays: list[PlayRecord]) -> list[LoopIncidentEntry]:
    """Walk plays in order and emit one entry per same-type failure streak >= 3.

    Resolution classification:
      * ``succeeded_after_streak_N`` — the streak ended because the next play of the
        same type succeeded; ``N`` is the peak failure streak.
      * ``force_masked`` — the streak ended because the next play was a different
        play type *and* peak streak was >= 5 (i.e. a force-switch tier streak).
      * ``play_switched_after_streak_N`` — peak streak in 3-4 ended by a different
        play type (the warning tier — informational, not force-masked).
      * ``human_override`` — final failing play has ``failure_category ==
        "human_override"``.
      * ``human_escalation`` — final failing play has ``failure_category ==
        "human_escalation"``, OR the streak was still active at the end of the
        session with peak streak >= 7.
      * ``session_ended_in_streak`` — the streak was still active at the last play
        and peak streak < 7.
    """

    def _tier(peak: int) -> str:
        if peak >= 7:
            return "escalation"
        if peak >= 5:
            return "force_switch"
        return "warning"

    incidents: list[LoopIncidentEntry] = []
    streak_type: str | None = None
    streak_count = 0
    streak_start_index = 0
    streak_start_play_id: int | None = None
    streak_start_started_at = ""
    last_failure: PlayRecord | None = None

    def _emit(resolution: str) -> None:
        assert streak_type is not None
        assert last_failure is not None
        incidents.append(
            LoopIncidentEntry(
                play_type=streak_type,
                peak_streak=streak_count,
                tier=_tier(streak_count),
                start_play_id=streak_start_play_id,
                end_play_id=last_failure.play_id,
                start_play_index=streak_start_index,
                end_play_index=streak_start_index + streak_count - 1,
                started_at=streak_start_started_at,
                ended_at=last_failure.started_at,
                resolution=resolution,
            )
        )

    def _classify_terminated_by_other(peak: int) -> str:
        if last_failure is not None:
            fc = last_failure.failure_category
            if fc == "human_override":
                return "human_override"
            if fc == "human_escalation":
                return "human_escalation"
        if peak >= 5:
            return "force_masked"
        return f"play_switched_after_streak_{peak}"

    def _classify_same_type_success(peak: int) -> str:
        if last_failure is not None:
            fc = last_failure.failure_category
            if fc == "human_override":
                return "human_override"
            if fc == "human_escalation":
                return "human_escalation"
        return f"succeeded_after_streak_{peak}"

    for idx, p in enumerate(plays):
        if not p.success:
            if p.play_type == streak_type:
                streak_count += 1
            else:
                # Finalize any prior active streak ended by a different-type play.
                if streak_type is not None and streak_count >= 3:
                    _emit(_classify_terminated_by_other(streak_count))
                streak_type = p.play_type
                streak_count = 1
                streak_start_index = idx
                streak_start_play_id = p.play_id
                streak_start_started_at = p.started_at
            last_failure = p
        else:
            # Success: terminates any active streak.
            if streak_type is not None and streak_count >= 3:
                if p.play_type == streak_type:
                    _emit(_classify_same_type_success(streak_count))
                else:
                    _emit(_classify_terminated_by_other(streak_count))
            streak_type = None
            streak_count = 0
            streak_start_index = 0
            streak_start_play_id = None
            streak_start_started_at = ""
            last_failure = None

    # Finalize a trailing active streak.
    if streak_type is not None and streak_count >= 3 and last_failure is not None:
        fc = last_failure.failure_category
        if fc == "human_override":
            resolution = "human_override"
        elif fc == "human_escalation" or streak_count >= 7:
            resolution = "human_escalation"
        else:
            resolution = "session_ended_in_streak"
        incidents.append(
            LoopIncidentEntry(
                play_type=streak_type,
                peak_streak=streak_count,
                tier=_tier(streak_count),
                start_play_id=streak_start_play_id,
                end_play_id=last_failure.play_id,
                start_play_index=streak_start_index,
                end_play_index=len(plays) - 1,
                started_at=streak_start_started_at,
                ended_at=last_failure.started_at,
                resolution=resolution,
            )
        )

    return incidents
