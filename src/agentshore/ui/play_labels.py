"""Canonical PlayType → human label formatters shared across UI surfaces."""

from __future__ import annotations

from agentshore.state import PlayType

_SHORT_LABELS: dict[PlayType, str] = {
    PlayType.INSTANTIATE_AGENT: "Instantiate",
    PlayType.UNBLOCK_PR: "Unblock",
    PlayType.WRITE_IMPLEMENTATION_PLAN: "Plan",
    PlayType.END_AGENT: "EndAgent",
    PlayType.ISSUE_PICKUP: "Pickup",
    PlayType.CODE_REVIEW: "Review",
    PlayType.MERGE_PR: "Merge",
    PlayType.RUN_QA: "QA",
    PlayType.SYSTEMATIC_DEBUGGING: "Debug",
    PlayType.DESIGN_AUDIT: "Audit",
    PlayType.END_SESSION: "EndSession",
    PlayType.RECONCILE_STATE: "Reconcile",
    PlayType.REFINE_TASK_BREAKDOWN: "Refine",
    PlayType.CLEANUP: "Cleanup",
    PlayType.FUTURE_4: "Reserved",
    PlayType.TAKE_BREAK: "Break",
    PlayType.GROOM_BACKLOG: "Groom",
    PlayType.SEED_PROJECT: "Seed",
    PlayType.CALIBRATE_ALIGNMENT: "Calibrate",
    PlayType.PRUNE: "Prune",
    PlayType.FUTURE_7: "Reserved",
    PlayType.FUTURE_8: "Reserved",
}


def play_label(play_type: PlayType) -> str:
    """Full title-cased label, e.g. ``ISSUE_PICKUP`` → ``Issue Pickup``."""
    return play_type.value.replace("_", " ").title()


def play_short_label(play_type: PlayType) -> str:
    """Compact abbreviation, e.g. ``ISSUE_PICKUP`` → ``Pickup``."""
    return _SHORT_LABELS.get(play_type, play_type.value.title())
