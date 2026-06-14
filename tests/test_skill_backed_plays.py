"""Tests for skill-backed play classes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agentshore.agents.handle import AgentInvocationResult
from agentshore.play_rules import needs_review
from agentshore.plays.base import PlayParams
from agentshore.plays.skill_backed.calibrate_alignment import CalibrateAlignmentPlay
from agentshore.plays.skill_backed.code_review import CodeReviewPlay
from agentshore.plays.skill_backed.design_audit import DesignAuditPlay
from agentshore.plays.skill_backed.groom_backlog import GroomBacklogPlay
from agentshore.plays.skill_backed.issue_pickup import IssuePickupPlay
from agentshore.plays.skill_backed.merge_pr import MergePRPlay
from agentshore.plays.skill_backed.refine_tasks import RefineTaskBreakdownPlay
from agentshore.plays.skill_backed.run_qa import RunQAPlay
from agentshore.plays.skill_backed.seed_project import SeedProjectPlay
from agentshore.plays.skill_backed.systematic_debugging import SystematicDebuggingPlay
from agentshore.plays.skill_backed.unblock_pr import UnblockPrPlay
from agentshore.plays.skill_backed.write_plan import WriteImplementationPlanPlay
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snap(
    agent_id: str = "a1",
    agent_type: AgentType = AgentType.CLAUDE_CODE,
    status: AgentStatus = AgentStatus.IDLE,
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=agent_type,
        status=status,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=1,
        tasks_failed=0,
    )


def _pr(
    num: int = 1,
    state: str = "open",
    issue_number: int | None = None,
    is_draft: bool = False,
    blocked: bool = False,
    labels: list[str] | None = None,
    mergeable: str | None = None,
    head_sha: str | None = None,
    last_reviewed_sha: str | None = None,
    last_review_status: str | None = None,
    review_decision: str | None = None,
) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        pr_number=num,
        title="Test PR",
        state=state,
        branch=None,
        issue_number=issue_number,
        labels=labels or [],
        review_decision=review_decision,
        status_check_summary=None,
        is_draft=is_draft,
        blocked=blocked,
        blocked_reasons=[],
        mergeable=mergeable,
        head_sha=head_sha,
        last_reviewed_sha=last_reviewed_sha,
        last_review_status=last_review_status,
    )


def _issue(num: int = 1, labels: list[str] | None = None) -> IssueSnapshot:
    return IssueSnapshot(
        issue_number=num,
        title="Test",
        state="open",
        priority=None,
        labels=labels or [],
        source=None,
    )


def _pending_review(
    queue_id: int = 1,
    pr_number: int = 1,
    author_label: str | None = None,
) -> PendingReviewSnapshot:
    return PendingReviewSnapshot(
        queue_id=queue_id,
        pr_number=pr_number,
        author_label=author_label,
        enqueued_at="2026-01-01T00:00:00Z",
    )


def _state(
    agents: list[AgentSnapshot] | None = None,
    issues: list[IssueSnapshot] | None = None,
    pull_requests: list[PullRequestSnapshot] | None = None,
    pending_review_queue: list[PendingReviewSnapshot] | None = None,
    total_plays: int = 6,
    in_flight_plays: list[PlayType] | None = None,
    plays_since_last_play_type: dict[PlayType, int] | None = None,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="sess",
        session_state=SessionState.RUNNING,
        total_plays=total_plays,
        total_cost=0.0,
        agents=[_snap()] if agents is None else agents,
        open_issues=[] if issues is None else issues,
        pull_requests=[] if pull_requests is None else pull_requests,
        pending_review_queue=([] if pending_review_queue is None else pending_review_queue),
        in_flight_plays=[] if in_flight_plays is None else in_flight_plays,
        plays_since_last_play_type=(
            {} if plays_since_last_play_type is None else plays_since_last_play_type
        ),
    )


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.manager.dispatch = AsyncMock()
    ctx.store.list_learnings = AsyncMock(return_value=[])
    ctx.store.list_pending_reviews = AsyncMock(return_value=[])
    ctx.store.complete_review = AsyncMock()
    ctx.session_id = "sess"
    ctx.play_id = 1
    ctx.project_path = MagicMock()
    ctx.cfg.mode = "solo"
    ctx.cfg.budget.enabled = True
    ctx.cfg.budget.total = 5.0
    return ctx


# ---------------------------------------------------------------------------
# Newly filled slot plays
# ---------------------------------------------------------------------------


def test_unblock_pr_precondition_requires_idle_implementer() -> None:
    agents = [_snap(status=AgentStatus.BUSY)]
    assert UnblockPrPlay().preconditions(_state(agents=agents)) != []


def test_unblock_pr_precondition_requires_blocked_pr() -> None:
    # An open but non-blocked PR is not enough — needs at least one blocked PR.
    prs = [_pr(num=1, state="open", blocked=False)]
    assert UnblockPrPlay().preconditions(_state(agents=[_snap()], pull_requests=prs)) != []


def test_unblock_pr_precondition_met() -> None:
    prs = [_pr(num=1, state="open", blocked=True)]
    assert UnblockPrPlay().preconditions(_state(agents=[_snap()], pull_requests=prs)) == []


def test_unblock_pr_precondition_skips_manual_required_pr() -> None:
    prs = [_pr(num=1, state="open", blocked=True, labels=["agentshore/manual-required"])]

    errors = UnblockPrPlay().preconditions(_state(agents=[_snap()], pull_requests=prs))

    assert errors


@pytest.mark.asyncio
async def test_unblock_pr_stale_review_counts_as_pass_review() -> None:
    """A stale-review unblock is itself the AgentShore review pass."""
    from unittest.mock import patch

    from agentshore.plays.skill_backed.base import SkillBackedPlay
    from agentshore.state import PlayOutcome

    skill_outcome = PlayOutcome(
        play_type=PlayType.UNBLOCK_PR,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=10,
        dollar_cost=0.01,
        artifacts=[
            {
                "type": "stale_review_state",
                "pr": 42,
                "stale_sha": "old123",
                "head_sha": "new456",
            }
        ],
        alignment_delta=0.0,
    )

    play = UnblockPrPlay()
    ctx = _ctx()
    ctx.store.update_pr_last_reviewed_sha = AsyncMock()
    ctx.store.list_pending_reviews = AsyncMock(return_value=[_pending_review(pr_number=42)])

    with patch.object(SkillBackedPlay, "execute", AsyncMock(return_value=skill_outcome)):
        outcome = await play.execute(_state(), PlayParams(pr_number=42), ctx=ctx)

    assert outcome.success is True
    ctx.store.update_pr_last_reviewed_sha.assert_awaited_once_with(
        42, "sess", "new456", status="PASS"
    )
    ctx.store.complete_review.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_unblock_pr_attempt_counts_as_pass_review() -> None:
    """A successful blocker fix also satisfies AgentShore's review gate."""
    from unittest.mock import patch

    from agentshore.plays.skill_backed.base import SkillBackedPlay
    from agentshore.state import PlayOutcome

    skill_outcome = PlayOutcome(
        play_type=PlayType.UNBLOCK_PR,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=10,
        dollar_cost=0.01,
        artifacts=[{"type": "pr_unblock_attempt", "number": 52, "head_sha": "fix789"}],
        alignment_delta=0.0,
    )

    play = UnblockPrPlay()
    ctx = _ctx()
    ctx.store.update_pr_last_reviewed_sha = AsyncMock()

    with patch.object(SkillBackedPlay, "execute", AsyncMock(return_value=skill_outcome)):
        await play.execute(_state(), PlayParams(pr_number=52), ctx=ctx)

    ctx.store.update_pr_last_reviewed_sha.assert_awaited_once_with(
        52, "sess", "fix789", status="PASS"
    )


