"""Shared GitHub label constants."""

from __future__ import annotations

BUG_LABELS: frozenset[str] = frozenset({"bug", "type/bug"})

DEBUG_TRIGGER_LABEL = "agentshore/debug-needed"
DEBUG_TRIGGER_LABELS: frozenset[str] = frozenset({"agentshore/qa", DEBUG_TRIGGER_LABEL})
ROOT_CAUSE_FOUND_LABEL = "agentshore/root-cause-found"

FAILURE_LABELS: frozenset[str] = frozenset(
    {"agentshore/qa", "agentshore/review", "bug", "type/bug"}
)

PLANNED_LABELS: frozenset[str] = frozenset({"agentshore/planned", "agentshore/has-plan"})

DISALLOWED_LABEL = "agentshore/disallowed"
MANUAL_REQUIRED_LABEL = "agentshore/manual-required"

BLOCKED_LABEL = "agentshore/blocked"

BLOCKED_BY_MARKER_TOKEN = "agentshore:blocked-by"


def blocked_by_marker(blocker_issue: int) -> str:
    """Return the comment body groom parses to learn an opaque label's blocker."""
    return f"<!-- {BLOCKED_BY_MARKER_TOKEN} #{blocker_issue} -->"


NEEDS_HUMAN_LABEL = "agentshore/needs-human"

AGENTSHORE_WORKFLOW_LABELS: tuple[tuple[str, str], ...] = (
    (DISALLOWED_LABEL, "b60205"),
    (DEBUG_TRIGGER_LABEL, "d4c5f9"),
    (ROOT_CAUSE_FOUND_LABEL, "5319e7"),
    (MANUAL_REQUIRED_LABEL, "fbca04"),
    (NEEDS_HUMAN_LABEL, "b60205"),
    ("agentshore/approved", "2ea44f"),
    (BLOCKED_LABEL, "d73a4a"),
    ("agentshore/review", "0366d6"),
    ("agentshore/qa", "d876e3"),
    ("agentshore/slop", "c27ba0"),
    ("agentshore/cleanup", "8c959f"),
    ("agentshore/intake", "1d76db"),
    ("agentshore/planned", "7fdbca"),
    ("agentshore/has-plan", "7fdbca"),
    ("agentshore/needs-refinement", "f9d0c4"),
    ("agentshore/refined", "0e8a16"),
    ("agentshore/revert-reopened", "e4e669"),
    ("agentshore/alignment", "5319e7"),
    ("agentshore/epic", "5e81ac"),
    ("agentshore/story", "74a2d2"),
    ("agentshore/task", "94d2bd"),
    ("type/bug", "d73a4a"),
    ("blocked", "d73a4a"),
)

ISSUE_PICKUP_SKIP_LABELS: frozenset[str] = frozenset(
    {BLOCKED_LABEL, DISALLOWED_LABEL, "agentshore/needs-refinement", NEEDS_HUMAN_LABEL}
)

PRIORITY_SCORES: dict[str, int] = {
    "priority/critical": 0,
    "priority/high": 1,
    "priority/medium": 2,
    "priority/low": 3,
}
