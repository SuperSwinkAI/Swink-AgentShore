"""Regression tests for the in-memory label shadow (desktop-quv9).

Sibling of ``_merge_recent_completions`` (desktop-65bg). When a successful
``systematic_debugging`` play adds ``agentshore/root-cause-found`` to issue N via
gh CLI inside the agent subprocess, the label exists on GitHub but AgentShore's
cached ``github_issues`` row doesn't learn about it until the next
``_refresh_issues`` poll. Without the shadow, the very next selector tick can
re-pick (N, systematic_debugging) against the stale snapshot — observed in
example-project session 2b8729bf where play_id 3938 (success) was followed
20s later by play_id 3947 on the SAME issue.
"""

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
    # Simulates the desktop-quv9 race: systematic_debugging succeeded on
    # issue 272 and applied root_cause_found via gh, but the cached row
    # hasn't refreshed yet. The shadow makes the label visible immediately.
    records = [_issue(272, ["agentshore/qa"]), _issue(99, ["bug"])]
    shadow = collections.deque([(272, ROOT_CAUSE_FOUND_LABEL)])
    out = _merge_recent_applied_labels(records, shadow)
    by_num = {r.issue_number: r for r in out}
    assert ROOT_CAUSE_FOUND_LABEL in by_num[272].labels
    assert "agentshore/qa" in by_num[272].labels
    # Untouched record passes through by reference (allocation-free hot path).
    assert by_num[99] is records[1]


def test_does_not_duplicate_a_label_already_on_the_record() -> None:
    # Once _refresh_issues catches up the label is on the DB row; the shadow
    # still holds the entry but the merge must not duplicate it.
    records = [_issue(272, ["agentshore/qa", ROOT_CAUSE_FOUND_LABEL])]
    shadow = collections.deque([(272, ROOT_CAUSE_FOUND_LABEL)])
    out = _merge_recent_applied_labels(records, shadow)
    assert out is records  # no mutation, no new list
    assert out[0].labels.count(ROOT_CAUSE_FOUND_LABEL) == 1


def test_ignores_shadow_entries_for_missing_issues() -> None:
    # Shadow can outlive the issue (closed, archived, GC'd). The merge must
    # not crash and must not invent records.
    records = [_issue(1, ["bug"])]
    shadow = collections.deque([(999, ROOT_CAUSE_FOUND_LABEL)])
    out = _merge_recent_applied_labels(records, shadow)
    assert out is records
    assert [r.issue_number for r in out] == [1]


def test_does_not_mutate_original_record_labels() -> None:
    # IssueSnapshot consumers later in the pipeline may mutate the labels
    # list; the shadow merge produces a fresh list per affected record so
    # the deque-resident original stays clean.
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