@pytest.mark.asyncio
async def test_unblock_pr_reconciles_merged_blocker() -> None:
    """A `pr_merged` artifact (stacked blocker merged in place) is reconciled
    into the local cache, and the target's own unblock still records a PASS."""
    from unittest.mock import patch

    from agentshore.plays.skill_backed.base import SkillBackedPlay
    from agentshore.state import PlayOutcome

    skill_outcome = PlayOutcome(
        play_type=PlayType.UNBLOCK_PR,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=10,
        dollar_cost=0.01,
        artifacts=[
            {"type": "pr_merged", "pr": 99, "head_sha": "blk999"},
            {"type": "pr_unblock_attempt", "number": 40, "head_sha": "tgt040"},
        ],
        alignment_delta=0.0,
    )

    play = UnblockPrPlay()
    ctx = _ctx()
    ctx.store.update_pr_last_reviewed_sha = AsyncMock()
    with (
        patch.object(SkillBackedPlay, "execute", AsyncMock(return_value=skill_outcome)),
        patch(
            "agentshore.plays.skill_backed.unblock_pr.reconcile_merged_pr",
            AsyncMock(return_value=[]),
        ) as reconcile,
    ):
        outcome = await play.execute(_state(), PlayParams(pr_number=40), ctx=ctx)

    assert outcome.success is True
    reconcile.assert_awaited_once()
    assert reconcile.await_args.args[0] == 99
    # Only the target (pr_unblock_attempt) flows through the review loop; the
    # merged blocker is handled by reconciliation, not review.
    ctx.store.update_pr_last_reviewed_sha.assert_awaited_once_with(
        40, "sess", "tgt040", status="PASS"
    )


@pytest.mark.asyncio
async def test_unblock_pr_reconciles_merged_blocker_on_failure() -> None:
    """Partial success: the blocker merged but the target still has its own
    blocker. The merged sibling must still be reconciled (runs before the
    success early-return)."""
    from unittest.mock import patch

    from agentshore.plays.skill_backed.base import SkillBackedPlay
    from agentshore.state import PlayOutcome

    skill_outcome = PlayOutcome(
        play_type=PlayType.UNBLOCK_PR,
        agent_id="a1",
        success=False,
        partial=False,
        duration_seconds=1.0,
        token_cost=10,
        dollar_cost=0.01,
        artifacts=[{"type": "pr_merged", "pr": 99, "head_sha": "blk999"}],
        alignment_delta=0.0,
        error="merge_conflicts",
    )

    play = UnblockPrPlay()
    ctx = _ctx()
    with (
        patch.object(SkillBackedPlay, "execute", AsyncMock(return_value=skill_outcome)),
        patch(
            "agentshore.plays.skill_backed.unblock_pr.reconcile_merged_pr",
            AsyncMock(return_value=[]),
        ) as reconcile,
    ):
        outcome = await play.execute(_state(), PlayParams(pr_number=40), ctx=ctx)

    assert outcome.success is False
    reconcile.assert_awaited_once()
    assert reconcile.await_args.args[0] == 99


@pytest.mark.asyncio
async def test_unblock_pr_no_reconcile_without_pr_merged() -> None:
    """No `pr_merged` artifact → reconciliation is never invoked."""
    from unittest.mock import patch

    from agentshore.plays.skill_backed.base import SkillBackedPlay
    from agentshore.state import PlayOutcome

    skill_outcome = PlayOutcome(
        play_type=PlayType.UNBLOCK_PR,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=10,
        dollar_cost=0.01,
        artifacts=[{"type": "pr_unblock_attempt", "number": 40, "head_sha": "tgt040"}],
        alignment_delta=0.0,
    )

    play = UnblockPrPlay()
    ctx = _ctx()
    ctx.store.update_pr_last_reviewed_sha = AsyncMock()
    with (
        patch.object(SkillBackedPlay, "execute", AsyncMock(return_value=skill_outcome)),
        patch(
            "agentshore.plays.skill_backed.unblock_pr.reconcile_merged_pr",
            AsyncMock(return_value=[]),
        ) as reconcile,
    ):
        await play.execute(_state(), PlayParams(pr_number=40), ctx=ctx)

    reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_merged_pr_marks_completes_and_closes() -> None:
    """The shared helper marks the PR merged, completes its reviews, and closes
    its linked issues — mirroring merge_pr's post-merge propagation."""
    from unittest.mock import patch

    from agentshore.plays.skill_backed import _merge_reconcile

    ctx = _ctx()
    ctx.store.mark_pr_merged = AsyncMock()
    ctx.store.complete_reviews_for_pr = AsyncMock()
    ctx.store.update_issues_state_batch = AsyncMock()
    ctx.cfg.project.target_branch = "integration"
    state = _state()  # graph defaults to None → beads loop is a no-op

    with (
        patch(
            "agentshore.plays.skill_backed._merge_reconcile._fetch_pr_links",
            AsyncMock(return_value=(7,)),
        ),
        patch(
            "agentshore.plays.skill_backed._merge_reconcile.fast_forward_local_branch",
            AsyncMock(),
        ),
        patch(
            "agentshore.plays.skill_backed._merge_reconcile.resolve_ff_fetch_overlay",
            return_value=None,
        ),
    ):
        result = await _merge_reconcile.reconcile_merged_pr(99, ctx=ctx, state=state)

    ctx.store.mark_pr_merged.assert_awaited_once_with(99, "sess")
    ctx.store.complete_reviews_for_pr.assert_awaited_once_with("sess", 99)
    ctx.store.update_issues_state_batch.assert_awaited_once_with([7], "sess", "closed")
    assert result == [7]


def test_write_plan_precondition_requires_open_issue() -> None:
    assert WriteImplementationPlanPlay().preconditions(_state(issues=[])) != []


def test_write_plan_precondition_met() -> None:
    assert WriteImplementationPlanPlay().preconditions(_state(issues=[_issue()])) == []


def test_write_plan_blocked_when_all_issues_covered_by_open_prs() -> None:
    pr = PullRequestSnapshot(
        pr_number=10,
        title="covers issue 1",
        state="open",
        branch=None,
        issue_number=1,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
    )
    errs = WriteImplementationPlanPlay().preconditions(
        _state(issues=[_issue(num=1)], pull_requests=[pr])
    )
    assert any("covered by open PR" in e.text or "eligible issue" in e.text for e in errs)


def test_write_plan_blocked_when_only_in_flight() -> None:
    state = _state(issues=[_issue(num=1)])
    state.in_flight_issues = [1]
    errs = WriteImplementationPlanPlay().preconditions(state)
    assert any("eligible issue" in e.text for e in errs)


def test_write_plan_unblocked_when_eligible_issue_remains() -> None:
    pr = PullRequestSnapshot(
        pr_number=10,
        title="covers issue 1",
        state="open",
        branch=None,
        issue_number=1,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
    )
    # Issue 1 is covered, but issue 2 is free — precondition should pass.
    errs = WriteImplementationPlanPlay().preconditions(
        _state(issues=[_issue(num=1), _issue(num=2)], pull_requests=[pr])
    )
    assert errs == []


