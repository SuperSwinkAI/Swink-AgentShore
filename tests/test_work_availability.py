"""Tests for derived work-availability summaries."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agentshore.github.labels import MANUAL_REQUIRED_LABEL
from agentshore.plays.candidates import MAX_OPEN_PRS, build_candidate_plan
from agentshore.state import (
    IssueSnapshot,
    OrchestratorState,
    PlayType,
    PullRequestSnapshot,
    SessionState,
)


def _state(**kwargs: object) -> OrchestratorState:
    base = dict(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        plays_since_last_play_type={
            PlayType.SEED_PROJECT: 0,
            PlayType.DESIGN_AUDIT: 0,
            PlayType.RUN_QA: 0,
        },
        last_play_success_by_type={
            PlayType.SEED_PROJECT: True,
            PlayType.DESIGN_AUDIT: True,
            PlayType.RUN_QA: True,
        },
    )
    base.update(kwargs)
    return OrchestratorState(**base)  # type: ignore[arg-type]


def _issue(number: int, labels: list[str] | None = None) -> IssueSnapshot:
    return IssueSnapshot(
        issue_number=number,
        title=f"Issue {number}",
        state="open",
        priority=None,
        labels=labels or [],
        source=None,
    )


def _pr(number: int, issue_number: int | None = None, **kwargs: object) -> PullRequestSnapshot:
    data = dict(
        pr_number=number,
        title=f"PR {number}",
        state="open",
        branch=f"branch-{number}",
        issue_number=issue_number,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
    )
    data.update(kwargs)
    return PullRequestSnapshot(**data)  # type: ignore[arg-type]


def _seeded_graph(
    *,
    has_ready_tasks: bool = False,
    tasks_ready: int = 0,
    tasks: list[object] | None = None,
) -> MagicMock:
    graph = MagicMock()
    graph.has_epics = True
    graph.has_ready_tasks = has_ready_tasks
    graph.tasks_ready = tasks_ready
    graph.tasks = tasks or []
    return graph


def test_blocked_disallowed_issue_is_open_but_not_workable() -> None:
    summary = build_candidate_plan(
        _state(
            graph=_seeded_graph(),
            open_issues=[_issue(209, ["agentshore/blocked", "agentshore/disallowed"])],
        )
    ).work_availability

    assert summary.github_open_issue_count == 1
    assert summary.blocked_issue_count == 1
    assert summary.disallowed_issue_count == 1
    assert summary.workable_issue_count == 0
    assert summary.terminal_no_work is True


def test_issue_covered_by_open_pr_is_not_workable_issue_work() -> None:
    summary = build_candidate_plan(
        _state(
            graph=_seeded_graph(),
            open_issues=[_issue(10)],
            pull_requests=[_pr(20, issue_number=10)],
        )
    ).work_availability

    assert summary.covered_by_open_pr_count == 1
    assert summary.workable_issue_count == 0
    assert summary.actionable_pr_work_count == 1
    assert summary.terminal_no_work is False


def test_needs_refinement_counts_as_refinement_work() -> None:
    summary = build_candidate_plan(
        _state(graph=_seeded_graph(), open_issues=[_issue(10, ["agentshore/needs-refinement"])])
    ).work_availability

    assert summary.refinement_eligible_count == 1
    assert summary.implementation_eligible_count == 0
    assert summary.workable_issue_count == 1
    assert summary.terminal_no_work is False


def test_in_flight_issue_is_excluded_from_workable_counts() -> None:
    summary = build_candidate_plan(
        _state(graph=_seeded_graph(), open_issues=[_issue(10)], in_flight_issues=[10])
    ).work_availability

    assert summary.in_flight_issue_count == 1
    assert summary.workable_issue_count == 0
    assert summary.terminal_no_work is True


def test_missing_successful_terminal_audits_prevents_terminal_no_work() -> None:
    summary = build_candidate_plan(
        _state(
            graph=_seeded_graph(),
            open_issues=[_issue(10, ["agentshore/blocked", "agentshore/disallowed"])],
            last_play_success_by_type={},
        )
    ).work_availability

    assert summary.workable_issue_count == 0
    assert summary.terminal_no_work is False


def test_successful_seed_without_design_audit_prevents_terminal_no_work() -> None:
    summary = build_candidate_plan(
        _state(
            graph=_seeded_graph(),
            last_play_success_by_type={PlayType.SEED_PROJECT: True},
            plays_since_last_play_type={PlayType.SEED_PROJECT: 0},
        )
    ).work_availability

    assert summary.terminal_no_work is False


def test_beads_without_ready_tasks_blocks_direct_issue_pickup_and_surfaces_groom_work() -> None:
    summary = build_candidate_plan(
        _state(
            graph=_seeded_graph(has_ready_tasks=False, tasks=[]),
            open_issues=[_issue(12, ["agentshore/planned", "agentshore/ai-slop"])],
            pull_requests=[_pr(350, mergeable="MERGEABLE")],
        )
    ).work_availability

    assert summary.github_open_issue_count == 1
    assert summary.beads_blocks_issue_pickup is True
    assert summary.implementation_eligible_count == 0
    assert summary.untracked_gh_issue_count == 1
    assert summary.backlog_sync_work_count == 1
    assert summary.mergeable_pr_count == 0
    assert summary.terminal_no_work is False


def test_ready_beads_tasks_without_actionable_candidate_do_not_block_terminal_no_work() -> None:
    summary = build_candidate_plan(
        _state(
            graph=_seeded_graph(
                has_ready_tasks=True,
                tasks_ready=1,
                tasks=[SimpleNamespace(issue_number=12, ready=True)],
            )
        )
    ).work_availability

    assert summary.ready_task_count == 1
    assert summary.terminal_no_work is True


def test_unreviewed_pr_without_manual_required_is_reviewable() -> None:
    # Baseline for the manual-required test below: an ordinary unreviewed PR
    # (review_decision=None) IS a reviewable, actionable target.
    summary = build_candidate_plan(
        _state(graph=_seeded_graph(), pull_requests=[_pr(20)])
    ).work_availability

    assert summary.reviewable_pr_count == 1
    assert summary.actionable_pr_work_count == 1
    assert summary.manual_required_open_pr_count == 0
    assert summary.terminal_no_work is False


def test_manual_required_pr_is_not_reviewable_so_terminal_no_work() -> None:
    # A manual-required PR is parked for a human: it must not leak into the
    # reviewable set (the bug that pinned END_SESSION masked). With no other
    # work, the session reaches terminal no-work.
    summary = build_candidate_plan(
        _state(graph=_seeded_graph(), pull_requests=[_pr(20, labels=[MANUAL_REQUIRED_LABEL])])
    ).work_availability

    assert summary.reviewable_pr_count == 0
    assert summary.actionable_pr_work_count == 0
    assert summary.manual_required_open_pr_count == 1
    assert summary.terminal_no_work is True


def test_pr_queue_human_blocked_at_cap_minus_one() -> None:
    prs = [_pr(100 + i, labels=[MANUAL_REQUIRED_LABEL]) for i in range(MAX_OPEN_PRS - 1)]
    summary = build_candidate_plan(
        _state(graph=_seeded_graph(), pull_requests=prs)
    ).work_availability

    assert summary.manual_required_open_pr_count == MAX_OPEN_PRS - 1
    assert summary.pr_queue_human_blocked is True


def test_pr_queue_not_human_blocked_below_threshold() -> None:
    prs = [_pr(100 + i, labels=[MANUAL_REQUIRED_LABEL]) for i in range(MAX_OPEN_PRS - 2)]
    summary = build_candidate_plan(
        _state(graph=_seeded_graph(), pull_requests=prs)
    ).work_availability

    assert summary.manual_required_open_pr_count == MAX_OPEN_PRS - 2
    assert summary.pr_queue_human_blocked is False
