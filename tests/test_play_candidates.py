"""Tests for centralized play candidate discovery."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentshore.beads import BeadStatus
from agentshore.config.models import RuntimeConfig, TrustedIdsConfig
from agentshore.plays.candidates import (
    PlayCandidateAnalyzer,
    PlayCandidateService,
    build_candidate_plan,
)
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    IssueSnapshot,
    OrchestratorState,
    PendingReviewSnapshot,
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


def _reviewer(
    agent_id: str,
    agent_type: AgentType,
    *,
    tasks_completed: int = 1,
    tasks_failed: int = 0,
    timeout_count: int = 0,
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=agent_type,
        status=AgentStatus.IDLE,
        context_size=10_000,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=tasks_completed,
        tasks_failed=tasks_failed,
        timeout_count=timeout_count,
        # code_review is large-only (#254); reviewers must be large tier.
        model_tier="large",
    )


def test_idle_can_review_excludes_circuit_broken_reviewer() -> None:
    """A dead reviewer (0 successes + timeout) is dropped from the review pool (#22).

    Mirrors the live grok-ETIMEDOUT case: a configured reviewer that produced
    0 successful calls and timed out must not be pinned for code_review.
    """
    from agentshore.plays.candidates import idle_can_review_agents

    dead_grok = _reviewer("g1", AgentType.GROK, tasks_completed=0, timeout_count=1)
    healthy_claude = _reviewer("c1", AgentType.CLAUDE_CODE, tasks_completed=2)
    pool = idle_can_review_agents(_state(agents=[dead_grok, healthy_claude]))
    assert [a.agent_id for a in pool] == ["c1"]


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


def test_open_pr_review_fallback_not_shadowed_by_stale_queue_rows() -> None:
    """Stale, undispatchable pending_review_queue rows must not suppress review of
    reviewable open PRs.

    Regression for the wedge: a manual-required PR's pending queue row (which
    yields no dispatchable candidate) made ``pending_review_queue`` non-empty, so
    the open-PR review fallback was skipped entirely and a perfectly reviewable
    MERGEABLE PR never became a code_review candidate (actionable_pr_work=0 →
    issue_pickup stayed capped at MAX_OPEN_PRS → session wedge).
    """
    parked = _pr(190, mergeable="CONFLICTING", labels=["agentshore/manual-required"])
    reviewable = _pr(414, mergeable="MERGEABLE", review_decision=None)
    plan = build_candidate_plan(
        _state(
            pull_requests=[parked, reviewable],
            pending_review_queue=[
                PendingReviewSnapshot(
                    queue_id=1,
                    pr_number=190,
                    author_label="agent-x",
                    enqueued_at="2026-06-30T00:00:00+00:00",
                )
            ],
        )
    )

    review_prs = [c.params.pr_number for c in plan.candidates_for(PlayType.CODE_REVIEW)]
    assert review_prs == [414]  # reviewable PR picked up; manual-required 190 stays parked


def test_queued_reviewable_pr_not_double_added_by_fallback() -> None:
    """A PR present in the queue must appear exactly once, not also via the
    always-on open-PR fallback."""
    pr = _pr(500, mergeable="MERGEABLE", review_decision=None)
    plan = build_candidate_plan(
        _state(
            pull_requests=[pr],
            pending_review_queue=[
                PendingReviewSnapshot(
                    queue_id=7,
                    pr_number=500,
                    author_label="agent-y",
                    enqueued_at="2026-06-30T00:00:00+00:00",
                )
            ],
        )
    )

    review_prs = [c.params.pr_number for c in plan.candidates_for(PlayType.CODE_REVIEW)]
    assert review_prs == [500]  # exactly once, from the queue (priority) lane
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


def test_changes_requested_with_agentshore_pass_at_head_blocks_merge() -> None:
    """#344: an AgentShore PASS at head never overrides a live CHANGES_REQUESTED.

    A PASS logged at the same head SHA as a fresh human CHANGES_REQUESTED is
    indistinguishable in order from the legit unblock case, so the old
    stale-review dismissal could not tell a co-SHA human block from a cleared
    one — it fired on the live verdict and merge_pr fixated on a PR a human had
    explicitly blocked, starving every genuinely-approved PR. A current
    CHANGES_REQUESTED now always blocks; it clears only when GitHub's
    reviewDecision changes (a fresh review/approval at the new head).
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

    assert plan.candidates_for(PlayType.MERGE_PR) == ()
    assert plan.work_availability.mergeable_pr_count == 0


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