def test_systematic_debugging_requires_failure_signal() -> None:
    errors = SystematicDebuggingPlay().preconditions(_state(issues=[_issue()]))
    assert [e.text for e in errors] == [
        "no explicit QA/debug issue available (all in-flight, PR-linked, or none exist)"
    ]


def test_systematic_debugging_precondition_met_with_qa_issue() -> None:
    assert (
        SystematicDebuggingPlay().preconditions(_state(issues=[_issue(labels=["agentshore/qa"])]))
        == []
    )


def test_systematic_debugging_precondition_met_with_debug_needed_issue() -> None:
    assert (
        SystematicDebuggingPlay().preconditions(
            _state(issues=[_issue(labels=["agentshore/debug-needed"])])
        )
        == []
    )


def test_systematic_debugging_ignores_review_bug_and_root_cause_found() -> None:
    errors = SystematicDebuggingPlay().preconditions(
        _state(
            issues=[
                _issue(num=1, labels=["agentshore/review"]),
                _issue(num=2, labels=["bug"]),
                _issue(num=3, labels=["agentshore/qa", "agentshore/root-cause-found"]),
            ]
        )
    )

    assert errors


def test_systematic_debugging_ignores_issue_linked_to_open_pr() -> None:
    errors = SystematicDebuggingPlay().preconditions(
        _state(
            issues=[_issue(num=7, labels=["agentshore/qa"])],
            pull_requests=[_pr(num=17, issue_number=7)],
        )
    )

    assert errors


# ---------------------------------------------------------------------------
# IssuePickupPlay
# ---------------------------------------------------------------------------


def test_issue_pickup_precondition_met_with_issue_and_implementer() -> None:
    assert IssuePickupPlay().preconditions(_state(agents=[_snap()], issues=[_issue()])) == []


def test_issue_pickup_blocked_when_only_issue_is_disallowed() -> None:
    errors = IssuePickupPlay().preconditions(
        _state(agents=[_snap()], issues=[_issue(labels=["agentshore/disallowed"])])
    )

    assert any("issue-label gates" in e.text for e in errors)


def test_issue_pickup_blocked_when_open_pr_queue_full() -> None:
    """Backpressure: when the open-PR queue exceeds the threshold, mask
    issue_pickup so the policy clears review/merge work first instead of
    piling on more PRs that won't drain.
    """
    prs = [_pr(num=i, state="open") for i in range(1, 12)]  # 11 open PRs
    errors = IssuePickupPlay().preconditions(
        _state(agents=[_snap()], issues=[_issue()], pull_requests=prs)
    )
    assert any("too many open PRs" in e.text for e in errors)
    assert any("11" in e.text for e in errors)


def test_issue_pickup_blocked_at_threshold() -> None:
    """Exactly 10 open PRs is the first blocked count (inclusive gate)."""
    prs = [_pr(num=i, state="open") for i in range(1, 11)]  # 10 open PRs
    errors = IssuePickupPlay().preconditions(
        _state(agents=[_snap()], issues=[_issue()], pull_requests=prs)
    )
    assert any("too many open PRs" in e.text for e in errors)
    assert any("10" in e.text for e in errors)


def test_issue_pickup_allowed_below_threshold() -> None:
    """9 open PRs is below the threshold and is allowed."""
    prs = [_pr(num=i, state="open") for i in range(1, 10)]  # 9 open PRs
    errors = IssuePickupPlay().preconditions(
        _state(agents=[_snap()], issues=[_issue()], pull_requests=prs)
    )
    assert errors == []


def test_issue_pickup_ignores_closed_prs_for_queue_count() -> None:
    """Only PRs in state='open' count toward the queue threshold —
    closed/merged PRs were drained already and shouldn't gate new pickup.
    """
    prs = [_pr(num=i, state="closed") for i in range(1, 12)]  # 11 closed PRs
    errors = IssuePickupPlay().preconditions(
        _state(agents=[_snap()], issues=[_issue()], pull_requests=prs)
    )
    assert errors == []


# ---------------------------------------------------------------------------
# CodeReviewPlay
# ---------------------------------------------------------------------------


def test_code_review_precondition_no_pending_reviews() -> None:
    assert CodeReviewPlay().preconditions(_state()) != []
    assert "no pending reviews" in CodeReviewPlay().preconditions(_state())[0].text


def test_code_review_precondition_no_stale_or_unreviewed_prs() -> None:
    errors = CodeReviewPlay().preconditions(
        _state(
            agents=[_snap()],
            pull_requests=[
                _pr(
                    num=7,
                    head_sha="abc",
                    last_reviewed_sha="abc",
                    last_review_status="PASS",
                )
            ],
        )
    )

    assert errors


def test_code_review_precondition_no_idle_reviewer() -> None:
    agents = [_snap(status=AgentStatus.BUSY)]
    assert (
        CodeReviewPlay().preconditions(
            _state(agents=agents, pending_review_queue=[_pending_review()])
        )
        != []
    )


def test_code_review_precondition_met_with_idle_claude() -> None:
    assert (
        CodeReviewPlay().preconditions(
            _state(agents=[_snap()], pending_review_queue=[_pending_review()])
        )
        == []
    )


def test_needs_review_false_at_reviewed_head() -> None:
    pr = _pr(head_sha="abc123", last_reviewed_sha="abc123", last_review_status="PASS")
    assert needs_review(pr) is False


def test_needs_review_after_head_advances() -> None:
    pr = _pr(head_sha="new456", last_reviewed_sha="old123", last_review_status="PASS")
    assert needs_review(pr) is True


def test_needs_review_false_when_already_approved_on_github() -> None:
    """GH APPROVED short-circuits even when AgentShore has no prior review.

    Without this, anti-confirmation masks code_review entirely when every
    open PR is APPROVED on GitHub but AgentShore's last_reviewed_sha is
    still None (observed 2026-05-28 session 08a948ed).
    """
    pr = _pr(review_decision="APPROVED", head_sha="abc123")
    assert needs_review(pr) is False


def test_needs_review_true_after_approval_when_head_advances() -> None:
    """A new commit pushed after GH approval invalidates the approval."""
    pr = _pr(
        review_decision="APPROVED",
        head_sha="new456",
        last_reviewed_sha="old123",
    )
    assert needs_review(pr) is True


def test_needs_review_true_when_review_decision_is_changes_requested() -> None:
    """CHANGES_REQUESTED never short-circuits."""
    pr = _pr(review_decision="CHANGES_REQUESTED", head_sha="abc123")
    assert needs_review(pr) is True


@pytest.mark.asyncio
async def test_code_review_writes_last_reviewed_sha_on_success() -> None:
    """On a successful review, execute() must persist the reviewed SHA."""
    from unittest.mock import patch

    from agentshore.plays.base import PlayParams
    from agentshore.plays.skill_backed.base import SkillBackedPlay
    from agentshore.state import PlayOutcome, PlayType

    skill_outcome = PlayOutcome(
        play_type=PlayType.CODE_REVIEW,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=2.0,
        token_cost=200,
        dollar_cost=0.05,
        artifacts=[{"type": "pr", "number": 42, "head_sha": "abc123", "status": "PASS"}],
        alignment_delta=0.0,
        error=None,
    )

    play = CodeReviewPlay()
    ctx = _ctx()
    ctx.store.update_pr_last_reviewed_sha = AsyncMock()
    with patch.object(SkillBackedPlay, "execute", AsyncMock(return_value=skill_outcome)):
        outcome = await play.execute(_state(), PlayParams(pr_number=42), ctx=ctx)

    assert outcome.success is True
    # status=None because the patched base.execute() doesn't populate
    # _last_skill_result; this test isolates the SHA-persistence path.
    ctx.store.update_pr_last_reviewed_sha.assert_awaited_once_with(
        42, "sess", "abc123", status=None
    )


