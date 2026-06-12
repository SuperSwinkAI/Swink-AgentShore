"""Tests for agentshore.pr_state.blocked_reasons()."""

from __future__ import annotations

from agentshore.pr_state import blocked_reasons


def _call(**kwargs: object) -> list[str]:
    defaults = {
        "state": "open",
        "labels": [],
        "review_decision": None,
        "status_check_summary": None,
        "is_draft": False,
    }
    defaults.update(kwargs)
    return blocked_reasons(**defaults)  # type: ignore[arg-type]


def test_clean_pr_has_no_blocked_reasons() -> None:
    assert _call() == []


def test_draft_returns_draft_only() -> None:
    result = _call(is_draft=True, state="blocked", review_decision="CHANGES_REQUESTED")
    assert result == ["draft"]


def test_blocked_state_included() -> None:
    for state in ("blocked", "changes_requested", "ci_failed"):
        result = _call(state=state)
        assert state in result


def test_changes_requested_review_decision() -> None:
    result = _call(review_decision="CHANGES_REQUESTED")
    assert "changes_requested" in result


def test_changes_requested_not_dismissed_by_agentshore_pass() -> None:
    """#344: an AgentShore PASS never overrides a live CHANGES_REQUESTED.

    A PASS logged at the same head as a fresh human CHANGES_REQUESTED used to
    suppress the reason (the two are indistinguishable in order at the same
    SHA), letting merge_pr fixate on a PR a human had explicitly blocked. The
    decision now always blocks; it clears only when GitHub's reviewDecision
    itself changes.
    """
    result = _call(review_decision="CHANGES_REQUESTED")
    assert "changes_requested" in result


def test_changes_requested_accumulates_with_other_blockers() -> None:
    """CHANGES_REQUESTED is reported alongside ci_failed and merge_conflicts."""
    result = _call(
        review_decision="CHANGES_REQUESTED",
        status_check_summary="failed",
        mergeable="CONFLICTING",
    )
    assert "changes_requested" in result
    assert "ci_failed" in result
    assert "merge_conflicts" in result


def test_blocked_label() -> None:
    for label in ("blocked", "agentshore/blocked", "do-not-merge"):
        result = _call(labels=[label])
        assert "blocked_label" in result, f"label {label!r} should cause blocked_label"


def test_ci_failed_status_summary() -> None:
    result = _call(status_check_summary="failed")
    assert "ci_failed" in result


def test_multiple_reasons_accumulated() -> None:
    result = _call(
        state="blocked",
        review_decision="CHANGES_REQUESTED",
        labels=["do-not-merge"],
        status_check_summary="failed",
    )
    assert "blocked" in result
    assert "changes_requested" in result
    assert "blocked_label" in result
    assert "ci_failed" in result


def test_no_duplicate_reasons() -> None:
    result = _call(state="changes_requested", review_decision="CHANGES_REQUESTED")
    assert result.count("changes_requested") == 1


def test_open_state_not_blocked() -> None:
    result = _call(state="open")
    assert "open" not in result
    assert result == []


def test_conflicting_mergeable_adds_merge_conflicts() -> None:
    result = _call(mergeable="CONFLICTING")
    assert "merge_conflicts" in result


def test_non_conflicting_mergeable_not_blocked() -> None:
    for value in ("MERGEABLE", "UNKNOWN", None):
        result = _call(mergeable=value)
        assert "merge_conflicts" not in result, f"mergeable={value!r} should not block"
