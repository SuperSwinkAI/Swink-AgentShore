"""Tests for the in-memory label shadow."""

from __future__ import annotations

import collections

from agentshore.core.mixins.state import _merge_recent_applied_labels
from agentshore.data.models import GitHubIssueRecord
from agentshore.github.labels import ROOT_CAUSE_FOUND_LABEL


def _issue(
    number: int,
    labels: list[str] | None = None,
    state: str = "open",
) -> GitHubIssueRecord:
    return GitHubIssueRecord(
        issue_number=number,
        session_id="s1",
        title=f"Issue {number}",
        state=state,
        created_at="2026-05-21T00:00:00+00:00",
        labels=list(labels or []),
    )


def test_returns_records_unchanged_when_shadow_is_empty() -> None:
    records = [_issue(1, ["bug"]), _issue(2, ["agentshore/qa"])]
    out = _merge_recent_applied_labels(records, [])
    assert out is records


def test_overlays_shadow_label_onto_matching_issue() -> None:
    records = [_issue(272, ["agentshore/qa"]), _issue(99, ["bug"])]
    shadow = collections.deque([(272, ROOT_CAUSE_FOUND_LABEL)])
    out = _merge_recent_applied_labels(records, shadow)
    by_num = {r.issue_number: r for r in out}
    assert ROOT_CAUSE_FOUND_LABEL in by_num[272].labels
    assert "agentshore/qa" in by_num[272].labels
    assert by_num[99] is records[1]


def test_does_not_duplicate_a_label_already_on_the_record() -> None:
    records = [_issue(272, ["agentshore/qa", ROOT_CAUSE_FOUND_LABEL])]
    shadow = collections.deque([(272, ROOT_CAUSE_FOUND_LABEL)])
    out = _merge_recent_applied_labels(records, shadow)
    assert out is records
    assert out[0].labels.count(ROOT_CAUSE_FOUND_LABEL) == 1


def test_ignores_shadow_entries_for_missing_issues() -> None:
    records = [_issue(1, ["bug"])]
    shadow = collections.deque([(999, ROOT_CAUSE_FOUND_LABEL)])
    out = _merge_recent_applied_labels(records, shadow)
    assert out is records
    assert [r.issue_number for r in out] == [1]


def test_does_not_mutate_original_record_labels() -> None:
    records = [_issue(272, ["agentshore/qa"])]
    original_labels = records[0].labels
    shadow = collections.deque([(272, ROOT_CAUSE_FOUND_LABEL)])
    out = _merge_recent_applied_labels(records, shadow)
    assert out[0].labels is not original_labels
    assert ROOT_CAUSE_FOUND_LABEL not in original_labels
    assert ROOT_CAUSE_FOUND_LABEL in out[0].labels


def test_skips_empty_label_entries() -> None:
    records = [_issue(1, ["bug"])]
    shadow = collections.deque([(1, "")])
    out = _merge_recent_applied_labels(records, shadow)
    assert out is records


def test_multiple_shadow_labels_for_same_issue_all_overlay() -> None:
    records = [_issue(1, ["bug"])]
    shadow = collections.deque([(1, ROOT_CAUSE_FOUND_LABEL), (1, "agentshore/needs-followup")])
    out = _merge_recent_applied_labels(records, shadow)
    assert ROOT_CAUSE_FOUND_LABEL in out[0].labels
    assert "agentshore/needs-followup" in out[0].labels
    assert "bug" in out[0].labels