@pytest.mark.asyncio
async def test_code_review_skip_zero_diff_persists_sha_via_state_fallback() -> None:
    """SKIP / zero-diff success blocks historically omit a `type=pr` artifact
    and put `head_sha` at the top level, which the parser drops. The fallback
    must read state.pull_requests by params.pr_number and persist the SHA so
    the precondition masks the PR next cycle. Without it, the resolver
    re-picks the same PR forever — the alternation pattern from session
    0712bbfb (zero-diff success ↔ "needs different reviewer" on the same PR).
    """
    from unittest.mock import patch

    from agentshore.plays.base import PlayParams
    from agentshore.plays.skill_backed.base import SkillBackedPlay
    from agentshore.state import PlayOutcome, PlayType

    skill_outcome = PlayOutcome(
        play_type=PlayType.CODE_REVIEW,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=10,
        dollar_cost=0.0,
        artifacts=[],  # parser drops top-level head_sha — no artifact carries it
        alignment_delta=0.0,
        error="zero-diff PR; no review needed",
    )

    pr = _pr(num=42, head_sha="cafebabecafebabecafebabecafebabecafebabe")
    state = _state(pull_requests=[pr])

    play = CodeReviewPlay()
    ctx = _ctx()
    ctx.store.update_pr_last_reviewed_sha = AsyncMock()
    with patch.object(SkillBackedPlay, "execute", AsyncMock(return_value=skill_outcome)):
        outcome = await play.execute(state, PlayParams(pr_number=42), ctx=ctx)

    assert outcome.success is True
    ctx.store.update_pr_last_reviewed_sha.assert_awaited_once_with(
        42, "sess", "cafebabecafebabecafebabecafebabecafebabe", status=None
    )


@pytest.mark.asyncio
async def test_code_review_already_reviewed_routes_to_next_step() -> None:
    """Skill `success=true error="already reviewed at <sha>"` persists the SHA
    and returns a partial success routing the PR to merge or unblock based on
    the prior verdict."""
    from unittest.mock import patch

    from agentshore.plays.base import PlayParams
    from agentshore.plays.skill_backed.base import SkillBackedPlay
    from agentshore.state import PlayOutcome, PlayType

    skill_outcome = PlayOutcome(
        play_type=PlayType.CODE_REVIEW,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=10,
        dollar_cost=0.01,
        artifacts=[],
        alignment_delta=0.0,
        error="already reviewed at abc123",
    )

    play = CodeReviewPlay()
    ctx = _ctx()
    ctx.store.update_pr_last_reviewed_sha = AsyncMock()
    with patch.object(SkillBackedPlay, "execute", AsyncMock(return_value=skill_outcome)):
        outcome = await play.execute(_state(), PlayParams(pr_number=42), ctx=ctx)

    assert outcome.success is True
    assert outcome.partial is True
    assert "already reviewed" in (outcome.error or "")
    assert "unblock" in (outcome.error or "")
    ctx.store.update_pr_last_reviewed_sha.assert_awaited_once_with(
        42, "sess", "abc123", status=None
    )


@pytest.mark.asyncio
async def test_code_review_genuine_success_passes_through() -> None:
    from unittest.mock import patch

    from agentshore.plays.base import PlayParams
    from agentshore.plays.skill_backed.base import SkillBackedPlay
    from agentshore.state import PlayOutcome, PlayType

    skill_outcome = PlayOutcome(
        play_type=PlayType.CODE_REVIEW,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=2.0,
        token_cost=200,
        dollar_cost=0.05,
        artifacts=[{"type": "review", "pr": 42, "verdict": "APPROVE"}],
        alignment_delta=0.0,
        error=None,
    )

    play = CodeReviewPlay()
    with patch.object(SkillBackedPlay, "execute", AsyncMock(return_value=skill_outcome)):
        outcome = await play.execute(_state(), PlayParams(pr_number=42), ctx=_ctx())

    assert outcome.success is True
    assert outcome.partial is False
    assert outcome.error is None


@pytest.mark.asyncio
async def test_code_review_persists_pass_verdict() -> None:
    """A PASS skill result with zero blocking findings must persist
    `status='PASS'` alongside the SHA so merge_pr can gate on it."""
    from unittest.mock import patch

    from agentshore.plays.base import PlayParams
    from agentshore.plays.skill_backed.base import SkillBackedPlay
    from agentshore.state import PlayOutcome, PlayType, SkillResult

    skill_outcome = PlayOutcome(
        play_type=PlayType.CODE_REVIEW,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=2.0,
        token_cost=200,
        dollar_cost=0.05,
        artifacts=[{"type": "pr", "number": 42, "head_sha": "abc123"}],
        alignment_delta=0.0,
        error=None,
    )

    play = CodeReviewPlay()
    play._last_skill_result = SkillResult(success=True, spec_compliance="PASS", blocking_findings=0)
    ctx = _ctx()
    ctx.store.update_pr_last_reviewed_sha = AsyncMock()
    with patch.object(SkillBackedPlay, "execute", AsyncMock(return_value=skill_outcome)):
        outcome = await play.execute(_state(), PlayParams(pr_number=42), ctx=ctx)

    assert outcome.success is True
    ctx.store.update_pr_last_reviewed_sha.assert_awaited_once_with(
        42, "sess", "abc123", status="PASS"
    )


@pytest.mark.asyncio
async def test_code_review_persists_block_verdict() -> None:
    """A BLOCK skill result must persist `status='BLOCK'` so merge_pr is
    excluded from this PR even though `last_reviewed_sha == head_sha`."""
    from unittest.mock import patch

    from agentshore.plays.base import PlayParams
    from agentshore.plays.skill_backed.base import SkillBackedPlay
    from agentshore.state import PlayOutcome, PlayType, SkillResult

    skill_outcome = PlayOutcome(
        play_type=PlayType.CODE_REVIEW,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=2.0,
        token_cost=200,
        dollar_cost=0.05,
        artifacts=[{"type": "pr", "number": 42, "head_sha": "abc123"}],
        alignment_delta=0.0,
        error=None,
    )

    play = CodeReviewPlay()
    play._last_skill_result = SkillResult(
        success=True, spec_compliance="BLOCK", blocking_findings=3
    )
    ctx = _ctx()
    ctx.store.update_pr_last_reviewed_sha = AsyncMock()
    with patch.object(SkillBackedPlay, "execute", AsyncMock(return_value=skill_outcome)):
        await play.execute(_state(), PlayParams(pr_number=42), ctx=ctx)

    ctx.store.update_pr_last_reviewed_sha.assert_awaited_once_with(
        42, "sess", "abc123", status="BLOCK"
    )


