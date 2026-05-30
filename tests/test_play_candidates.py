"""Tests for centralized play candidate discovery."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentshore.beads import BeadStatus
from agentshore.config.models import RuntimeConfig, TrustedIdsConfig
from agentshore.plays.candidates import (
    PlayCandidateService,
    build_candidate_plan,
    issue_available_for_plan,
)
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


def _agent_busy_on_pr(play_type: PlayType, pr_number: int) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id="agent-1",
        agent_type=AgentType.CODEX,
        status=AgentStatus.BUSY,
        context_size=10_000,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
        current_play_type=play_type,
        current_play_pr_number=pr_number,
    )


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
    graph.global_closure_ratio = 1.0
    return graph


def test_unapproved_mergeable_pr_is_reviewable_not_mergeable() -> None:
    plan = build_candidate_plan(
        _state(pull_requests=[_pr(350, mergeable="MERGEABLE", review_decision=None)])
    )

    assert [c.params.pr_number for c in plan.candidates_for(PlayType.CODE_REVIEW)] == [350]
    assert plan.candidates_for(PlayType.MERGE_PR) == ()
    assert plan.work_availability.reviewable_pr_count == 1
    assert plan.work_availability.mergeable_pr_count == 0
    assert plan.has_remaining_work is True


def test_pr_scoped_candidates_carry_branch_for_worktree_allocation() -> None:
    """Issue #567: every PR-scoped candidate (code_review, merge_pr, unblock_pr)
    must populate params.branch from the PullRequestSnapshot so the worktree
    allocator can find/create the branch. Before the fix, candidates omitted
    branch and worktree_allocate_failed fired on every dispatch with
    `PR-scoped play <type> dispatched without params.branch`, deadlocking any
    project that started AgentShore with pre-existing open PRs."""
    plan = build_candidate_plan(
        _state(
            pull_requests=[
                _pr(487, mergeable="MERGEABLE", review_decision=None),
                _pr(488, mergeable="MERGEABLE", review_decision="APPROVED"),
                _pr(
                    489,
                    mergeable="CONFLICTING",
                    review_decision=None,
                    blocked=True,
                    blocked_reasons=["merge_conflict"],
                ),
            ]
        )
    )
    for play_type in (PlayType.CODE_REVIEW, PlayType.MERGE_PR, PlayType.UNBLOCK_PR):
        for candidate in plan.candidates_for(play_type):
            assert candidate.params.branch is not None, (
                f"{play_type.value} candidate for PR {candidate.params.pr_number} "
                f"missing branch — would trip worktree_allocate_failed at dispatch"
            )
            assert candidate.params.branch == f"branch-{candidate.params.pr_number}"


def test_approved_or_pass_at_head_mergeable_pr_is_mergeable() -> None:
    plan = build_candidate_plan(
        _state(
            pull_requests=[
                _pr(351, mergeable="MERGEABLE", review_decision="APPROVED"),
                _pr(
                    352,
                    mergeable="MERGEABLE",
                    head_sha="abc123",
                    last_reviewed_sha="abc123",
                    last_review_status="PASS",
                ),
            ]
        )
    )

    assert [c.params.pr_number for c in plan.candidates_for(PlayType.MERGE_PR)] == [351, 352]
    assert plan.work_availability.mergeable_pr_count == 2


def test_changes_requested_with_agentshore_pass_at_head_is_mergeable() -> None:
    """When AgentShore logs PASS at the current head SHA, a CHANGES_REQUESTED review
    is treated as stale and does not block merge_pr.

    This is the data-layer half of the unblock_pr stale-review short-circuit:
    after the agent dismisses the stale review on GitHub (skill Step 2.2/6) and
    AgentShore records PASS at head_sha, merge_pr must be eligible. The previous
    regression (#315 — merge_pr thrashing) is now guarded by requiring humans
    to use the agentshore/manual-required or do-not-merge label to durably block,
    rather than relying on CHANGES_REQUESTED state alone.
    """

    plan = build_candidate_plan(
        _state(
            pull_requests=[
                _pr(
                    290,
                    mergeable="MERGEABLE",
                    head_sha="abc123",
                    last_reviewed_sha="abc123",
                    last_review_status="PASS",
                    review_decision="CHANGES_REQUESTED",
                ),
            ]
        )
    )

    assert [c.params.pr_number for c in plan.candidates_for(PlayType.MERGE_PR)] == [290]
    assert plan.work_availability.mergeable_pr_count == 1


def test_changes_requested_without_pass_at_head_blocks_merge() -> None:
    """A CHANGES_REQUESTED PR with no AgentShore PASS at head still blocks merge_pr."""

    plan = build_candidate_plan(
        _state(
            pull_requests=[
                _pr(
                    291,
                    mergeable="MERGEABLE",
                    head_sha="def456",
                    last_reviewed_sha="abc123",
                    last_review_status="PASS",
                    review_decision="CHANGES_REQUESTED",
                ),
            ]
        )
    )

    assert plan.candidates_for(PlayType.MERGE_PR) == ()
    assert plan.work_availability.mergeable_pr_count == 0


def test_pass_at_head_pr_with_blocked_label_is_not_mergeable() -> None:
    """A stale AgentShore PASS verdict must not keep a PR mergeable once a
    blocking label is added. Regression for #315 (merge_pr thrashing)."""

    plan = build_candidate_plan(
        _state(
            pull_requests=[
                _pr(
                    291,
                    mergeable="MERGEABLE",
                    head_sha="abc123",
                    last_reviewed_sha="abc123",
                    last_review_status="PASS",
                    labels=["blocked"],
                ),
            ]
        )
    )

    assert plan.candidates_for(PlayType.MERGE_PR) == ()
    assert plan.work_availability.mergeable_pr_count == 0


def test_issue_pickup_excludes_covered_blocked_disallowed_in_flight_and_merged() -> None:
    plan = build_candidate_plan(
        _state(
            open_issues=[
                _issue(1),
                _issue(2),
                _issue(3, ["agentshore/blocked"]),
                _issue(4, ["agentshore/disallowed"]),
                _issue(5),
                _issue(6),
            ],
            pull_requests=[
                _pr(20, issue_number=2),
                _pr(21, issue_number=6, state="MERGED"),
            ],
            in_flight_issues=[5],
        )
    )

    assert [c.params.issue_number for c in plan.candidates_for(PlayType.ISSUE_PICKUP)] == [1]


def test_issue_candidates_exclude_every_issue_linked_to_open_pr() -> None:
    plan = build_candidate_plan(
        _state(
            open_issues=[_issue(109), _issue(110), _issue(111)],
            pull_requests=[_pr(42, linked_issue_numbers=(109, 110))],
        )
    )

    assert [c.params.issue_number for c in plan.candidates_for(PlayType.ISSUE_PICKUP)] == [111]
    assert [
        c.params.issue_number for c in plan.candidates_for(PlayType.WRITE_IMPLEMENTATION_PLAN)
    ] == [111]
    assert plan.work_availability.covered_by_open_pr_count == 2


def test_issue_pickup_excludes_only_matching_in_progress_bead() -> None:
    graph = _seeded_graph(
        has_ready_tasks=True,
        tasks_ready=2,
        tasks=[
            SimpleNamespace(issue_number=1, status=BeadStatus.IN_PROGRESS, ready=False),
            SimpleNamespace(issue_number=2, status=BeadStatus.OPEN, ready=True),
        ],
    )
    plan = build_candidate_plan(_state(graph=graph, open_issues=[_issue(1), _issue(2)]))

    assert [c.params.issue_number for c in plan.candidates_for(PlayType.ISSUE_PICKUP)] == [2]
    assert plan.work_availability.bead_in_progress_issue_count == 1


def test_issue_pickup_excludes_dependency_blocked_bead() -> None:
    """#2: an issue whose beads task is blocked by an open dependency (a ``blocks``
    edge, now parsed into ``blocked_by_ids``) must be pre-masked from issue_pickup
    CANDIDATES so the policy never re-selects a deterministically dep-blocked issue.
    """
    graph = _seeded_graph(
        has_ready_tasks=True,
        tasks_ready=1,
        tasks=[
            # gh-964 is blocked by the still-open dep bead for gh-963.
            SimpleNamespace(
                issue_number=964,
                status=BeadStatus.OPEN,
                ready=False,
                blocked_by_ids=frozenset({"bd-963"}),
            ),
            SimpleNamespace(
                issue_number=965,
                status=BeadStatus.OPEN,
                ready=True,
                blocked_by_ids=frozenset(),
            ),
        ],
    )
    plan = build_candidate_plan(_state(graph=graph, open_issues=[_issue(964), _issue(965)]))

    assert [c.params.issue_number for c in plan.candidates_for(PlayType.ISSUE_PICKUP)] == [965]
    assert plan.work_availability.bead_blocked_issue_count == 1


def _in_progress_then_open_graph() -> MagicMock:
    """Graph where gh-6 is in_progress (orphaned) and gh-7 is open/ready."""
    return _seeded_graph(
        has_ready_tasks=True,
        tasks_ready=2,
        tasks=[
            SimpleNamespace(issue_number=6, status=BeadStatus.IN_PROGRESS, ready=False),
            SimpleNamespace(issue_number=7, status=BeadStatus.OPEN, ready=True),
        ],
    )


def test_in_progress_bead_excluded_from_plan_candidates() -> None:
    plan = build_candidate_plan(
        _state(graph=_in_progress_then_open_graph(), open_issues=[_issue(6), _issue(7)])
    )
    nums = [c.params.issue_number for c in plan.candidates_for(PlayType.WRITE_IMPLEMENTATION_PLAN)]
    assert 6 not in nums
    assert nums == [7]


def test_in_progress_bead_excluded_from_refine_candidates() -> None:
    plan = build_candidate_plan(
        _state(
            graph=_in_progress_then_open_graph(),
            open_issues=[
                _issue(6, labels=["agentshore/needs-refinement"]),
                _issue(7, labels=["agentshore/needs-refinement"]),
            ],
        )
    )
    nums = [c.params.issue_number for c in plan.candidates_for(PlayType.REFINE_TASK_BREAKDOWN)]
    assert 6 not in nums
    assert nums == [7]


def test_in_progress_bead_excluded_from_debug_candidates() -> None:
    plan = build_candidate_plan(
        _state(
            graph=_in_progress_then_open_graph(),
            open_issues=[_issue(6, labels=["agentshore/qa"]), _issue(7, labels=["agentshore/qa"])],
        )
    )
    nums = [c.params.issue_number for c in plan.candidates_for(PlayType.SYSTEMATIC_DEBUGGING)]
    assert 6 not in nums
    assert nums == [7]


def test_in_progress_bead_does_not_starve_workable_issues_e2e() -> None:
    # Reproduces the live fixation in-process: gh-6 stuck in_progress must not
    # be a candidate for ANY issue-track play, while the other workable issues
    # remain available. No agentshore CLI required.
    workable = [7, 12, 13, 14, 16, 18, 19]
    tasks: list[object] = [
        SimpleNamespace(issue_number=6, status=BeadStatus.IN_PROGRESS, ready=False)
    ]
    tasks += [SimpleNamespace(issue_number=n, status=BeadStatus.OPEN, ready=True) for n in workable]
    graph = _seeded_graph(has_ready_tasks=True, tasks_ready=len(workable), tasks=tasks)
    plan = build_candidate_plan(
        _state(graph=graph, open_issues=[_issue(6)] + [_issue(n) for n in workable])
    )

    for play_type in (
        PlayType.WRITE_IMPLEMENTATION_PLAN,
        PlayType.ISSUE_PICKUP,
        PlayType.REFINE_TASK_BREAKDOWN,
        PlayType.SYSTEMATIC_DEBUGGING,
    ):
        nums = [c.params.issue_number for c in plan.candidates_for(play_type)]
        assert 6 not in nums, f"{play_type} still offered in_progress #6"

    plan_nums = {
        c.params.issue_number for c in plan.candidates_for(PlayType.WRITE_IMPLEMENTATION_PLAN)
    }
    assert plan_nums == set(workable)


def test_issue_available_for_plan_free_function_excludes_in_progress() -> None:
    issue = _issue(6)
    state = _state(open_issues=[issue])
    kwargs = dict(
        open_pr_issue_numbers=set(),
        merged_pr_issue_numbers=set(),
        in_flight_issue_numbers=set(),
    )
    assert (
        issue_available_for_plan(issue, state, bead_in_progress_issue_numbers={6}, **kwargs)
        is False
    )
    # Default-None path is unchanged (issue still available).
    assert issue_available_for_plan(issue, state, **kwargs) is True


def test_beads_epics_without_ready_tasks_expose_groom_work() -> None:
    plan = build_candidate_plan(
        _state(
            graph=_seeded_graph(has_ready_tasks=False, tasks=[]),
            open_issues=[_issue(12, ["agentshore/planned", "agentshore/ai-slop"])],
            pull_requests=[_pr(350, mergeable="MERGEABLE")],
        )
    )

    assert plan.work_availability.beads_blocks_issue_pickup is True
    assert plan.work_availability.implementation_eligible_count == 0
    assert plan.work_availability.backlog_sync_work_count == 1
    assert len(plan.candidates_for(PlayType.GROOM_BACKLOG)) == 1
    assert plan.has_remaining_work is True
    assert plan.work_availability.terminal_no_work is False


def test_terminal_no_work_uses_actionable_candidates_not_raw_ready_task_count() -> None:
    ready_plan = build_candidate_plan(
        _state(
            graph=_seeded_graph(
                has_ready_tasks=True,
                tasks_ready=1,
                tasks=[SimpleNamespace(issue_number=12, ready=True)],
            )
        )
    )
    issue_plan = build_candidate_plan(_state(graph=_seeded_graph(), open_issues=[_issue(10)]))
    pr_plan = build_candidate_plan(
        _state(graph=_seeded_graph(), pull_requests=[_pr(20, review_decision=None)])
    )

    assert ready_plan.has_remaining_work is False
    assert issue_plan.has_remaining_work is True
    assert pr_plan.has_remaining_work is True
    assert ready_plan.work_availability.terminal_no_work is True
    assert issue_plan.work_availability.terminal_no_work is False
    assert pr_plan.work_availability.terminal_no_work is False


def test_seed_only_closed_graph_is_not_terminal_no_work() -> None:
    plan = build_candidate_plan(
        _state(
            graph=_seeded_graph(),
            plays_since_last_play_type={PlayType.SEED_PROJECT: 0},
            last_play_success_by_type={PlayType.SEED_PROJECT: True},
        )
    )

    assert plan.work_availability.terminal_no_work is False


def test_terminal_no_work_without_recent_qa_keeps_shutdown_work_remaining() -> None:
    plan = build_candidate_plan(
        _state(
            graph=_seeded_graph(),
            plays_since_last_play_type={
                PlayType.SEED_PROJECT: 0,
                PlayType.DESIGN_AUDIT: 0,
            },
            last_play_success_by_type={
                PlayType.SEED_PROJECT: True,
                PlayType.DESIGN_AUDIT: True,
            },
        )
    )

    assert plan.work_availability.terminal_no_work is True
    assert plan.has_remaining_work is True


def test_terminal_shutdown_requires_design_audit_inside_fifty_plays() -> None:
    plan = build_candidate_plan(
        _state(
            graph=_seeded_graph(),
            plays_since_last_play_type={
                PlayType.SEED_PROJECT: 0,
                PlayType.DESIGN_AUDIT: 50,
                PlayType.RUN_QA: 0,
            },
            last_play_success_by_type={
                PlayType.SEED_PROJECT: True,
                PlayType.DESIGN_AUDIT: True,
                PlayType.RUN_QA: True,
            },
        )
    )

    assert plan.work_availability.terminal_no_work is False
    assert plan.has_remaining_work is True


def test_terminal_shutdown_requires_successful_qa_inside_fifty_plays() -> None:
    stale_plan = build_candidate_plan(
        _state(
            graph=_seeded_graph(),
            plays_since_last_play_type={
                PlayType.SEED_PROJECT: 0,
                PlayType.DESIGN_AUDIT: 0,
                PlayType.RUN_QA: 50,
            },
            last_play_success_by_type={
                PlayType.SEED_PROJECT: True,
                PlayType.DESIGN_AUDIT: True,
                PlayType.RUN_QA: True,
            },
        )
    )
    failed_plan = build_candidate_plan(
        _state(
            graph=_seeded_graph(),
            plays_since_last_play_type={
                PlayType.SEED_PROJECT: 0,
                PlayType.DESIGN_AUDIT: 0,
                PlayType.RUN_QA: 1,
            },
            last_play_success_by_type={
                PlayType.SEED_PROJECT: True,
                PlayType.DESIGN_AUDIT: True,
                PlayType.RUN_QA: False,
            },
        )
    )

    assert stale_plan.work_availability.terminal_no_work is True
    assert stale_plan.has_remaining_work is True
    assert failed_plan.work_availability.terminal_no_work is True
    assert failed_plan.has_remaining_work is True


def test_pr_under_unblock_is_not_review_or_merge_candidate() -> None:
    plan = build_candidate_plan(
        _state(
            agents=[_agent_busy_on_pr(PlayType.UNBLOCK_PR, 404)],
            pull_requests=[
                _pr(
                    404,
                    issue_number=402,
                    blocked=True,
                    mergeable="MERGEABLE",
                    review_decision="APPROVED",
                )
            ],
        )
    )

    assert plan.candidates_for(PlayType.CODE_REVIEW) == ()
    assert plan.candidates_for(PlayType.MERGE_PR) == ()
    assert plan.candidates_for(PlayType.UNBLOCK_PR) == ()
    assert "resource already in flight" in plan.blocked_reasons_by_play_type[PlayType.MERGE_PR][0]


def test_pr_under_code_review_is_not_merge_or_unblock_candidate() -> None:
    plan = build_candidate_plan(
        _state(
            agents=[_agent_busy_on_pr(PlayType.CODE_REVIEW, 405)],
            pull_requests=[
                _pr(
                    405,
                    issue_number=403,
                    blocked=True,
                    mergeable="MERGEABLE",
                    review_decision="APPROVED",
                )
            ],
        )
    )

    assert plan.candidates_for(PlayType.MERGE_PR) == ()
    assert plan.candidates_for(PlayType.UNBLOCK_PR) == ()


def test_issue_linked_to_active_pr_is_unavailable_for_issue_work() -> None:
    plan = build_candidate_plan(
        _state(
            agents=[_agent_busy_on_pr(PlayType.CODE_REVIEW, 406)],
            open_issues=[_issue(404)],
            pull_requests=[_pr(406, issue_number=404, state="closed")],
        )
    )

    assert plan.candidates_for(PlayType.WRITE_IMPLEMENTATION_PLAN) == ()
    assert plan.candidates_for(PlayType.ISSUE_PICKUP) == ()
    assert plan.candidates_for(PlayType.REFINE_TASK_BREAKDOWN) == ()
    assert plan.candidates_for(PlayType.SYSTEMATIC_DEBUGGING) == ()
    assert (
        "resource already in flight" in plan.blocked_reasons_by_play_type[PlayType.ISSUE_PICKUP][0]
    )


@pytest.mark.asyncio
async def test_github_fallback_filters_active_resource_keys() -> None:
    github = MagicMock()
    github.list_pull_requests = AsyncMock(
        return_value=[
            _pr(
                407,
                issue_number=405,
                mergeable="MERGEABLE",
                review_decision="APPROVED",
                github_author="example-user",
            )
        ]
    )
    service = PlayCandidateService(
        store=MagicMock(),
        cfg=RuntimeConfig(trusted_ids=TrustedIdsConfig(github_logins=("example-user",))),
        github=github,
    )

    candidates = await service._github_pr_candidates(
        _state(
            agents=[_agent_busy_on_pr(PlayType.CODE_REVIEW, 407)],
            pull_requests=[_pr(407, issue_number=405)],
        ),
        PlayType.MERGE_PR,
        lambda pr: True,
        limit=5,
        log_key="github_pr_resolve_failed",
    )

    assert candidates == []


# ---------------------------------------------------------------------------
# Deterministic merge-side base gate (base != target_branch is never mergeable)
# ---------------------------------------------------------------------------

from agentshore.plays.candidates import pr_merge_ready  # noqa: E402


def _approved_pr(number: int, base_ref: str) -> PullRequestSnapshot:
    return _pr(
        number,
        base_ref=base_ref,
        mergeable="MERGEABLE",
        review_decision="APPROVED",
    )


def test_pr_merge_ready_rejects_base_mismatch() -> None:
    pr = _approved_pr(50, "main")
    assert pr_merge_ready(pr) is True  # no target → unchanged behavior
    assert pr_merge_ready(pr, target_branch="integration") is False
    assert pr_merge_ready(_approved_pr(51, "integration"), target_branch="integration") is True


def test_merge_pr_candidate_excludes_wrong_base() -> None:
    plan = build_candidate_plan(
        _state(pull_requests=[_approved_pr(50, "main")], target_branch="integration")
    )
    assert plan.candidates_for(PlayType.MERGE_PR) == ()


def test_merge_pr_candidate_includes_matching_base() -> None:
    plan = build_candidate_plan(
        _state(pull_requests=[_approved_pr(51, "integration")], target_branch="integration")
    )
    nums = [c.params.pr_number for c in plan.candidates_for(PlayType.MERGE_PR)]
    assert nums == [51]
