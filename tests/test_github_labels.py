"""Tests for the consolidated GitHub label constants.

These tests pin the canonical sets that ``github/adapter.py`` and
``plays/resolver.py`` previously maintained as separate copies. If a future
refactor deletes a member, both files break in lockstep — which is exactly
what the consolidation was meant to enforce.
"""

from __future__ import annotations

from agentshore.github.labels import (
    AGENTSHORE_WORKFLOW_LABELS,
    BLOCKED_BY_MARKER_TOKEN,
    BLOCKED_LABEL,
    BUG_LABELS,
    DEBUG_TRIGGER_LABELS,
    FAILURE_LABELS,
    MANUAL_REQUIRED_LABEL,
    PLANNED_LABELS,
    PRIORITY_SCORES,
    ROOT_CAUSE_FOUND_LABEL,
    blocked_by_marker,
)


def test_bug_labels_match_pre_consolidation_set() -> None:
    assert frozenset({"bug", "type/bug"}) == BUG_LABELS


def test_failure_labels_match_pre_consolidation_set() -> None:
    # Review/bug labels still influence backlog priority, but systematic
    # debugging now uses DEBUG_TRIGGER_LABELS instead.
    assert frozenset({"agentshore/qa", "agentshore/review", "bug", "type/bug"}) == FAILURE_LABELS


def test_debug_trigger_labels_are_explicit() -> None:
    assert frozenset({"agentshore/qa", "agentshore/debug-needed"}) == DEBUG_TRIGGER_LABELS
    assert "agentshore/review" not in DEBUG_TRIGGER_LABELS
    assert "bug" not in DEBUG_TRIGGER_LABELS
    assert "type/bug" not in DEBUG_TRIGGER_LABELS


def test_workflow_labels_include_debug_and_manual_gates() -> None:
    label_names = {name for name, _color in AGENTSHORE_WORKFLOW_LABELS}
    assert "agentshore/debug-needed" in label_names
    assert ROOT_CAUSE_FOUND_LABEL in label_names
    assert MANUAL_REQUIRED_LABEL in label_names


def test_workflow_labels_include_beads_and_skill_taxonomy() -> None:
    label_names = {name for name, _color in AGENTSHORE_WORKFLOW_LABELS}
    for label in (
        "agentshore/alignment",
        "agentshore/epic",
        "agentshore/story",
        "agentshore/task",
        "agentshore/intake",
        "agentshore/qa",
        "agentshore/slop",
        "agentshore/review",
        "agentshore/planned",
        "agentshore/needs-refinement",
    ):
        assert label in label_names


def test_planned_labels_match_pre_consolidation_set() -> None:
    assert frozenset({"agentshore/planned", "agentshore/has-plan"}) == PLANNED_LABELS


def test_priority_scores_lower_is_more_urgent() -> None:
    # Both adapter and resolver rely on critical < high < medium < low.
    assert PRIORITY_SCORES == {
        "priority/critical": 0,
        "priority/high": 1,
        "priority/medium": 2,
        "priority/low": 3,
    }
    ordered = sorted(PRIORITY_SCORES, key=PRIORITY_SCORES.__getitem__)
    assert ordered == ["priority/critical", "priority/high", "priority/medium", "priority/low"]


def test_label_collections_have_expected_types() -> None:
    # frozenset prevents accidental in-place mutation by callers — important
    # because resolver.py and adapter.py both import these at module load.
    assert isinstance(BUG_LABELS, frozenset)
    assert isinstance(DEBUG_TRIGGER_LABELS, frozenset)
    assert isinstance(FAILURE_LABELS, frozenset)
    assert isinstance(PLANNED_LABELS, frozenset)
    assert isinstance(PRIORITY_SCORES, dict)


def test_bug_labels_are_subset_of_failure_labels() -> None:
    """Bug issues should re-trigger the failure-handling branch in resolver.py."""
    assert BUG_LABELS <= FAILURE_LABELS


def test_blocked_by_marker_round_trips_blocker_number() -> None:
    # The marker is an HTML comment carrying the token + blocker #N. groom parses
    # the #N back out of this exact string, so its shape is a contract (#241).
    marker = blocked_by_marker(12)
    assert marker == "<!-- agentshore:blocked-by #12 -->"
    assert BLOCKED_BY_MARKER_TOKEN in marker
    assert marker.startswith("<!--") and marker.endswith("-->")


def test_blocked_label_is_agentshore_namespaced() -> None:
    # groom's stale-gate sweep is scoped to the agentshore/ namespace; the plain
    # `blocked` label (possibly human-set) is never swept.
    assert BLOCKED_LABEL == "agentshore/blocked"
    assert BLOCKED_LABEL.startswith("agentshore/")