@pytest.mark.asyncio
async def test_code_review_pass_with_blocking_findings_is_block() -> None:
    """spec_compliance=PASS but blocking_findings>0 must still persist as BLOCK.

    The skill template treats blocking findings as a hard gate; the verdict
    mapping must mirror that — never claim PASS when there are any blocking
    findings, regardless of the headline `spec_compliance` field.
    """
    from unittest.mock import patch

    from agentshore.plays.base import PlayParams
    from agentshore.plays.skill_backed.base import SkillBackedPlay
    from agentshore.state import PlayOutcome, PlayType, SkillResult

    skill_outcome = PlayOutcome(
        play_type=PlayType.CODE_REVIEW,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=2.0,
        token_cost=200,
        dollar_cost=0.05,
        artifacts=[{"type": "pr", "number": 42, "head_sha": "abc123"}],
        alignment_delta=0.0,
        error=None,
    )

    play = CodeReviewPlay()
    play._last_skill_result = SkillResult(success=True, spec_compliance="PASS", blocking_findings=2)
    ctx = _ctx()
    ctx.store.update_pr_last_reviewed_sha = AsyncMock()
    with patch.object(SkillBackedPlay, "execute", AsyncMock(return_value=skill_outcome)):
        await play.execute(_state(), PlayParams(pr_number=42), ctx=ctx)

    ctx.store.update_pr_last_reviewed_sha.assert_awaited_once_with(
        42, "sess", "abc123", status="BLOCK"
    )


@pytest.mark.asyncio
async def test_code_review_skip_does_not_overwrite_status() -> None:
    """SKIP path persists the SHA only — status=None — so a previously-
    recorded PASS/BLOCK verdict on the PR remains authoritative."""
    from unittest.mock import patch

    from agentshore.plays.base import PlayParams
    from agentshore.plays.skill_backed.base import SkillBackedPlay
    from agentshore.state import PlayOutcome, PlayType, SkillResult

    skill_outcome = PlayOutcome(
        play_type=PlayType.CODE_REVIEW,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=10,
        dollar_cost=0.0,
        artifacts=[{"type": "pr", "number": 42, "head_sha": "abc123"}],
        alignment_delta=0.0,
        error="zero-diff PR; no review needed",
    )

    play = CodeReviewPlay()
    play._last_skill_result = SkillResult(success=True, spec_compliance="SKIP")
    ctx = _ctx()
    ctx.store.update_pr_last_reviewed_sha = AsyncMock()
    with patch.object(SkillBackedPlay, "execute", AsyncMock(return_value=skill_outcome)):
        await play.execute(_state(), PlayParams(pr_number=42), ctx=ctx)

    ctx.store.update_pr_last_reviewed_sha.assert_awaited_once_with(
        42, "sess", "abc123", status=None
    )


@pytest.mark.asyncio
async def test_code_review_skip_backfills_status_from_prior_pass() -> None:
    """Dedup short-circuit must persist last_review_status when the skill
    surfaces prior_verdict=PASS from an existing AGENTSHORE_CODE_REVIEW comment.

    Without this, PRs whose first review crashed mid-run or filed as
    COMMENTED leave last_review_status NULL forever and never become
    merge_pr-eligible.
    """
    from unittest.mock import patch

    from agentshore.plays.base import PlayParams
    from agentshore.plays.skill_backed.base import SkillBackedPlay
    from agentshore.state import PlayOutcome, PlayType, SkillResult

    skill_outcome = PlayOutcome(
        play_type=PlayType.CODE_REVIEW,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=10,
        dollar_cost=0.01,
        artifacts=[],
        alignment_delta=0.0,
        error="already reviewed at abc123",
    )

    play = CodeReviewPlay()
    play._last_skill_result = SkillResult(
        success=True, prior_verdict="PASS", prior_blocking_findings=0
    )
    ctx = _ctx()
    ctx.store.update_pr_last_reviewed_sha = AsyncMock()
    with patch.object(SkillBackedPlay, "execute", AsyncMock(return_value=skill_outcome)):
        outcome = await play.execute(_state(), PlayParams(pr_number=42), ctx=ctx)

    assert outcome.success is True
    assert outcome.partial is True
    assert "merge" in (outcome.error or "")
    ctx.store.update_pr_last_reviewed_sha.assert_awaited_once_with(
        42, "sess", "abc123", status="PASS"
    )


@pytest.mark.asyncio
async def test_code_review_skip_backfills_status_from_prior_block() -> None:
    """Dedup with prior BLOCK verdict (or PASS+blocking_findings>0) must
    persist BLOCK so unblock_pr can route the PR for fixes."""
    from unittest.mock import patch

    from agentshore.plays.base import PlayParams
    from agentshore.plays.skill_backed.base import SkillBackedPlay
    from agentshore.state import PlayOutcome, PlayType, SkillResult

    skill_outcome = PlayOutcome(
        play_type=PlayType.CODE_REVIEW,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=10,
        dollar_cost=0.01,
        artifacts=[],
        alignment_delta=0.0,
        error="already reviewed at abc123",
    )

    play = CodeReviewPlay()
    play._last_skill_result = SkillResult(
        success=True, prior_verdict="BLOCK", prior_blocking_findings=2
    )
    ctx = _ctx()
    ctx.store.update_pr_last_reviewed_sha = AsyncMock()
    with patch.object(SkillBackedPlay, "execute", AsyncMock(return_value=skill_outcome)):
        await play.execute(_state(), PlayParams(pr_number=42), ctx=ctx)

    ctx.store.update_pr_last_reviewed_sha.assert_awaited_once_with(
        42, "sess", "abc123", status="BLOCK"
    )


@pytest.mark.asyncio
async def test_code_review_skip_prior_pass_with_blocking_findings_is_block() -> None:
    """prior_verdict=PASS but prior_blocking_findings>0 must downgrade to
    BLOCK — mirrors the fresh-review _verdict() rule that any blocking
    finding overrides a headline PASS."""
    from unittest.mock import patch

    from agentshore.plays.base import PlayParams
    from agentshore.plays.skill_backed.base import SkillBackedPlay
    from agentshore.state import PlayOutcome, PlayType, SkillResult

    skill_outcome = PlayOutcome(
        play_type=PlayType.CODE_REVIEW,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=10,
        dollar_cost=0.01,
        artifacts=[],
        alignment_delta=0.0,
        error="already reviewed at abc123",
    )

    play = CodeReviewPlay()
    play._last_skill_result = SkillResult(
        success=True, prior_verdict="PASS", prior_blocking_findings=3
    )
    ctx = _ctx()
    ctx.store.update_pr_last_reviewed_sha = AsyncMock()
    with patch.object(SkillBackedPlay, "execute", AsyncMock(return_value=skill_outcome)):
        await play.execute(_state(), PlayParams(pr_number=42), ctx=ctx)

    ctx.store.update_pr_last_reviewed_sha.assert_awaited_once_with(
        42, "sess", "abc123", status="BLOCK"
    )


@pytest.mark.asyncio
async def test_code_review_skip_no_backfill_when_prior_unparseable() -> None:
    """Skill that can't parse the prior comment must omit prior_verdict;
    play falls back to preserving any existing last_review_status (status=None)."""
    from unittest.mock import patch

    from agentshore.plays.base import PlayParams
    from agentshore.plays.skill_backed.base import SkillBackedPlay
    from agentshore.state import PlayOutcome, PlayType, SkillResult

    skill_outcome = PlayOutcome(
        play_type=PlayType.CODE_REVIEW,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=10,
        dollar_cost=0.01,
        artifacts=[],
        alignment_delta=0.0,
        error="already reviewed at abc123",
    )

    play = CodeReviewPlay()
    play._last_skill_result = SkillResult(success=True)  # no prior_verdict
    ctx = _ctx()
    ctx.store.update_pr_last_reviewed_sha = AsyncMock()
    with patch.object(SkillBackedPlay, "execute", AsyncMock(return_value=skill_outcome)):
        await play.execute(_state(), PlayParams(pr_number=42), ctx=ctx)

    ctx.store.update_pr_last_reviewed_sha.assert_awaited_once_with(
        42, "sess", "abc123", status=None
    )


