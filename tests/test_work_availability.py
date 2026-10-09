"""Tests for derived work-availability summaries."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agentshore.errors import ErrorClass
from agentshore.github.labels import MANUAL_REQUIRED_LABEL
from agentshore.plays.candidates import MAX_OPEN_PRS, build_candidate_plan
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
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
    # Below the cap AND not every open PR is manual-required (one plain PR keeps
    # the queue drainable), so neither hatch path fires — a pure cap-boundary check.
    prs = [_pr(100 + i, labels=[MANUAL_REQUIRED_LABEL]) for i in range(MAX_OPEN_PRS - 2)]
    prs.append(_pr(900))
    summary = build_candidate_plan(
        _state(graph=_seeded_graph(), pull_requests=prs)
    ).work_availability

    assert summary.manual_required_open_pr_count == MAX_OPEN_PRS - 2
    assert summary.pr_queue_human_blocked is False


def _agent(
    agent_id: str,
    identity: str,
    *,
    tier: str = "large",
    status: AgentStatus = AgentStatus.IDLE,
    error_class: ErrorClass | None = None,
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CLAUDE_CODE,
        status=status,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=1,
        tasks_failed=0,
        model_tier=tier,
        github_identity=identity,
        last_error_class=error_class,
    )


def test_review_only_pr_not_actionable_without_cross_identity_reviewer() -> None:
    # The wedge: every PR authored by "alice", the only large agent is also
    # "alice" (a "bob" medium can't review). Review is infeasible, so the PRs
    # must not hold END_SESSION shut via actionable_pr_work.
    summary = build_candidate_plan(
        _state(
            graph=_seeded_graph(),
            agents=[_agent("a1", "alice"), _agent("b1", "bob", tier="medium")],
            pull_requests=[_pr(20, github_author="alice"), _pr(21, github_author="alice")],
        )
    ).work_availability

    assert summary.reviewable_pr_count == 2
    assert summary.actionable_pr_work_count == 0
    assert summary.has_actionable_work is False


def test_review_only_pr_actionable_with_busy_or_recovering_cross_identity_reviewer() -> None:
    for reviewer in (
        _agent("b1", "bob", status=AgentStatus.BUSY),
        _agent("b1", "bob", status=AgentStatus.ERROR, error_class=ErrorClass.RATE_LIMIT),
    ):
        summary = build_candidate_plan(
            _state(
                graph=_seeded_graph(),
                agents=[_agent("a1", "alice"), reviewer],
                pull_requests=[_pr(20, github_author="alice")],
            )
        ).work_availability
        assert summary.actionable_pr_work_count == 1


def test_review_infeasible_when_cross_identity_reviewer_out_of_service() -> None:
    for state_kwargs in (
        {"agents": [_agent("a1", "alice"), _agent("b1", "bob", status=AgentStatus.TERMINATED)]},
        {
            "agents": [_agent("a1", "alice"), _agent("b1", "bob")],
            "recovery_exhausted_agent_ids": frozenset({"b1"}),
        },
    ):
        summary = build_candidate_plan(
            _state(
                graph=_seeded_graph(),
                pull_requests=[_pr(20, github_author="alice")],
                **state_kwargs,
            )
        ).work_availability
        assert summary.actionable_pr_work_count == 0


def test_review_infeasible_pr_still_actionable_when_mergeable() -> None:
    # Merge-ready work isn't review work: it still counts with no cross-identity reviewer.
    summary = build_candidate_plan(
        _state(
            graph=_seeded_graph(),
            agents=[_agent("a1", "alice")],
            pull_requests=[
                _pr(20, github_author="alice"),
                _pr(
                    30,
                    github_author="alice",
                    review_decision="APPROVED",
                    mergeable="MERGEABLE",
                    status_check_summary="SUCCESS",
                    base_ref="main",
                ),
            ],
            target_branch="main",
        )
    ).work_availability

    assert summary.mergeable_pr_count == 1
    assert summary.actionable_pr_work_count == 1


def test_pr_queue_human_blocked_when_all_open_prs_manual_required_and_no_work() -> None:
    # End-session-wedge fix: every open PR manual-required AND no other actionable
    # work → the queue cannot drain without a human even well below the cap, so
    # pr_queue_human_blocked is True (this is what lets END_SESSION unmask and the
    # session end cleanly instead of parking).
    prs = [_pr(100 + i, labels=[MANUAL_REQUIRED_LABEL]) for i in range(4)]
    summary = build_candidate_plan(
        _state(graph=_seeded_graph(), pull_requests=prs)
    ).work_availability

    assert summary.manual_required_open_pr_count == 4
    assert summary.pr_queue_human_blocked is True
