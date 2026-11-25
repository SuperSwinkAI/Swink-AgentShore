"""Tests for systematic_debugging re-selection guard."""

from __future__ import annotations

import collections

from agentshore.core.mixins.snapshots import SnapshotProjector
from agentshore.core.mixins.state import _merge_recent_applied_labels
from agentshore.data.models import GitHubIssueRecord
from agentshore.github.labels import DEBUG_TRIGGER_LABEL, ROOT_CAUSE_FOUND_LABEL
from agentshore.plays.candidates import build_candidate_plan
from agentshore.state import (
    BudgetSnapshot,
    OrchestratorState,
    PlayType,
    SessionState,
)

ISSUE_272 = 272


def _record(labels: list[str]) -> GitHubIssueRecord:
    return GitHubIssueRecord(
        issue_number=ISSUE_272,
        session_id="s-test",
        title="parser regression",
        state="open",
        created_at="2026-05-21T00:00:00+00:00",
        labels=labels,
    )


def _state_from_records(records: list[GitHubIssueRecord]) -> OrchestratorState:
    open_issues = SnapshotProjector.project_open_issues(records, None)
    return OrchestratorState(
        session_id="s-test",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        budget=BudgetSnapshot(10.0, 0.0, 10.0, 0.0),
        open_issues=open_issues,
    )


def test_baseline_systematic_debugging_is_a_candidate() -> None:
    records = [_record([DEBUG_TRIGGER_LABEL])]
    state = _state_from_records(records)
    candidates = build_candidate_plan(state).candidates_for(PlayType.SYSTEMATIC_DEBUGGING)
    assert any(c.params.issue_number == ISSUE_272 for c in candidates)


def test_shadow_label_excludes_issue_from_systematic_debugging_next_tick() -> None:
    records = [_record([DEBUG_TRIGGER_LABEL])]
    shadow = collections.deque([(ISSUE_272, ROOT_CAUSE_FOUND_LABEL)])
    merged_records = _merge_recent_applied_labels(records, shadow)
    state = _state_from_records(merged_records)
    candidates = build_candidate_plan(state).candidates_for(PlayType.SYSTEMATIC_DEBUGGING)
    assert not any(c.params.issue_number == ISSUE_272 for c in candidates), (
        "selector picked the same issue at the next tick — shadow did not "
        "prevent the desktop-quv9 re-selection"
    )


def test_shadow_does_not_affect_unrelated_issue() -> None:
    other = GitHubIssueRecord(
        issue_number=273,
        session_id="s-test",
        title="another regression",
        state="open",
        created_at="2026-05-21T00:00:00+00:00",
        labels=[DEBUG_TRIGGER_LABEL],
    )
    records = [_record([DEBUG_TRIGGER_LABEL]), other]
    shadow = collections.deque([(ISSUE_272, ROOT_CAUSE_FOUND_LABEL)])
    merged_records = _merge_recent_applied_labels(records, shadow)
    state = _state_from_records(merged_records)
    candidates = build_candidate_plan(state).candidates_for(PlayType.SYSTEMATIC_DEBUGGING)
    issue_numbers = {c.params.issue_number for c in candidates}
    assert ISSUE_272 not in issue_numbers
    assert 273 in issue_numbers


def test_shadow_idempotent_after_refresh_lands() -> None:
    records = [_record([DEBUG_TRIGGER_LABEL, ROOT_CAUSE_FOUND_LABEL])]
    shadow = collections.deque([(ISSUE_272, ROOT_CAUSE_FOUND_LABEL)])
    merged_records = _merge_recent_applied_labels(records, shadow)
    state = _state_from_records(merged_records)
    candidates = build_candidate_plan(state).candidates_for(PlayType.SYSTEMATIC_DEBUGGING)
    assert not any(c.params.issue_number == ISSUE_272 for c in candidates)
    assert merged_records[0].labels.count(ROOT_CAUSE_FOUND_LABEL) == 1
