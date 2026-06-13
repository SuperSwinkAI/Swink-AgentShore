"""Shared GitHub label constants.

Centralised so adapter and resolver both reference the same canonical sets;
renaming a label (e.g. ``bug`` -> ``type/bug``) only requires editing this
module instead of multiple unrelated files.
"""

from __future__ import annotations

# Issues classified as bugs sort ahead of all other open issues regardless of
# priority label.
BUG_LABELS: frozenset[str] = frozenset({"bug", "type/bug"})

# Issues that represent explicit diagnostic failures. These are the only
# issue labels that make systematic_debugging eligible by default.
DEBUG_TRIGGER_LABEL = "agentshore/debug-needed"
DEBUG_TRIGGER_LABELS: frozenset[str] = frozenset({"agentshore/qa", DEBUG_TRIGGER_LABEL})
ROOT_CAUSE_FOUND_LABEL = "agentshore/root-cause-found"

# Issues that returned to the backlog from failed or follow-up workflows. These
# typically need implementation pickup, not necessarily root-cause debugging.
FAILURE_LABELS: frozenset[str] = frozenset(
    {"agentshore/qa", "agentshore/review", "bug", "type/bug"}
)

# Issues with an existing decomposition / plan attached. The resolver skips
# planning for these and goes straight to issue pickup.
PLANNED_LABELS: frozenset[str] = frozenset({"agentshore/planned", "agentshore/has-plan"})

# Issues that AgentShore should not attempt because the requested work is outside
# autonomous agent policy, for example CI workflow ownership.
DISALLOWED_LABEL = "agentshore/disallowed"
MANUAL_REQUIRED_LABEL = "agentshore/manual-required"

# Applied to an issue the planner could not turn into an implementation plan
# (too ambiguous / too large / needs a human to decompose). Stops
# write_implementation_plan — and every other implementation-style play — from
# re-selecting the same un-plannable issue every tick, which otherwise spams
# comments and burns agent budget with no progress (#458). Cleared by a human
# (or a grooming pass that splits the issue) removing the label.
NEEDS_HUMAN_LABEL = "agentshore/needs-human"

# Labels that AgentShore's own skills may add/remove during PR and issue
# workflows. Bootstrap ensures these exist before agents attempt gh label ops.
AGENTSHORE_WORKFLOW_LABELS: tuple[tuple[str, str], ...] = (
    (DISALLOWED_LABEL, "b60205"),
    (DEBUG_TRIGGER_LABEL, "d4c5f9"),
    (ROOT_CAUSE_FOUND_LABEL, "5319e7"),
    (MANUAL_REQUIRED_LABEL, "fbca04"),
    (NEEDS_HUMAN_LABEL, "b60205"),
    ("agentshore/approved", "2ea44f"),
    ("agentshore/blocked", "d73a4a"),
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

# Issue-level labels that make the issue ineligible for implementation-style
# plays. ``agentshore/blocked`` is a broad legacy/manual gate; ``agentshore/disallowed``
# is the terminal policy-out-of-scope gate.
ISSUE_PICKUP_SKIP_LABELS: frozenset[str] = frozenset(
    {"agentshore/blocked", DISALLOWED_LABEL, "agentshore/needs-refinement", NEEDS_HUMAN_LABEL}
)

# Priority labels map to numeric ranks where lower = more urgent. The store's
# get_open_issues query orders ASC NULLS LAST, so critical (0) lands first and
# unlabeled issues land last.
PRIORITY_SCORES: dict[str, int] = {
    "priority/critical": 0,
    "priority/high": 1,
    "priority/medium": 2,
    "priority/low": 3,
}