@pytest.mark.asyncio
async def test_code_review_persists_review_patterns_from_skill_result() -> None:
    from unittest.mock import patch

    from agentshore.plays.base import PlayParams
    from agentshore.plays.skill_backed.base import SkillBackedPlay
    from agentshore.state import PlayOutcome, PlayType, SkillResult

    skill_outcome = PlayOutcome(
        play_type=PlayType.CODE_REVIEW,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=10,
        dollar_cost=0.01,
        artifacts=[],
        alignment_delta=0.0,
        error=None,
    )

    play = CodeReviewPlay()
    play._last_skill_result = SkillResult(
        success=True,
        review_patterns=[
            {"pattern": "missing regression test", "category": "testing", "frequency": 2},
            {"pattern": "tighten type annotations", "category": "typing", "frequency": 1},
        ],
    )
    ctx = _ctx()
    ctx.store.record_review_patterns = AsyncMock()
    ctx.store.update_pr_last_reviewed_sha = AsyncMock()
    with patch.object(SkillBackedPlay, "execute", AsyncMock(return_value=skill_outcome)):
        outcome = await play.execute(_state(), PlayParams(pr_number=42), ctx=ctx)

    assert outcome.success is True
    assert ctx.store.record_review_patterns.await_count == 1
    persisted = ctx.store.record_review_patterns.await_args.args[0]
    assert len(persisted) == 2


@pytest.mark.asyncio
async def test_code_review_bool_frequency_is_retained_as_one() -> None:
    from unittest.mock import patch

    from agentshore.data.models import ReviewFeedbackPatternRecord
    from agentshore.plays.base import PlayParams
    from agentshore.plays.skill_backed.base import SkillBackedPlay
    from agentshore.state import PlayOutcome, PlayType, SkillResult

    skill_outcome = PlayOutcome(
        play_type=PlayType.CODE_REVIEW,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=10,
        dollar_cost=0.01,
        artifacts=[],
        alignment_delta=0.0,
        error=None,
    )

    play = CodeReviewPlay()
    play._last_skill_result = SkillResult(
        success=True,
        review_patterns=[
            {"pattern": "add regression test", "category": "testing", "frequency": True},
            {"pattern": "cover edge-case parsing", "category": "testing", "frequency": False},
        ],
    )
    ctx = _ctx()
    captured: list[ReviewFeedbackPatternRecord] = []

    async def _capture(records: list[ReviewFeedbackPatternRecord]) -> None:
        captured.extend(records)

    ctx.store.record_review_patterns = AsyncMock(side_effect=_capture)
    ctx.store.update_pr_last_reviewed_sha = AsyncMock()
    with patch.object(SkillBackedPlay, "execute", AsyncMock(return_value=skill_outcome)):
        outcome = await play.execute(_state(), PlayParams(pr_number=42), ctx=ctx)

    assert outcome.success is True
    assert len(captured) == 2
    assert {(r.pattern, r.frequency) for r in captured} == {
        ("add regression test", 1),
        ("cover edge-case parsing", 1),
    }


@pytest.mark.asyncio
async def test_issue_pickup_injects_review_patterns_into_context_payload() -> None:
    from unittest.mock import patch

    from agentshore.state import SkillResult

    state = _state(issues=[_issue(num=1)])
    ctx = _ctx_for_execute()
    ctx.store.list_review_patterns = AsyncMock(
        return_value=[
            MagicMock(
                pattern_id=11,
                pattern="missing regression test",
                category="testing",
                frequency=3,
            )
        ]
    )
    ctx.store.mark_review_patterns_injected = AsyncMock()
    captured_payloads: list[dict[str, object]] = []

    def _capture_payload(*_args: object, **kwargs: object) -> dict[str, object]:
        payload = {"review_patterns": [{"pattern": "missing regression test"}]}
        payload.update(kwargs.get("extra", {}))
        captured_payloads.append(kwargs)  # type: ignore[arg-type]
        return payload

    with (
        patch(
            "agentshore.plays.skill_backed.base.asyncio.to_thread",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "agentshore.plays.skill_backed.base.parse_skill_result",
            return_value=SkillResult(success=True, artifacts=[]),
        ),
        patch(
            "agentshore.plays.skill_backed.base.render_skill_prompt",
            return_value="<prompt>",
        ),
        patch(
            "agentshore.plays.skill_backed.base.serialize_state_for_skill",
            side_effect=_capture_payload,
        ),
    ):
        outcome = await IssuePickupPlay().execute(
            state,
            PlayParams(agent_id="a1", issue_number=1),
            ctx=ctx,
        )

    assert outcome.success is True
    assert captured_payloads
    extra = captured_payloads[0]["extra"]
    assert isinstance(extra, dict)
    assert extra["review_patterns"] == [
        {"pattern": "missing regression test", "category": "testing", "frequency": 3}
    ]
    ctx.store.mark_review_patterns_injected.assert_awaited_once_with("sess", [11])


# ---------------------------------------------------------------------------
# RunQAPlay
# ---------------------------------------------------------------------------


def test_run_qa_precondition_no_idle_tester() -> None:
    agents = [_snap(status=AgentStatus.BUSY)]
    assert RunQAPlay().preconditions(_state(agents=agents)) != []


def test_run_qa_precondition_met() -> None:
    # Simulate a session past the first-run floor with a prior QA run recorded.
    state = _state(
        agents=[_snap()],
        plays_since_last_play_type={PlayType.RUN_QA: 42},
        total_plays=46,
    )
    assert RunQAPlay().preconditions(state) == []


def test_run_qa_blocks_before_min_plays() -> None:
    errors = RunQAPlay().preconditions(_state(agents=[_snap()], total_plays=6))
    assert any(r.text == "warmup floor (6/20 plays)" for r in errors)


def test_run_qa_blocks_in_flight() -> None:
    errors = RunQAPlay().preconditions(
        _state(agents=[_snap()], in_flight_plays=[PlayType.RUN_QA], total_plays=30)
    )
    assert any(r.text == "run_qa already in flight" for r in errors)


def test_run_qa_blocks_during_cooldown() -> None:
    errors = RunQAPlay().preconditions(
        _state(agents=[_snap()], plays_since_last_play_type={PlayType.RUN_QA: 41})
    )
    assert [e.text for e in errors] == ["run_qa cooldown (41/42 plays since last)"]


def test_run_qa_allows_after_cooldown() -> None:
    assert (
        RunQAPlay().preconditions(
            _state(agents=[_snap()], plays_since_last_play_type={PlayType.RUN_QA: 42})
        )
        == []
    )


# ---------------------------------------------------------------------------
# MergePRPlay
# ---------------------------------------------------------------------------


_MERGE_PR_BLOCK_ERR = (
    "no PR with GitHub or AgentShore approval at current head_sha "
    "and mergeable=MERGEABLE (awaiting review or CI)"
)


def test_merge_pr_precondition_met_with_codex() -> None:
    agents = [_snap(agent_type=AgentType.CODEX)]
    prs = [_pr(review_decision="APPROVED", mergeable="MERGEABLE")]
    assert MergePRPlay().preconditions(_state(agents=agents, pull_requests=prs)) == []