def test_needs_human_label_excludes_issue_from_plan_and_pickup() -> None:
    """#458: an issue parked with agentshore/needs-human is dropped from both
    write_implementation_plan and issue_pickup, so the planner stops
    re-selecting an un-plannable issue every tick."""
    plan = build_candidate_plan(
        _state(
            open_issues=[_issue(1), _issue(458, labels=["agentshore/needs-human"])],
        )
    )

    plan_nums = [
        c.params.issue_number for c in plan.candidates_for(PlayType.WRITE_IMPLEMENTATION_PLAN)
    ]
    pickup_nums = [c.params.issue_number for c in plan.candidates_for(PlayType.ISSUE_PICKUP)]
    assert 458 not in plan_nums
    assert 458 not in pickup_nums
    assert 1 in plan_nums


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


def test_refined_issue_excluded_from_refine_candidates() -> None:
    # An issue marked agentshore/refined is not a refine candidate even though
    # it still carries needs-refinement; removing refined re-arms it.
    plan = build_candidate_plan(
        _state(
            open_issues=[
                _issue(6, labels=["agentshore/needs-refinement", "agentshore/refined"]),
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


def test_analyzer_issue_available_for_plan_excludes_in_progress() -> None:
    issue = _issue(6)
    in_progress_graph = _seeded_graph(
        has_ready_tasks=True,
        tasks_ready=1,
        tasks=[SimpleNamespace(issue_number=6, status=BeadStatus.IN_PROGRESS, ready=False)],
    )
    analyzer_in_progress = PlayCandidateAnalyzer(
        _state(graph=in_progress_graph, open_issues=[issue])
    )
    assert analyzer_in_progress.issue_available_for_plan(issue) is False
    # With no in-progress bead the same issue is still available for planning.
    analyzer_open = PlayCandidateAnalyzer(_state(open_issues=[issue]))
    assert analyzer_open.issue_available_for_plan(issue) is True


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


# ---------------------------------------------------------------------------
# Opt-in trusted-issue-author gating (Phase B)
# ---------------------------------------------------------------------------


def _authored_issue(
    number: int, author: str | None, labels: list[str] | None = None
) -> IssueSnapshot:
    return IssueSnapshot(
        issue_number=number,
        title=f"Issue {number}",
        state="open",
        priority=None,
        labels=labels or [],
        source=None,
        github_author=author,
    )


def _gated_state(open_issues: list[IssueSnapshot], **kwargs: object) -> OrchestratorState:
    """State with issue-author gating ON and ``trusted-user`` as the trusted set.

    The trusted set is resolved once per tick at assembly in production
    (``assemble_state`` → ``trusted_issue_author_logins``); the candidate
    analyzer reads it straight off the state, so tests set it directly.
    """
    return _state(
        open_issues=open_issues,
        restrict_issues_to_trusted_authors=True,
        trusted_issue_authors=frozenset({"trusted-user"}),
        **kwargs,
    )


def test_untrusted_issue_workable_when_gating_off() -> None:
    # Default state has the toggle off → author is never consulted.
    state = _state(open_issues=[_authored_issue(1, "stranger")])

    plan = build_candidate_plan(state)

    pickup_nums = [c.params.issue_number for c in plan.candidates_for(PlayType.ISSUE_PICKUP)]
    assert pickup_nums == [1]
    assert plan.work_availability.untrusted_issue_count == 0


def test_untrusted_issue_excluded_from_all_issue_plays_when_gating_on() -> None:
    state = _gated_state(
        [
            _authored_issue(1, "stranger"),
            _authored_issue(2, "stranger", labels=["agentshore/needs-refinement"]),
            _authored_issue(3, "stranger", labels=["bug"]),
        ]
    )

    plan = build_candidate_plan(state)

    assert plan.candidates_for(PlayType.ISSUE_PICKUP) == ()
    assert plan.candidates_for(PlayType.WRITE_IMPLEMENTATION_PLAN) == ()
    assert plan.candidates_for(PlayType.REFINE_TASK_BREAKDOWN) == ()
    assert plan.candidates_for(PlayType.SYSTEMATIC_DEBUGGING) == ()
    assert plan.work_availability.untrusted_issue_count == 3
    assert plan.work_availability.workable_issue_count == 0


def test_trusted_login_issue_stays_workable_when_gating_on() -> None:
    state = _gated_state([_authored_issue(1, "trusted-user")])

    plan = build_candidate_plan(state)

    pickup_nums = [c.params.issue_number for c in plan.candidates_for(PlayType.ISSUE_PICKUP)]
    plan_nums = [
        c.params.issue_number for c in plan.candidates_for(PlayType.WRITE_IMPLEMENTATION_PLAN)
    ]
    assert pickup_nums == [1]
    assert plan_nums == [1]
    assert plan.work_availability.untrusted_issue_count == 0


def test_agent_identity_issue_stays_workable_when_gating_on() -> None:
    # Agent identities are folded into the trusted set at assembly (covered by
    # trusted_issue_author_logins tests); here the resolved bot login is in the
    # state's trusted set, and its issues stay workable. Author match is
    # case-insensitive (canonicalized).
    state = _state(
        open_issues=[_authored_issue(1, "AgentShoreBot")],
        restrict_issues_to_trusted_authors=True,
        trusted_issue_authors=frozenset({"agentshorebot"}),
    )

    plan = build_candidate_plan(state)

    pickup_nums = [c.params.issue_number for c in plan.candidates_for(PlayType.ISSUE_PICKUP)]
    assert pickup_nums == [1]
    assert plan.work_availability.untrusted_issue_count == 0


def test_null_author_issue_excluded_when_gating_on() -> None:
    state = _gated_state([_authored_issue(1, None)])

    plan = build_candidate_plan(state)

    assert plan.candidates_for(PlayType.ISSUE_PICKUP) == ()
    assert plan.work_availability.untrusted_issue_count == 1


# --- Piece A: parked-resource exclusion (issue #60 backstop) ------------------


def test_parked_pr_is_excluded_from_unblock_candidates() -> None:
    """A resource parked after repeated worktree-allocation failures is excluded
    from every play that touches it, so it can't be re-selected each tick."""
    conflicting = _pr(
        489,
        mergeable="CONFLICTING",
        review_decision=None,
        blocked=True,
        blocked_reasons=["merge_conflict"],
    )
    # Control: without parking, #489 is an unblock_pr candidate.
    unparked = build_candidate_plan(_state(pull_requests=[conflicting]))
    assert 489 in [c.params.pr_number for c in unparked.candidates_for(PlayType.UNBLOCK_PR)]

    # Parked: the candidate disappears and a "parked" blocked-reason is recorded.
    parked = build_candidate_plan(
        _state(pull_requests=[conflicting], parked_resource_keys=frozenset({"pr:489"}))
    )
    assert parked.candidates_for(PlayType.UNBLOCK_PR) == ()
    reasons = parked.blocked_reasons_by_play_type.get(PlayType.UNBLOCK_PR, ())
    assert any("parked" in r and "pr:489" in r for r in reasons)


# ---------------------------------------------------------------------------
# #312 — fast per-PR repick cooldown excludes recently-failed PRs from both
# resolver-time candidate builders, and lets them back in once the cooldown
# window (PR_REPICK_COOLDOWN_SPEC.cooldown plays) elapses.
# ---------------------------------------------------------------------------

from agentshore.cooldown import Cooldown  # noqa: E402
from agentshore.plays.candidates import PR_REPICK_COOLDOWN_SPEC  # noqa: E402


def _repick_cooldown() -> Cooldown[int]:
    return Cooldown(PR_REPICK_COOLDOWN_SPEC)


@pytest.mark.asyncio
async def test_unblock_pr_candidates_excludes_pr_on_repick_cooldown() -> None:
    """A PR that just failed unblock_pr with merge_conflicts (cooldown armed
    for its number) is excluded from _unblock_pr_candidates, even though it
    is otherwise unblockable and far from the permanent exhaustion count."""
    conflicting = _pr(489, mergeable="CONFLICTING", review_decision=None, blocked=True)
    store = MagicMock()
    store.list_open_pull_requests = AsyncMock(return_value=[conflicting])
    cooldown = _repick_cooldown()
    service = PlayCandidateService(
        store=store, cfg=RuntimeConfig(), github=None, pr_repick_cooldown=cooldown
    )
    state = _state(pull_requests=[conflicting])

    # Sanity: it's a candidate before any failure is recorded.
    before = await service.candidates_for(PlayType.UNBLOCK_PR, state)
    assert [c.params.pr_number for c in before] == [489]

    cooldown.record_failure(489, now=state.total_plays)

    after = await service.candidates_for(PlayType.UNBLOCK_PR, state)
    assert after == []


@pytest.mark.asyncio
async def test_unblock_pr_candidates_reappear_after_cooldown_window() -> None:
    conflicting = _pr(489, mergeable="CONFLICTING", review_decision=None, blocked=True)
    store = MagicMock()
    store.list_open_pull_requests = AsyncMock(return_value=[conflicting])
    cooldown = _repick_cooldown()
    cooldown.record_failure(489, now=0)
    service = PlayCandidateService(
        store=store, cfg=RuntimeConfig(), github=None, pr_repick_cooldown=cooldown
    )

    still_cooling = _state(
        pull_requests=[conflicting], total_plays=PR_REPICK_COOLDOWN_SPEC.cooldown - 1
    )
    assert await service.candidates_for(PlayType.UNBLOCK_PR, still_cooling) == []

    expired = _state(pull_requests=[conflicting], total_plays=PR_REPICK_COOLDOWN_SPEC.cooldown)
    candidates = await service.candidates_for(PlayType.UNBLOCK_PR, expired)
    assert [c.params.pr_number for c in candidates] == [489]


@pytest.mark.asyncio
async def test_merge_pr_candidates_excludes_pr_on_repick_cooldown() -> None:
    """A PR that just failed merge_pr with dirty_trunk is excluded from
    _merge_pr_candidates — there is no permanent counter for merge_pr at all
    today, so this is the only memory layer for it."""
    approved = _pr(51, mergeable="MERGEABLE", review_decision="APPROVED")
    store = MagicMock()
    store.list_approved_pull_requests = AsyncMock(return_value=[approved])
    cooldown = _repick_cooldown()
    service = PlayCandidateService(
        store=store, cfg=RuntimeConfig(), github=None, pr_repick_cooldown=cooldown
    )
    state = _state(pull_requests=[approved])

    before = await service.candidates_for(PlayType.MERGE_PR, state)
    assert [c.params.pr_number for c in before] == [51]

    cooldown.record_failure(51, now=state.total_plays)

    after = await service.candidates_for(PlayType.MERGE_PR, state)
    assert after == []


@pytest.mark.asyncio
async def test_merge_pr_candidates_reappear_after_cooldown_window() -> None:
    approved = _pr(51, mergeable="MERGEABLE", review_decision="APPROVED")
    store = MagicMock()
    store.list_approved_pull_requests = AsyncMock(return_value=[approved])
    cooldown = _repick_cooldown()
    cooldown.record_failure(51, now=0)
    service = PlayCandidateService(
        store=store, cfg=RuntimeConfig(), github=None, pr_repick_cooldown=cooldown
    )

    still_cooling = _state(
        pull_requests=[approved], total_plays=PR_REPICK_COOLDOWN_SPEC.cooldown - 1
    )
    assert await service.candidates_for(PlayType.MERGE_PR, still_cooling) == []

    expired = _state(pull_requests=[approved], total_plays=PR_REPICK_COOLDOWN_SPEC.cooldown)
    candidates = await service.candidates_for(PlayType.MERGE_PR, expired)
    assert [c.params.pr_number for c in candidates] == [51]


@pytest.mark.asyncio
async def test_unblock_pr_candidates_rearm_when_no_longer_conflicting() -> None:
    """#312: a rearmable cooldown entry clears the instant the PR's live
    ``mergeable`` field is no longer CONFLICTING — mirrors
    issue_pickup._rearm_ready_issues, ahead of the fixed window."""
    recovered = _pr(489, mergeable="MERGEABLE", review_decision="CHANGES_REQUESTED")
    store = MagicMock()
    store.list_open_pull_requests = AsyncMock(return_value=[recovered])
    cooldown = _repick_cooldown()
    cooldown.record_failure(489, now=0)
    rearmable = {489: True}
    service = PlayCandidateService(
        store=store,
        cfg=RuntimeConfig(),
        github=None,
        pr_repick_cooldown=cooldown,
        pr_repick_cooldown_rearmable=rearmable,
    )

    state = _state(pull_requests=[recovered], total_plays=1)  # well inside the window
    candidates = await service.candidates_for(PlayType.UNBLOCK_PR, state)
    assert [c.params.pr_number for c in candidates] == [489]


# ---------------------------------------------------------------------------
# Parity: PlayCandidateService.candidates_for agrees with build_candidate_plan
# wherever the service adds no store/GitHub-only candidates. Guards the unified
# single source of candidate logic against future drift.
# ---------------------------------------------------------------------------


def _candidate_keys(candidates: object) -> set[tuple[object, object, object]]:
    """Identity of a candidate ignoring resolver-only fields (target_agent_id)."""
    return {
        (c.play_type, c.params.issue_number, c.params.pr_number)
        for c in candidates  # type: ignore[attr-defined]
    }


async def test_service_candidates_match_build_plan_for_each_play_type() -> None:
    """For a representative state, the resolver service and the pure-state plan
    select the same candidate set per play type, once the store mirrors live
    state and there is no GitHub fallback (the only service-specific additions).
    """
    issues = [_issue(1), _issue(2, labels=["agentshore/needs-refinement"]), _issue(3)]
    review_pr = _pr(10, review_decision=None)  # needs review
    merge_pr = _pr(11, mergeable="MERGEABLE", review_decision="APPROVED")
    unblock_pr = _pr(12, mergeable="CONFLICTING", blocked=True, blocked_reasons=["merge_conflict"])
    prs = [review_pr, merge_pr, unblock_pr]
    reviewer = _reviewer("c1", AgentType.CLAUDE_CODE)

    state = _state(open_issues=issues, pull_requests=prs, agents=[reviewer])
    plan = build_candidate_plan(state)

    store = MagicMock()
    # Store mirrors live state so the store-backed passes have no extra rows.
    store.list_pending_reviews = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=prs)
    store.list_open_pull_requests = AsyncMock(return_value=prs)
    store.complete_review = AsyncMock()
    service = PlayCandidateService(store=store, cfg=RuntimeConfig(), github=None)

    for play_type in (
        PlayType.WRITE_IMPLEMENTATION_PLAN,
        PlayType.REFINE_TASK_BREAKDOWN,
        PlayType.SYSTEMATIC_DEBUGGING,
        PlayType.ISSUE_PICKUP,
        PlayType.CODE_REVIEW,
        PlayType.MERGE_PR,
        PlayType.UNBLOCK_PR,
    ):
        service_candidates = await service.candidates_for(
            play_type, state, idle_reviewers=[reviewer]
        )
        assert _candidate_keys(service_candidates) == _candidate_keys(
            plan.candidates_for(play_type)
        ), f"candidate set diverged for {play_type}"


# ---------------------------------------------------------------------------
# Candidate agent-type resolution (used by the launch-wedge cooldown mask).
# ---------------------------------------------------------------------------

from agentshore.plays.base import PlayParams  # noqa: E402
from agentshore.plays.candidates import (  # noqa: E402
    PlayCandidate,
    _candidate_resolved_agent_type,
)


def _candidate(play_type: PlayType, params: PlayParams) -> PlayCandidate:
    return PlayCandidate(
        play_type=play_type,
        params=params,
        resource_keys=(),
        source="test",
        sort_key=(0,),
    )


def test_candidate_resolved_agent_type_prefers_target_agent_type() -> None:
    c = _candidate(PlayType.INSTANTIATE_AGENT, PlayParams(target_agent_type="codex"))
    assert _candidate_resolved_agent_type(c, {}) == "codex"


def test_candidate_resolved_agent_type_maps_target_agent_id() -> None:
    c = _candidate(PlayType.CODE_REVIEW, PlayParams(target_agent_id="agent-7"))
    assert _candidate_resolved_agent_type(c, {"agent-7": "codex"}) == "codex"


def test_candidate_resolved_agent_type_none_when_unresolvable() -> None:
    c = _candidate(PlayType.ISSUE_PICKUP, PlayParams(issue_number=1))
    assert _candidate_resolved_agent_type(c, {}) is None


def test_candidate_resolved_agent_type_maps_concrete_agent_id() -> None:
    c = _candidate(PlayType.CODE_REVIEW, PlayParams(target_agent_id="codex-1"))
    assert _candidate_resolved_agent_type(c, {"codex-1": "codex"}) == "codex"
