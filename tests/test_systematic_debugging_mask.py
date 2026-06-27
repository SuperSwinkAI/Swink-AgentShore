"""Regression test for systematic_debugging re-selection guard (desktop-quv9).

Reproducer: example-project session 2b8729bf.
- play_id 3938: systematic_debugging on issue #272 — completed success=true.
- play_id 3947: systematic_debugging on issue #272 — re-selected 20s later.

The agent's gh CLI label-add ran inside its subprocess; ``agentshore/root-cause-found``
landed on GitHub immediately, but AgentShore's cached ``github_issues`` row didn't
learn about the label until the next ``_refresh_issues`` poll. The selector
fired before that and picked the same issue again.

Fix (mirrors desktop-65bg's recent-completions shadow): orchestrator pushes the
applied label onto ``_recent_applied_labels`` synchronously when the play
succeeds; the next ``_build_state`` overlays it onto the cached issue records,
and ``issue_available_for_debug`` correctly excludes the issue.
"""

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
    # Sanity guard: without this baseline, the exclusion assertion below
    # would pass trivially.
    records = [_record([DEBUG_TRIGGER_LABEL])]
    state = _state_from_records(records)
    candidates = build_candidate_plan(state).candidates_for(PlayType.SYSTEMATIC_DEBUGGING)
    assert any(c.params.issue_number == ISSUE_272 for c in candidates)


def test_shadow_label_excludes_issue_from_systematic_debugging_next_tick() -> None:
    # refresh_issues hasn't run, so root_cause_found isn't on the cached SQLite
    # row yet; the shadow merge overlays it so the issue drops from candidates.
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
    # An applied label on issue 272 must not bleed into the candidate
    # decision for issue 273 — the scoping is strict to the issue number.
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
    # When the label is already on the cached row, the shadow still holds it;
    # the merge must not duplicate it or re-admit the issue.
    records = [_record([DEBUG_TRIGGER_LABEL, ROOT_CAUSE_FOUND_LABEL])]
    shadow = collections.deque([(ISSUE_272, ROOT_CAUSE_FOUND_LABEL)])
    merged_records = _merge_recent_applied_labels(records, shadow)
    state = _state_from_records(merged_records)
    candidates = build_candidate_plan(state).candidates_for(PlayType.SYSTEMATIC_DEBUGGING)
    assert not any(c.params.issue_number == ISSUE_272 for c in candidates)
    # No duplicate label entries on the merged record.
    assert merged_records[0].labels.count(ROOT_CAUSE_FOUND_LABEL) == 1