def test_merge_pr_precondition_met_with_claude() -> None:
    prs = [_pr(review_decision="APPROVED", mergeable="MERGEABLE")]
    assert MergePRPlay().preconditions(_state(agents=[_snap()], pull_requests=prs)) == []


def test_merge_pr_blocked_when_no_mergeable_pr() -> None:
    # Approved PRs exist but none are MERGEABLE (CI pending or merge conflicts)
    prs = [
        _pr(review_decision="APPROVED", mergeable="UNKNOWN"),
        _pr(num=2, review_decision="APPROVED", mergeable="CONFLICTING"),
        _pr(num=3, review_decision="APPROVED"),
    ]
    errors = MergePRPlay().preconditions(_state(agents=[_snap()], pull_requests=prs))
    assert [e.text for e in errors] == [_MERGE_PR_BLOCK_ERR]


def test_merge_pr_blocked_when_pr_mergeable_but_not_approved() -> None:
    # Regression: precondition must require approval from at least one source
    # (GitHub APPROVED OR AgentShore PASS@head); an unapproved+mergeable PR
    # shouldn't satisfy.
    prs = [_pr(state="open", mergeable="MERGEABLE")]
    errors = MergePRPlay().preconditions(_state(agents=[_snap()], pull_requests=prs))
    assert [e.text for e in errors] == [_MERGE_PR_BLOCK_ERR]


def test_merge_pr_blocked_when_no_prs() -> None:
    errors = MergePRPlay().preconditions(_state(agents=[_snap()], pull_requests=[]))
    assert [e.text for e in errors] == [_MERGE_PR_BLOCK_ERR]


def test_merge_pr_precondition_met_via_agentshore_internal_pass() -> None:
    # Single-user setup: GitHub blocks self-approval so review_decision is None,
    # but AgentShore's own code_review play returned PASS at the current head SHA.
    # That must be sufficient for merge_pr.
    prs = [
        _pr(
            mergeable="MERGEABLE",
            review_decision=None,
            head_sha="abc123",
            last_reviewed_sha="abc123",
            last_review_status="PASS",
        )
    ]
    assert MergePRPlay().preconditions(_state(agents=[_snap()], pull_requests=prs)) == []


def test_merge_pr_blocked_when_agentshore_pass_is_stale() -> None:
    # AgentShore approved an older SHA, but a new commit has since been pushed.
    # The SHA mismatch must invalidate the approval automatically.
    prs = [
        _pr(
            mergeable="MERGEABLE",
            review_decision=None,
            head_sha="def456",
            last_reviewed_sha="abc123",
            last_review_status="PASS",
        )
    ]
    errors = MergePRPlay().preconditions(_state(agents=[_snap()], pull_requests=prs))
    assert [e.text for e in errors] == [_MERGE_PR_BLOCK_ERR]


def test_merge_pr_blocked_when_agentshore_block_at_current_head() -> None:
    # AgentShore returned BLOCK at the current head — must not be merge-eligible.
    prs = [
        _pr(
            mergeable="MERGEABLE",
            review_decision=None,
            head_sha="abc123",
            last_reviewed_sha="abc123",
            last_review_status="BLOCK",
        )
    ]
    errors = MergePRPlay().preconditions(_state(agents=[_snap()], pull_requests=prs))
    assert [e.text for e in errors] == [_MERGE_PR_BLOCK_ERR]


# ---------------------------------------------------------------------------
# RefineTaskBreakdownPlay
# ---------------------------------------------------------------------------


def test_refine_tasks_requires_open_issues() -> None:
    assert RefineTaskBreakdownPlay().preconditions(_state(issues=[])) != []


def test_refine_tasks_precondition_met() -> None:
    """At least one open issue must carry agentshore/needs-refinement."""
    assert (
        RefineTaskBreakdownPlay().preconditions(
            _state(issues=[_issue(labels=["agentshore/needs-refinement"])])
        )
        == []
    )


def test_refine_tasks_masked_when_no_issue_needs_refinement() -> None:
    """Open issues without the gate label do not unmask the play."""
    assert (
        RefineTaskBreakdownPlay().preconditions(
            _state(issues=[_issue(labels=["priority/medium", "size/s"])])
        )
        != []
    )


# ---------------------------------------------------------------------------
# CleanupPlay
# (see tests/test_plays_cleanup.py for full precondition coverage)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# execute() — parametrized happy-path and failure-path for base-class plays
# ---------------------------------------------------------------------------


_RAW_OK = '{"success": true, "artifacts": ["done"]}'
_RAW_FAIL = '{"success": false, "error": "boom"}'


def _canned_invocation(success: bool = True) -> AgentInvocationResult:
    return AgentInvocationResult(
        raw_output=_RAW_OK if success else _RAW_FAIL,
        tokens_in=100,
        tokens_out=50,
        dollar_cost=0.005,
        duration_ms=1200,
        exit_code=0,
    )


def _ctx_for_execute() -> MagicMock:
    ctx = _ctx()
    ctx.cfg.learnings.inject_into_prompts = False
    ctx.cfg.learnings.enabled = False
    ctx.store.get_play_history = AsyncMock(return_value=[])
    ctx.store.complete_reviews_for_pr = AsyncMock()
    return ctx


# Plays that use the base-class execute() without override (or trivial override)
_BASE_PLAYS: list[tuple[str, object, PlayParams]] = [
    ("UnblockPr", UnblockPrPlay(), PlayParams(agent_id="a1")),
    ("CalibrateAlignment", CalibrateAlignmentPlay(), PlayParams(agent_id="a1")),
    ("DesignAudit", DesignAuditPlay(), PlayParams(agent_id="a1")),
    ("GroomBacklog", GroomBacklogPlay(), PlayParams(agent_id="a1")),
    ("SeedProject", SeedProjectPlay(), PlayParams(agent_id="a1")),
    ("IssuePickup", IssuePickupPlay(), PlayParams(agent_id="a1", issue_number=1)),
    ("RefineTaskBreakdown", RefineTaskBreakdownPlay(), PlayParams(agent_id="a1")),
    ("RunQA", RunQAPlay(), PlayParams(agent_id="a1")),
    ("SystematicDebugging", SystematicDebuggingPlay(), PlayParams(agent_id="a1")),
    ("WriteImplementationPlan", WriteImplementationPlanPlay(), PlayParams(agent_id="a1")),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,play,params", _BASE_PLAYS, ids=[p[0] for p in _BASE_PLAYS])
async def test_execute_happy_path(name: str, play: object, params: object) -> None:
    from unittest.mock import patch

    from agentshore.state import SkillResult

    ctx = _ctx_for_execute()
    ctx.manager.dispatch = AsyncMock(return_value=_canned_invocation(success=True))
    artifacts: list[object] = ["done"]
    if name == "SeedProject":
        artifacts.append(
            {
                "type": "seed_audit",
                "requirements_total": 1,
                "verified_requirements": 1,
                "represented_open_requirements": 0,
                "scope_gaps_found": 0,
                "unresolved_scope_gaps": 0,
                "unknown_requirements": 0,
                "scope_gap_issue_numbers": [],
            }
        )
    elif name == "DesignAudit":
        artifacts.append(
            {
                "type": "design_audit",
                "requirements_scanned": 1,
                "gaps_found": 0,
                "issues_created": 0,
                "issues_linked": 0,
                "unresolved_gaps": 0,
                "unknown_requirements": 0,
                "gap_issue_numbers": [],
            }
        )

    with (
        patch(
            "agentshore.plays.skill_backed.base.asyncio.to_thread",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "agentshore.plays.skill_backed.base.parse_skill_result",
            return_value=SkillResult(success=True, artifacts=artifacts),
        ),
        patch(
            "agentshore.plays.skill_backed.base.render_skill_prompt",
            return_value="<prompt>",
        ),
    ):
        outcome = await play.execute(_state(), params, ctx=ctx)  # type: ignore[attr-defined]

    assert outcome.success is True
    assert outcome.play_type == play.play_type  # type: ignore[attr-defined]
    assert outcome.agent_id == "a1"


@pytest.mark.asyncio
@pytest.mark.parametrize("name,play,params", _BASE_PLAYS, ids=[p[0] for p in _BASE_PLAYS])
async def test_execute_failure_path(name: str, play: object, params: object) -> None:
    from unittest.mock import patch

    from agentshore.state import SkillResult

    ctx = _ctx_for_execute()
    ctx.manager.dispatch = AsyncMock(return_value=_canned_invocation(success=False))

    with (
        patch(
            "agentshore.plays.skill_backed.base.asyncio.to_thread",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "agentshore.plays.skill_backed.base.parse_skill_result",
            return_value=SkillResult(success=False, error="boom"),
        ),
        patch(
            "agentshore.plays.skill_backed.base.render_skill_prompt",
            return_value="<prompt>",
        ),
    ):
        outcome = await play.execute(_state(), params, ctx=ctx)  # type: ignore[attr-defined]

    assert outcome.success is False
    assert outcome.agent_id == "a1"
    assert outcome.error == "boom"


@pytest.mark.asyncio
async def test_execute_marks_repo_access_failure_as_auth_error() -> None:
    from unittest.mock import patch

    from agentshore.state import SkillResult

    ctx = _ctx_for_execute()
    ctx.manager.dispatch = AsyncMock(return_value=_canned_invocation(success=False))
    ctx.manager.mark_agent_error = AsyncMock()
    error = (
        "GitHub preflight failed: authenticated identity matched assigned user, but "
        "repository/PR is not accessible with injected token. `gh pr view 101` and "
        "`gh api repos/example-user/example-repo` both returned not found/could not "
        "resolve repository."
    )

    with (
        patch(
            "agentshore.plays.skill_backed.base.asyncio.to_thread",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "agentshore.plays.skill_backed.base.parse_skill_result",
            return_value=SkillResult(success=False, error=error),
        ),
        patch(
            "agentshore.plays.skill_backed.base.render_skill_prompt",
            return_value="<prompt>",
        ),
    ):
        outcome = await IssuePickupPlay().execute(
            _state(),
            PlayParams(agent_id="a1", issue_number=1),
            ctx=ctx,
        )

    assert outcome.success is False
    ctx.manager.mark_agent_error.assert_awaited_once_with("a1", "auth", error)


@pytest.mark.asyncio
async def test_execute_marks_irrecoverable_github_access_failure_as_auth_error() -> None:
    from unittest.mock import patch

    from agentshore.state import SkillResult

    ctx = _ctx_for_execute()
    ctx.manager.dispatch = AsyncMock(return_value=_canned_invocation(success=False))
    ctx.manager.mark_agent_error = AsyncMock()
    error = (
        "Irrecoverable GitHub access failure: `gh pr view 116` and `gh pr view "
        "https://github.com/example-user/example-repo/pull/116` both failed with "
        "`GraphQL: Could not resolve to a Repository with the name "
        "'example-user/example-repo'. (repository)`. GitHub auth is active as "
        "`unseriousAI` (matching assigned identity `unseriousai` case-insensitively), "
        "but the repository is not resolvable to this token/session."
    )

    with (
        patch(
            "agentshore.plays.skill_backed.base.asyncio.to_thread",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "agentshore.plays.skill_backed.base.parse_skill_result",
            return_value=SkillResult(success=False, error=error),
        ),
        patch(
            "agentshore.plays.skill_backed.base.render_skill_prompt",
            return_value="<prompt>",
        ),
    ):
        outcome = await IssuePickupPlay().execute(
            _state(),
            PlayParams(agent_id="a1", issue_number=1),
            ctx=ctx,
        )

    assert outcome.success is False
    ctx.manager.mark_agent_error.assert_awaited_once_with("a1", "auth", error)


@pytest.mark.asyncio
async def test_execute_no_agent_id_returns_failure() -> None:
    from agentshore.plays.base import PlayParams

    play = IssuePickupPlay()
    ctx = _ctx_for_execute()
    outcome = await play.execute(_state(), PlayParams(agent_id=None), ctx=ctx)
    assert outcome.success is False
    assert outcome.agent_id is None


# ---------------------------------------------------------------------------
# MergePRPlay execute()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_pr_execute_dispatches_when_no_pending(tmp_path: object) -> None:
    from unittest.mock import patch

    from agentshore.state import SkillResult

    ctx = _ctx_for_execute()
    ctx.manager.dispatch = AsyncMock(return_value=_canned_invocation(success=True))
    ctx.store.mark_pr_merged = AsyncMock()

    with (
        patch(
            "agentshore.plays.skill_backed.base.asyncio.to_thread",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "agentshore.plays.skill_backed.base.parse_skill_result",
            return_value=SkillResult(success=True, artifacts=[]),
        ),
        patch(
            "agentshore.plays.skill_backed.base.render_skill_prompt",
            return_value="<prompt>",
        ),
        patch(
            "agentshore.plays.skill_backed._merge_reconcile.resolve_ff_fetch_overlay",
            return_value=None,
        ),
    ):
        outcome = await MergePRPlay().execute(
            _state(pull_requests=[_pr(mergeable="MERGEABLE")]),
            PlayParams(agent_id="a1", pr_number=1),
            ctx=ctx,
        )

    assert outcome.success is True
    # Post-merge cache write-through: the just-merged PR's row is marked
    # MERGED immediately so the resolver's next pick doesn't re-target it
    # for unblock_pr.
    ctx.store.mark_pr_merged.assert_awaited_once_with(1, "sess")


@pytest.mark.asyncio
async def test_merge_pr_execute_does_not_mark_merged_on_skill_failure() -> None:
    """When the merge skill fails (e.g., gh CLI error, conflict surfaced
    too late), don't write MERGED to the cache — the PR is still open."""
    from unittest.mock import patch

    from agentshore.state import SkillResult

    ctx = _ctx_for_execute()
    ctx.manager.dispatch = AsyncMock(return_value=_canned_invocation(success=False))
    ctx.store.mark_pr_merged = AsyncMock()

    with (
        patch(
            "agentshore.plays.skill_backed.base.asyncio.to_thread",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "agentshore.plays.skill_backed.base.parse_skill_result",
            return_value=SkillResult(success=False, error="merge failed"),
        ),
        patch(
            "agentshore.plays.skill_backed.base.render_skill_prompt",
            return_value="<prompt>",
        ),
    ):
        outcome = await MergePRPlay().execute(
            _state(pull_requests=[_pr(mergeable="MERGEABLE")]),
            PlayParams(agent_id="a1", pr_number=1),
            ctx=ctx,
        )

    assert outcome.success is False
    ctx.store.mark_pr_merged.assert_not_awaited()
