"""Tests for ParameterResolver — derives PlayParams from OrchestratorState."""

from __future__ import annotations

import dataclasses
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentshore.config import (
    AgentConfig,
    IntakeConfig,
    ModelTierConfig,
    RuntimeConfig,
    TrustedIdsConfig,
)
from agentshore.data.models import PullRequestRecord
from agentshore.errors import ErrorClass
from agentshore.plays.base import PlayParams
from agentshore.plays.resolver import ParameterResolver
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    BudgetSnapshot,
    IssueSnapshot,
    OrchestratorState,
    PlayType,
    PullRequestSnapshot,
    SessionState,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_snapshot(
    agent_id: str,
    agent_type: AgentType = AgentType.CLAUDE_CODE,
    status: AgentStatus = AgentStatus.IDLE,
    model_tier: str | None = None,
    context_size: int = 10_000,
    tasks_completed: int = 1,
    tasks_failed: int = 0,
    total_cost: float = 0.1,
    github_identity: str | None = None,
    last_error_class: ErrorClass | None = None,
    current_play_type: PlayType | None = None,
    current_play_pr_number: int | None = None,
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=agent_type,
        status=status,
        model_tier=model_tier,
        context_size=context_size,
        total_cost=total_cost,
        total_tokens=0,
        tasks_completed=tasks_completed,
        tasks_failed=tasks_failed,
        github_identity=github_identity,
        last_error_class=last_error_class,
        current_play_type=current_play_type,
        current_play_pr_number=current_play_pr_number,
    )


def _make_issue(
    num: int,
    *,
    state: str = "open",
    priority: int | None = None,
    labels: list[str] | None = None,
) -> IssueSnapshot:
    return IssueSnapshot(
        issue_number=num,
        title=f"Issue {num}",
        state=state,
        priority=priority,
        labels=labels or [],
        source=None,
    )


def _make_pr_snapshot(
    num: int,
    *,
    state: str = "open",
    issue_number: int | None = None,
    linked_issue_numbers: tuple[int, ...] = (),
) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        pr_number=num,
        title=f"PR {num}",
        state=state,
        branch=f"feature/{num}",
        issue_number=issue_number,
        linked_issue_numbers=linked_issue_numbers,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
    )


def _make_state(
    agents: list[AgentSnapshot] | None = None,
    issues: list[IssueSnapshot] | None = None,
    pull_requests: list[PullRequestSnapshot] | None = None,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="sess-test",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=agents or [],
        open_issues=issues or [],
        pull_requests=pull_requests or [],
        budget=BudgetSnapshot(
            total_budget=5.0, spent=1.0, remaining=4.0, estimated_cost_per_play=0.1
        ),
    )


def _make_cfg(
    *,
    seed_paths: list[str] | None = None,
    claude_max_context: int = 200_000,
    trusted_github_logins: tuple[str, ...] = (),
) -> RuntimeConfig:
    return RuntimeConfig(
        intake=IntakeConfig(seed_paths=seed_paths or []),
        trusted_ids=TrustedIdsConfig(github_logins=trusted_github_logins),
        agents={
            "claude_code": AgentConfig(enabled=True, max_context=claude_max_context),
            "codex": AgentConfig(enabled=True, max_context=192_000),
        },
    )


def _make_resolver(cfg: RuntimeConfig | None = None) -> ParameterResolver:
    store = AsyncMock()
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.list_pending_reviews = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    manager = MagicMock()
    return ParameterResolver(store=store, manager=manager, cfg=cfg or _make_cfg())


def _make_pr_record(
    num: int,
    *,
    state: str = "open",
    issue_number: int | None = None,
    github_author: str | None = "trusted",
    mergeable: str | None = "MERGEABLE",
    review_decision: str | None = None,
    head_sha: str | None = None,
    last_reviewed_sha: str | None = None,
    last_review_status: str | None = None,
) -> PullRequestRecord:
    return PullRequestRecord(
        pr_number=num,
        session_id="sess-test",
        state=state,
        created_at="2026-01-01T00:00:00Z",
        issue_number=issue_number,
        branch=f"feature/{num}",
        title=f"PR {num}",
        github_author=github_author,
        mergeable=mergeable,
        review_decision=review_decision,
        head_sha=head_sha,
        last_reviewed_sha=last_reviewed_sha,
        last_review_status=last_review_status,
    )


# ---------------------------------------------------------------------------
# Override validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_claims_target_without_revalidating_eligibility() -> None:
    """The resolver claims the override target; it no longer re-decides validity.

    Eligibility refactor: target validity (issue open + available) is owned by
    the EligibilityAuthority's confirm(), not the resolver. _resolve_override
    enumerates the named target and acquires the work claim, returning the
    params verbatim (or None only on a claim-CAS loss). A stale target is
    rejected upstream by confirm()'s live candidate-set check (clean re-pick),
    not by the resolver — see test_confirm_live_drift_is_clean_repick.
    """
    resolver = _make_resolver()
    override = PlayParams(issue_number=99)
    state = _make_state(issues=[_make_issue(1)])

    result = await resolver.resolve(PlayType.ISSUE_PICKUP, state, override=override)

    assert result is not None
    assert result.issue_number == 99


# ---------------------------------------------------------------------------
# UNBLOCK_PR
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_unblock_pr_returns_blocked_pr() -> None:
    store = AsyncMock()
    pr = MagicMock()
    pr.pr_number = 42
    pr.state = "changes_requested"
    pr.mergeable = "MERGEABLE"
    store.list_open_pull_requests = AsyncMock(return_value=[pr])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    result = await resolver.resolve(PlayType.UNBLOCK_PR, _make_state())

    assert result is not None
    assert result.pr_number == 42


@pytest.mark.asyncio
async def test_resolve_unblock_pr_uses_cached_review_metadata() -> None:
    store = AsyncMock()
    pr = MagicMock()
    pr.pr_number = 51
    pr.state = "open"
    pr.labels = []
    pr.review_decision = "CHANGES_REQUESTED"
    pr.status_check_summary = None
    pr.is_draft = False
    pr.mergeable = "MERGEABLE"
    store.list_open_pull_requests = AsyncMock(return_value=[pr])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    result = await resolver.resolve(PlayType.UNBLOCK_PR, _make_state())

    assert result is not None
    assert result.pr_number == 51


@pytest.mark.asyncio
async def test_resolve_unblock_pr_selects_conflicting_pr() -> None:
    store = AsyncMock()
    pr = MagicMock()
    pr.pr_number = 77
    pr.state = "open"
    pr.labels = []
    pr.review_decision = None
    pr.status_check_summary = None
    pr.is_draft = False
    pr.mergeable = "CONFLICTING"
    store.list_open_pull_requests = AsyncMock(return_value=[pr])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    result = await resolver.resolve(PlayType.UNBLOCK_PR, _make_state())

    assert result is not None
    assert result.pr_number == 77


@pytest.mark.asyncio
async def test_resolve_unblock_pr_falls_back_to_github() -> None:
    store = AsyncMock()
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    github = AsyncMock()
    github.list_pull_requests = AsyncMock(
        return_value=[_make_pr_record(99, review_decision="CHANGES_REQUESTED")]
    )
    resolver = ParameterResolver(
        store=store,
        manager=MagicMock(),
        cfg=_make_cfg(trusted_github_logins=("trusted",)),
        github=github,
    )

    result = await resolver.resolve(PlayType.UNBLOCK_PR, _make_state())

    assert result is not None
    assert result.pr_number == 99


@pytest.mark.asyncio
async def test_resolve_unblock_pr_skips_pr_already_being_unblocked() -> None:
    """#6: two unblock_pr plays must not run against the same PR concurrently.

    A PR with an in-flight unblock_pr dispatch (an agent BUSY on UNBLOCK_PR for
    that PR) is masked from re-selection via the per-PR resource key / in-flight
    guard, so the resolver returns no other candidate for it.
    """
    store = AsyncMock()
    pr = MagicMock()
    pr.pr_number = 9
    pr.state = "open"
    pr.labels = []
    pr.review_decision = "CHANGES_REQUESTED"
    pr.status_check_summary = None
    pr.is_draft = False
    pr.mergeable = "MERGEABLE"
    pr.issue_number = None
    pr.branch = "feature/9"
    store.list_open_pull_requests = AsyncMock(return_value=[pr])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    busy_agent = _make_snapshot(
        "agent-a",
        status=AgentStatus.BUSY,
        current_play_type=PlayType.UNBLOCK_PR,
        current_play_pr_number=9,
    )

    result = await resolver.resolve(PlayType.UNBLOCK_PR, _make_state(agents=[busy_agent]))
    assert result is None


@pytest.mark.asyncio
async def test_resolve_unblock_pr_skips_manual_required_pr() -> None:
    """#6: once an unblock_pr terminal failure labels the PR ``manual-required``,
    ``pr_unblockable`` returns False so the candidate filter excludes it.
    """
    store = AsyncMock()
    pr = MagicMock()
    pr.pr_number = 9
    pr.state = "open"
    pr.labels = ["agentshore/manual-required"]
    pr.review_decision = "CHANGES_REQUESTED"
    pr.status_check_summary = None
    pr.is_draft = False
    pr.mergeable = "MERGEABLE"
    pr.issue_number = None
    pr.branch = "feature/9"
    store.list_open_pull_requests = AsyncMock(return_value=[pr])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    result = await resolver.resolve(PlayType.UNBLOCK_PR, _make_state())
    assert result is None


@pytest.mark.asyncio
async def test_resolve_unblock_pr_skips_exhausted_pr() -> None:
    """#6: after the failure threshold, the PR is excluded from unblock_pr picks."""
    store = AsyncMock()
    pr = MagicMock()
    pr.pr_number = 9
    pr.state = "open"
    pr.labels = []
    pr.review_decision = "CHANGES_REQUESTED"
    pr.status_check_summary = None
    pr.is_draft = False
    pr.mergeable = "MERGEABLE"
    pr.issue_number = None
    pr.branch = "feature/9"
    store.list_open_pull_requests = AsyncMock(return_value=[pr])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    # Drive the per-PR failure counter to exhaustion; the last call returns True.
    assert resolver.record_unblock_pr_failure(9) is False
    assert resolver.record_unblock_pr_failure(9) is False
    assert resolver.record_unblock_pr_failure(9) is True

    result = await resolver.resolve(PlayType.UNBLOCK_PR, _make_state())
    assert result is None


# ---------------------------------------------------------------------------
# WRITE_IMPLEMENTATION_PLAN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_write_plan_skips_planned_issues() -> None:
    resolver = _make_resolver()
    state = _make_state(
        issues=[
            _make_issue(7, labels=["agentshore/planned"]),
            _make_issue(12, labels=["priority/high"]),
        ]
    )

    result = await resolver.resolve(PlayType.WRITE_IMPLEMENTATION_PLAN, state)

    assert result is not None
    assert result.issue_number == 12


@pytest.mark.asyncio
async def test_resolve_write_plan_skips_disallowed_issues() -> None:
    resolver = _make_resolver()
    state = _make_state(
        issues=[
            _make_issue(7, labels=["agentshore/disallowed"]),
            _make_issue(12, labels=["priority/high"]),
        ]
    )

    result = await resolver.resolve(PlayType.WRITE_IMPLEMENTATION_PLAN, state)

    assert result is not None
    assert result.issue_number == 12


@pytest.mark.asyncio
async def test_resolve_write_plan_skips_issue_resolved_by_merged_pr() -> None:
    resolver = _make_resolver()
    state = _make_state(
        issues=[
            _make_issue(7, labels=["priority/critical"]),
            _make_issue(12, labels=["priority/low"]),
        ],
        pull_requests=[_make_pr_snapshot(107, state="MERGED", issue_number=7)],
    )

    result = await resolver.resolve(PlayType.WRITE_IMPLEMENTATION_PLAN, state)

    assert result is not None
    assert result.issue_number == 12


@pytest.mark.asyncio
async def test_resolve_write_plan_returns_none_when_all_planned() -> None:
    resolver = _make_resolver()
    state = _make_state(issues=[_make_issue(7, labels=["agentshore/has-plan"])])

    result = await resolver.resolve(PlayType.WRITE_IMPLEMENTATION_PLAN, state)

    assert result is None


# ---------------------------------------------------------------------------
# SYSTEMATIC_DEBUGGING
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_systematic_debugging_prefers_qa_issue() -> None:
    resolver = _make_resolver()
    state = _make_state(issues=[_make_issue(7), _make_issue(12, labels=["agentshore/qa"])])

    result = await resolver.resolve(PlayType.SYSTEMATIC_DEBUGGING, state)

    assert result is not None
    assert result.issue_number == 12


@pytest.mark.asyncio
async def test_resolve_systematic_debugging_prefers_debug_needed_issue() -> None:
    resolver = _make_resolver()
    state = _make_state(
        issues=[_make_issue(7, labels=["bug"]), _make_issue(12, labels=["agentshore/debug-needed"])]
    )

    result = await resolver.resolve(PlayType.SYSTEMATIC_DEBUGGING, state)

    assert result is not None
    assert result.issue_number == 12


@pytest.mark.asyncio
async def test_resolve_systematic_debugging_ignores_review_bug_and_root_cause_found() -> None:
    resolver = _make_resolver()
    state = _make_state(
        issues=[
            _make_issue(7, labels=["agentshore/review"]),
            _make_issue(8, labels=["bug"]),
            _make_issue(12, labels=["agentshore/qa", "agentshore/root-cause-found"]),
        ]
    )

    result = await resolver.resolve(PlayType.SYSTEMATIC_DEBUGGING, state)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_systematic_debugging_skips_issue_linked_to_open_pr() -> None:
    resolver = _make_resolver()
    pr = PullRequestSnapshot(
        pr_number=100,
        title="PR for issue 12",
        state="open",
        branch="feat/12",
        issue_number=12,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
    )
    state = _make_state(issues=[_make_issue(12, labels=["agentshore/qa"])], pull_requests=[pr])

    result = await resolver.resolve(PlayType.SYSTEMATIC_DEBUGGING, state)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_systematic_debugging_does_not_use_recent_branch_after_repeated_failure() -> (
    None
):
    store = AsyncMock()
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value="agentshore/12-fix")
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())
    state = _make_state()
    state.same_type_failure_streak = 2

    result = await resolver.resolve(PlayType.SYSTEMATIC_DEBUGGING, state)

    assert result is None
    store.get_most_recent_branch.assert_not_awaited()


@pytest.mark.asyncio
async def test_override_systematic_debugging_allows_explicit_branch() -> None:
    resolver = _make_resolver()
    state = _make_state()

    result = await resolver.resolve(
        PlayType.SYSTEMATIC_DEBUGGING,
        state,
        override=PlayParams(branch="agentshore/debug-branch"),
    )

    assert result is not None
    assert result.branch == "agentshore/debug-branch"
    assert result.bypass_preconditions


# ---------------------------------------------------------------------------
# ISSUE_PICKUP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_issue_pickup_returns_first_issue() -> None:
    resolver = _make_resolver()
    issues = [_make_issue(7), _make_issue(12)]
    state = _make_state(issues=issues)

    result = await resolver.resolve(PlayType.ISSUE_PICKUP, state)

    assert result is not None
    assert result.issue_number == 7


@pytest.mark.asyncio
async def test_resolve_issue_pickup_accepts_review_bug_and_root_cause_found() -> None:
    resolver = _make_resolver()
    state = _make_state(
        issues=[
            _make_issue(7, labels=["agentshore/review"]),
            _make_issue(8, labels=["bug", "agentshore/root-cause-found"]),
        ]
    )

    result = await resolver.resolve(PlayType.ISSUE_PICKUP, state)

    assert result is not None
    assert result.issue_number == 8


@pytest.mark.asyncio
async def test_resolve_issue_pickup_skips_claimed_first_candidate() -> None:
    store = AsyncMock()
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.list_pending_reviews = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    store.acquire_work_claims = AsyncMock(side_effect=[None, "claim-12"])
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())
    state = _make_state(issues=[_make_issue(7), _make_issue(12)])

    result = await resolver.resolve(PlayType.ISSUE_PICKUP, state)

    assert result is not None
    assert result.issue_number == 12
    assert result.extras["claim_group_id"] == "claim-12"


@pytest.mark.asyncio
async def test_resolve_issue_pickup_returns_none_when_no_issues() -> None:
    resolver = _make_resolver()
    state = _make_state(issues=[])

    result = await resolver.resolve(PlayType.ISSUE_PICKUP, state)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_issue_pickup_skips_closed_issues() -> None:
    resolver = _make_resolver()
    issues = [_make_issue(7, state="closed"), _make_issue(12)]
    state = _make_state(issues=issues)

    result = await resolver.resolve(PlayType.ISSUE_PICKUP, state)

    assert result is not None
    assert result.issue_number == 12


@pytest.mark.asyncio
async def test_resolve_issue_pickup_skips_issues_with_open_pr() -> None:
    # Open PR in state.pull_requests pointing to issue 7 — should be skipped.
    resolver = _make_resolver()
    pr = _make_pr_snapshot(100, issue_number=7)
    state = _make_state(issues=[_make_issue(7), _make_issue(12)], pull_requests=[pr])

    result = await resolver.resolve(PlayType.ISSUE_PICKUP, state)

    assert result is not None
    assert result.issue_number == 12


@pytest.mark.asyncio
async def test_resolve_issue_pickup_skips_all_issues_linked_to_open_pr() -> None:
    resolver = _make_resolver()
    pr = _make_pr_snapshot(100, linked_issue_numbers=(7, 12))
    state = _make_state(
        issues=[_make_issue(7), _make_issue(12), _make_issue(20)], pull_requests=[pr]
    )

    result = await resolver.resolve(PlayType.ISSUE_PICKUP, state)

    assert result is not None
    assert result.issue_number == 20


@pytest.mark.asyncio
async def test_resolve_issue_pickup_skips_issue_resolved_by_merged_pr() -> None:
    resolver = _make_resolver()
    state = _make_state(
        issues=[
            _make_issue(7, labels=["priority/critical"]),
            _make_issue(12, labels=["priority/low"]),
        ],
        pull_requests=[_make_pr_snapshot(101, state="MERGED", issue_number=7)],
    )

    result = await resolver.resolve(PlayType.ISSUE_PICKUP, state)

    assert result is not None
    assert result.issue_number == 12


@pytest.mark.asyncio
async def test_resolve_issue_pickup_closed_pr_does_not_block_issue() -> None:
    """A closed but unmerged PR for an issue should not block pickup."""
    resolver = _make_resolver()
    pr = _make_pr_snapshot(101, state="closed", issue_number=7)
    state = _make_state(issues=[_make_issue(7)], pull_requests=[pr])

    result = await resolver.resolve(PlayType.ISSUE_PICKUP, state)

    assert result is not None
    assert result.issue_number == 7


@pytest.mark.asyncio
async def test_resolve_issue_pickup_pr_without_issue_number_ignored() -> None:
    """PRs with no linked issue_number don't accidentally block any issue."""
    resolver = _make_resolver()
    pr = _make_pr_snapshot(102)
    state = _make_state(issues=[_make_issue(7)], pull_requests=[pr])

    result = await resolver.resolve(PlayType.ISSUE_PICKUP, state)

    assert result is not None
    assert result.issue_number == 7


@pytest.mark.asyncio
async def test_resolve_issue_pickup_skips_blocked_label() -> None:
    resolver = _make_resolver()
    issues = [_make_issue(7, labels=["agentshore/blocked"]), _make_issue(12)]
    state = _make_state(issues=issues)

    result = await resolver.resolve(PlayType.ISSUE_PICKUP, state)

    assert result is not None
    assert result.issue_number == 12


@pytest.mark.asyncio
async def test_resolve_issue_pickup_skips_disallowed_label() -> None:
    resolver = _make_resolver()
    issues = [_make_issue(7, labels=["agentshore/disallowed"]), _make_issue(12)]
    state = _make_state(issues=issues)

    result = await resolver.resolve(PlayType.ISSUE_PICKUP, state)

    assert result is not None
    assert result.issue_number == 12


@pytest.mark.asyncio
async def test_resolve_issue_pickup_skips_needs_refinement_label() -> None:
    """Issues still awaiting refine-tasks scope analysis should not be picked up."""
    resolver = _make_resolver()
    issues = [
        _make_issue(7, labels=["agentshore/needs-refinement"]),
        _make_issue(12),
    ]
    state = _make_state(issues=issues)

    result = await resolver.resolve(PlayType.ISSUE_PICKUP, state)

    assert result is not None
    assert result.issue_number == 12


@pytest.mark.asyncio
async def test_resolve_issue_pickup_prefers_higher_priority() -> None:
    resolver = _make_resolver()
    # Issue 12 (low) and 7 (critical) — 7 should win despite higher number
    issues = [
        _make_issue(12, priority=3, labels=["priority/low"]),
        _make_issue(7, priority=0, labels=["priority/critical"]),
    ]
    state = _make_state(issues=issues)

    result = await resolver.resolve(PlayType.ISSUE_PICKUP, state)

    assert result is not None
    assert result.issue_number == 7


@pytest.mark.asyncio
async def test_resolve_issue_pickup_prefers_smaller_size_within_priority() -> None:
    resolver = _make_resolver()
    # Both medium priority; smaller size wins.
    issues = [
        _make_issue(7, priority=2, labels=["priority/medium", "size/L"]),
        _make_issue(12, priority=2, labels=["priority/medium", "size/S"]),
    ]
    state = _make_state(issues=issues)

    result = await resolver.resolve(PlayType.ISSUE_PICKUP, state)

    assert result is not None
    assert result.issue_number == 12


@pytest.mark.asyncio
async def test_resolve_issue_pickup_falls_back_to_labels_when_priority_field_missing() -> None:
    """When the numeric priority field is None but a label exists, rank by label."""
    resolver = _make_resolver()
    issues = [
        _make_issue(7, priority=None, labels=["priority/low"]),
        _make_issue(12, priority=None, labels=["priority/critical"]),
    ]
    state = _make_state(issues=issues)

    result = await resolver.resolve(PlayType.ISSUE_PICKUP, state)

    assert result is not None
    assert result.issue_number == 12


@pytest.mark.asyncio
async def test_resolve_issue_pickup_returns_none_when_all_ineligible() -> None:
    resolver = _make_resolver()
    issues = [
        _make_issue(7, labels=["agentshore/blocked"]),
        _make_issue(12, state="closed"),
    ]
    state = _make_state(issues=issues)

    result = await resolver.resolve(PlayType.ISSUE_PICKUP, state)

    assert result is None


# ---------------------------------------------------------------------------
# CODE_REVIEW
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_code_review_returns_oldest_pending() -> None:
    from agentshore.data.models import ReviewQueueRecord

    store = AsyncMock()
    row = ReviewQueueRecord(
        pr_number=42,
        session_id="sess-test",
        enqueued_at="2026-01-01T00:00:00Z",
        queue_id=1,
        author_label=None,
    )
    store.list_pending_reviews = AsyncMock(return_value=[row])
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    pr_snap = PullRequestSnapshot(
        pr_number=42,
        title="PR #42",
        state="open",
        branch="feat/42",
        issue_number=None,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
    )
    state = _make_state(
        # code_review is large-only (#254).
        agents=[_make_snapshot("a-codex", agent_type=AgentType.CODEX, model_tier="large")],
        pull_requests=[pr_snap],
    )
    result = await resolver.resolve(PlayType.CODE_REVIEW, state)

    assert result is not None
    assert result.pr_number == 42
    assert result.target_agent_id == "a-codex"


@pytest.mark.asyncio
async def test_resolve_code_review_picks_pr_with_cross_identity_reviewer() -> None:
    """A pending review is dispatched to the agent whose GH identity differs."""
    from agentshore.data.models import ReviewQueueRecord

    store = AsyncMock()
    row = ReviewQueueRecord(
        pr_number=42,
        session_id="sess-test",
        enqueued_at="2026-01-01T00:00:00Z",
        queue_id=1,
        author_label=None,
    )
    store.list_pending_reviews = AsyncMock(return_value=[row])
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    pr_snap = PullRequestSnapshot(
        pr_number=42,
        title="PR 42",
        state="open",
        branch="feature/42",
        issue_number=None,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
        github_author="user_a",
    )
    state = OrchestratorState(
        session_id="sess-test",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[
            _make_snapshot(
                "a-claude",
                agent_type=AgentType.CLAUDE_CODE,
                model_tier="large",
                github_identity="user_a",
            ),
            _make_snapshot(
                "a-codex",
                agent_type=AgentType.CODEX,
                model_tier="large",
                github_identity="user_b",
            ),
        ],
        open_issues=[],
        pull_requests=[pr_snap],
        budget=BudgetSnapshot(
            total_budget=5.0, spent=0.0, remaining=5.0, estimated_cost_per_play=0.1
        ),
    )
    result = await resolver.resolve(PlayType.CODE_REVIEW, state)
    assert result is not None
    assert result.pr_number == 42
    # Only a-codex has a different identity from the PR author "user_a".
    assert result.target_agent_id == "a-codex"


@pytest.mark.asyncio
async def test_resolve_code_review_skips_pr_when_only_same_identity_reviewer() -> None:
    """A pending review is deferred when every idle reviewer shares the PR author identity.

    Replaces the old type-based 'skips when only same-type reviewers' test.
    Identity is the only deconfliction key now: two reviewers of the same
    type with different GH identities ARE eligible; one reviewer with the
    same identity as the PR author is NOT.
    """
    from agentshore.data.models import ReviewQueueRecord

    store = AsyncMock()
    row = ReviewQueueRecord(
        pr_number=42,
        session_id="sess-test",
        enqueued_at="2026-01-01T00:00:00Z",
        queue_id=1,
        author_label=None,
    )
    store.list_pending_reviews = AsyncMock(return_value=[row])
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    pr_snap = PullRequestSnapshot(
        pr_number=42,
        title="PR 42",
        state="open",
        branch="feature/42",
        issue_number=None,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
        github_author="user_a",
    )
    state = OrchestratorState(
        session_id="sess-test",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[
            _make_snapshot(
                "a-codex",
                agent_type=AgentType.CODEX,
                model_tier="medium",
                github_identity="user_a",  # same as PR author
            ),
        ],
        open_issues=[],
        pull_requests=[pr_snap],
        budget=BudgetSnapshot(
            total_budget=5.0, spent=0.0, remaining=5.0, estimated_cost_per_play=0.1
        ),
    )

    from unittest.mock import patch

    with patch(
        "agentshore.plays.candidates.PlayCandidateService._github_pr_candidates",
        return_value=[],
    ):
        result = await resolver.resolve(PlayType.CODE_REVIEW, state)
    assert result is None


@pytest.mark.asyncio
async def test_resolve_code_review_returns_none_when_no_pending() -> None:
    resolver = _make_resolver()
    result = await resolver.resolve(PlayType.CODE_REVIEW, _make_state())
    assert result is None


@pytest.mark.asyncio
async def test_resolve_code_review_unknown_author_accepts_any_reviewer() -> None:
    """When pr.github_author is None (pre-session PR, deleted account, etc.),
    any idle reviewer is acceptable — GitHub itself will reject a self-review."""
    from agentshore.data.models import ReviewQueueRecord

    store = AsyncMock()
    row = ReviewQueueRecord(
        pr_number=77,
        session_id="sess-test",
        enqueued_at="2026-01-01T00:00:00Z",
        queue_id=1,
        author_label=None,
    )
    store.list_pending_reviews = AsyncMock(return_value=[row])
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    pr_snap = PullRequestSnapshot(
        pr_number=77,
        title="PR #77",
        state="open",
        branch="feat/77",
        issue_number=None,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
        github_author=None,
    )
    state = _make_state(
        agents=[
            _make_snapshot(
                "a-claude",
                agent_type=AgentType.CLAUDE_CODE,
                model_tier="large",
                github_identity="user_a",
            )
        ],
        pull_requests=[pr_snap],
    )
    result = await resolver.resolve(PlayType.CODE_REVIEW, state)
    assert result is not None
    assert result.pr_number == 77
    assert result.target_agent_id == "a-claude"


@pytest.mark.asyncio
async def test_resolve_code_review_falls_back_to_github_when_queue_empty() -> None:
    """When no pending reviews exist, resolver falls back to GitHub."""
    store = AsyncMock()
    store.list_pending_reviews = AsyncMock(return_value=[])
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    github = AsyncMock()
    github.list_pull_requests = AsyncMock(return_value=[_make_pr_record(99)])
    resolver = ParameterResolver(
        store=store,
        manager=MagicMock(),
        cfg=_make_cfg(trusted_github_logins=("trusted",)),
        github=github,
    )

    result = await resolver.resolve(
        PlayType.CODE_REVIEW,
        _make_state(
            agents=[
                _make_snapshot(
                    "r",
                    agent_type=AgentType.CODEX,
                    model_tier="large",
                    github_identity="reviewer",
                )
            ]
        ),
    )

    assert result is not None
    assert result.pr_number == 99
    assert result.target_agent_id == "r"


@pytest.mark.asyncio
async def test_resolve_code_review_uses_state_pull_requests_when_queue_empty() -> None:
    """When pending_reviews is empty, resolver picks from state.pull_requests
    using freshness ranking, skipping already-reviewed-at-current-SHA PRs."""
    store = AsyncMock()
    store.list_pending_reviews = AsyncMock(return_value=[])
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    def _pr(num: int, last_sha: str | None = None, head: str | None = None) -> PullRequestSnapshot:
        return PullRequestSnapshot(
            pr_number=num,
            title=f"PR {num}",
            state="open",
            branch=f"feature/{num}",
            issue_number=None,
            labels=[],
            review_decision=None,
            status_check_summary=None,
            is_draft=False,
            blocked=False,
            blocked_reasons=[],
            last_reviewed_sha=last_sha,
            head_sha=head,
        )

    pr_current = _pr(10, last_sha="abc", head="abc")  # unchanged reviewed head — excluded
    pr_never = _pr(20, last_sha=None, head="def")  # never reviewed — eligible
    pr_stale = _pr(30, last_sha="old", head="new")  # stale (sha advanced) — eligible

    state = OrchestratorState(
        session_id="sess-test",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[
            _make_snapshot(
                "a-codex",
                agent_type=AgentType.CODEX,
                model_tier="large",
                github_identity="user_x",
            )
        ],
        open_issues=[],
        pull_requests=[pr_current, pr_never, pr_stale],
        budget=BudgetSnapshot(
            total_budget=5.0, spent=0.0, remaining=5.0, estimated_cost_per_play=0.1
        ),
    )
    result = await resolver.resolve(PlayType.CODE_REVIEW, state)
    # Both pr_never and pr_stale are eligible; pr_stale has _STALE_VERDICT_RANK when
    # last_review_status is set — but in this fixture it's None, so both get NEVER_REVIEWED
    # rank. PR 20 comes first in the list and is picked.
    assert result is not None
    assert result.pr_number in (20, 30)  # either eligible PR is acceptable
    assert result.target_agent_id == "a-codex"


@pytest.mark.asyncio
async def test_resolve_code_review_skips_draft_prs_in_state_fallback() -> None:
    """Draft PRs in state.pull_requests are not eligible for code_review."""
    store = AsyncMock()
    store.list_pending_reviews = AsyncMock(return_value=[])
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    draft_pr = PullRequestSnapshot(
        pr_number=42,
        title="Draft PR",
        state="open",
        branch="feature/42",
        issue_number=None,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=True,
        blocked=False,
        blocked_reasons=[],
    )
    state = OrchestratorState(
        session_id="sess-test",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[],
        open_issues=[],
        pull_requests=[draft_pr],
        budget=BudgetSnapshot(
            total_budget=5.0, spent=0.0, remaining=5.0, estimated_cost_per_play=0.1
        ),
    )
    result = await resolver.resolve(PlayType.CODE_REVIEW, state)
    assert result is None


@pytest.mark.asyncio
async def test_resolve_code_review_skips_manual_required_prs_in_state_fallback() -> None:
    """A manual-required PR is parked for a human — never dispatched to a reviewer
    (#167). Mirrors pr_reviewable / the build_candidate_plan exclusion so the queue
    can't churn reviewers on a human-blocked PR."""
    from agentshore.github.labels import MANUAL_REQUIRED_LABEL

    store = AsyncMock()
    store.list_pending_reviews = AsyncMock(return_value=[])
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    manual_pr = PullRequestSnapshot(
        pr_number=42,
        title="Blocked PR",
        state="open",
        branch="feature/42",
        issue_number=None,
        labels=[MANUAL_REQUIRED_LABEL],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
    )
    state = OrchestratorState(
        session_id="sess-test",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[
            _make_snapshot(
                "a-codex",
                agent_type=AgentType.CODEX,
                model_tier="medium",
                github_identity="user_x",
            )
        ],
        open_issues=[],
        pull_requests=[manual_pr],
        budget=BudgetSnapshot(
            total_budget=5.0, spent=0.0, remaining=5.0, estimated_cost_per_play=0.1
        ),
    )
    result = await resolver.resolve(PlayType.CODE_REVIEW, state)
    assert result is None


# ---------------------------------------------------------------------------
# MERGE_PR
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_run_qa_returns_empty_params_for_default_branch() -> None:
    """run_qa now targets the merged default branch — no specific branch lookup.

    run_qa is trunk-scoped but not trunk-mutating, so it self-serializes on a
    ``session:run_qa`` claim. The resolved params carry the claim metadata but
    no branch/issue/pr target.
    """
    resolver = _make_resolver()
    resolver._store.acquire_work_claims = AsyncMock(return_value="claim-qa")

    result = await resolver.resolve(PlayType.RUN_QA, _make_state())

    assert result is not None
    assert result.branch is None
    assert result.issue_number is None
    assert result.pr_number is None
    assert result.extras["claim_group_id"] == "claim-qa"
    resolver._store.acquire_work_claims.assert_awaited_once_with(
        "sess-test", "run_qa", ["session:run_qa"]
    )


# ---------------------------------------------------------------------------
# MERGE_PR
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_merge_pr_returns_approved_pr() -> None:
    store = AsyncMock()
    pr = MagicMock()
    pr.pr_number = 55
    pr.mergeable = "MERGEABLE"
    pr.review_decision = "APPROVED"
    pr.last_review_status = None
    pr.last_reviewed_sha = None
    pr.head_sha = None
    pr.issue_number = None
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[pr])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    result = await resolver.resolve(PlayType.MERGE_PR, _make_state())

    assert result is not None
    assert result.pr_number == 55


@pytest.mark.asyncio
async def test_resolve_merge_pr_returns_none_when_pr_claimed() -> None:
    store = AsyncMock()
    pr = MagicMock()
    pr.pr_number = 210
    pr.issue_number = 195
    pr.mergeable = "MERGEABLE"
    pr.review_decision = "APPROVED"
    pr.last_review_status = None
    pr.last_reviewed_sha = None
    pr.head_sha = None
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[pr])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    store.acquire_work_claims = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    result = await resolver.resolve(PlayType.MERGE_PR, _make_state())

    assert result is None
    store.acquire_work_claims.assert_awaited_once_with(
        "sess-test", "merge_pr", ["pr:210", "issue:195", "trunk:main_repo"]
    )


# ---------------------------------------------------------------------------
# Trunk-claim split (#17): only trunk-*mutating* plays take trunk:main_repo;
# read-only trunk plays self-serialize on a session key instead, so they can't
# starve merge_pr.
# ---------------------------------------------------------------------------


# ``_resource_keys_for_params`` is the chokepoint every play-type→claim-key
# decision flows through: no-arg trunk plays reach it via _claim_candidate →
# _claim_params (empty candidate keys → ``keys = [] or _resource_keys_for_params``),
# and run_qa via _resolve_run_qa. The DB evidence for #17 — a design_audit
# claim whose only key was ``trunk:main_repo`` — is exactly this function's
# output, so testing it directly pins the fix.


@pytest.mark.parametrize(
    "play_type",
    [
        PlayType.RUN_QA,
        PlayType.DESIGN_AUDIT,
        PlayType.CALIBRATE_ALIGNMENT,
        PlayType.GROOM_BACKLOG,
        PlayType.SEED_PROJECT,
    ],
)
@pytest.mark.asyncio
async def test_read_only_trunk_plays_self_serialize_not_trunk_lock(
    play_type: PlayType,
) -> None:
    """run_qa/design_audit/calibrate/groom/seed claim only a session key — never trunk."""
    resolver = _make_resolver()

    keys = await resolver._resource_keys_for_params(play_type, _make_state(), PlayParams())

    assert keys == [f"session:{play_type.value}"]
    assert "trunk:main_repo" not in keys


@pytest.mark.parametrize("play_type", [PlayType.CLEANUP, PlayType.RECONCILE_STATE])
@pytest.mark.asyncio
async def test_trunk_mutating_plays_keep_trunk_lock(play_type: PlayType) -> None:
    """cleanup + reconcile_state still serialize on trunk:main_repo (merge_pr covered above)."""
    resolver = _make_resolver()

    keys = await resolver._resource_keys_for_params(play_type, _make_state(), PlayParams())

    assert keys == ["trunk:main_repo"]


@pytest.mark.asyncio
async def test_resolve_merge_pr_skips_unmergeable_approved_pr() -> None:
    # Regression for the example-repo burn: resolver returned the first
    # approved PR even when it was CONFLICTING, causing the executor to
    # spin on a merge that could never succeed.
    store = AsyncMock()
    bad = MagicMock()
    bad.pr_number = 103
    bad.mergeable = "CONFLICTING"
    bad.review_decision = "APPROVED"
    bad.last_review_status = None
    bad.last_reviewed_sha = None
    bad.head_sha = None
    good = MagicMock()
    good.pr_number = 98
    good.mergeable = "MERGEABLE"
    good.review_decision = "APPROVED"
    good.last_review_status = None
    good.last_reviewed_sha = None
    good.head_sha = None
    good.issue_number = None
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[bad, good])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    result = await resolver.resolve(PlayType.MERGE_PR, _make_state())

    assert result is not None
    assert result.pr_number == 98


@pytest.mark.asyncio
async def test_resolve_merge_pr_returns_none_when_no_approved_prs() -> None:
    resolver = _make_resolver()
    result = await resolver.resolve(PlayType.MERGE_PR, _make_state())
    assert result is None


@pytest.mark.asyncio
async def test_resolve_merge_pr_live_fallback_ignores_unapproved_mergeable_pr() -> None:
    store = AsyncMock()
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    github = AsyncMock()
    github.list_pull_requests = AsyncMock(
        return_value=[_make_pr_record(350, review_decision=None, last_review_status=None)]
    )
    resolver = ParameterResolver(
        store=store,
        manager=MagicMock(),
        cfg=_make_cfg(trusted_github_logins=("trusted",)),
        github=github,
    )

    result = await resolver.resolve(PlayType.MERGE_PR, _make_state())

    assert result is None


@pytest.mark.asyncio
async def test_resolve_merge_pr_live_fallback_accepts_approved_mergeable_pr() -> None:
    store = AsyncMock()
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    store.acquire_work_claims = AsyncMock(return_value="claim-351")
    github = AsyncMock()
    github.list_pull_requests = AsyncMock(
        return_value=[_make_pr_record(351, issue_number=222, review_decision="APPROVED")]
    )
    resolver = ParameterResolver(
        store=store,
        manager=MagicMock(),
        cfg=_make_cfg(trusted_github_logins=("trusted",)),
        github=github,
    )

    result = await resolver.resolve(PlayType.MERGE_PR, _make_state())

    assert result is not None
    assert result.pr_number == 351
    assert result.extras["resource_keys"] == ["pr:351", "issue:222"]
    store.acquire_work_claims.assert_awaited_once_with(
        "sess-test", "merge_pr", ["pr:351", "issue:222"]
    )


@pytest.mark.asyncio
async def test_resolve_merge_pr_live_fallback_accepts_agentshore_pass_at_head() -> None:
    store = AsyncMock()
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    github = AsyncMock()
    github.list_pull_requests = AsyncMock(
        return_value=[
            _make_pr_record(
                352,
                head_sha="abc123",
                last_reviewed_sha="abc123",
                last_review_status="PASS",
            )
        ]
    )
    resolver = ParameterResolver(
        store=store,
        manager=MagicMock(),
        cfg=_make_cfg(trusted_github_logins=("trusted",)),
        github=github,
    )

    result = await resolver.resolve(PlayType.MERGE_PR, _make_state())

    assert result is not None
    assert result.pr_number == 352


@pytest.mark.asyncio
async def test_resolve_merge_pr_returns_none_when_only_unmergeable_approved_prs() -> None:
    store = AsyncMock()
    bad = MagicMock()
    bad.pr_number = 103
    bad.mergeable = "CONFLICTING"
    bad.review_decision = "APPROVED"
    bad.last_review_status = None
    bad.last_reviewed_sha = None
    bad.head_sha = None
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[bad])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg(), github=None)

    result = await resolver.resolve(PlayType.MERGE_PR, _make_state())

    assert result is None


@pytest.mark.asyncio
async def test_resolve_merge_pr_skips_pr_already_being_merged() -> None:
    """Two agents must not be dispatched to merge the same PR concurrently."""
    store = AsyncMock()
    pr = MagicMock()
    pr.pr_number = 77
    pr.mergeable = "MERGEABLE"
    pr.review_decision = "APPROVED"
    pr.last_review_status = None
    pr.last_reviewed_sha = None
    pr.head_sha = None
    pr.issue_number = None
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[pr])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    # Agent A is already merging PR 77
    busy_agent = AgentSnapshot(
        agent_id="agent-a",
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.BUSY,
        model_tier="large",
        context_size=10_000,
        total_cost=0.5,
        total_tokens=0,
        tasks_completed=5,
        tasks_failed=0,
        current_play_type=PlayType.MERGE_PR,
        current_play_pr_number=77,
    )
    state = OrchestratorState(
        session_id="sess-test",
        session_state=SessionState.RUNNING,
        total_plays=5,
        total_cost=0.5,
        agents=[busy_agent],
        budget=BudgetSnapshot(
            total_budget=5.0, spent=0.5, remaining=4.5, estimated_cost_per_play=0.1
        ),
    )

    result = await resolver.resolve(PlayType.MERGE_PR, state)
    assert result is None


@pytest.mark.asyncio
async def test_resolve_merge_pr_live_fallback_skips_pr_already_being_merged() -> None:
    store = AsyncMock()
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    github = AsyncMock()
    github.list_pull_requests = AsyncMock(
        return_value=[_make_pr_record(353, review_decision="APPROVED")]
    )
    resolver = ParameterResolver(
        store=store,
        manager=MagicMock(),
        cfg=_make_cfg(trusted_github_logins=("trusted",)),
        github=github,
    )
    busy_agent = _make_snapshot(
        "agent-a",
        status=AgentStatus.BUSY,
        current_play_type=PlayType.MERGE_PR,
        current_play_pr_number=353,
    )

    result = await resolver.resolve(PlayType.MERGE_PR, _make_state(agents=[busy_agent]))
    assert result is None


# ---------------------------------------------------------------------------
# END_AGENT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_end_agent_picks_highest_failure_rate() -> None:
    """Among agents past the play-count gate (>10), pick the worst performer."""
    resolver = _make_resolver()
    agents = [
        # Both agents have >10 plays (gate satisfied); agent-b has higher
        # failure rate (50%) so it should be the termination target.
        _make_snapshot("agent-a", tasks_completed=20, tasks_failed=2),  # ~9% fail
        _make_snapshot("agent-b", tasks_completed=10, tasks_failed=10),  # 50% fail
    ]
    state = _make_state(agents=agents)

    result = await resolver.resolve(PlayType.END_AGENT, state)

    assert result is not None
    assert result.agent_id == "agent-b"


@pytest.mark.asyncio
async def test_resolve_end_agent_returns_none_when_no_idle_agents() -> None:
    resolver = _make_resolver()
    agents = [_make_snapshot("agent-a", status=AgentStatus.BUSY)]
    state = _make_state(agents=agents)

    result = await resolver.resolve(PlayType.END_AGENT, state)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_end_agent_targets_terminal_error_agent() -> None:
    """#20: a non-recoverable ERROR agent (e.g. invalid_model) is the END_AGENT
    target, bypassing the min-plays gate, even with zero plays — it has no
    recovery path and would otherwise leak until end_session."""
    resolver = _make_resolver()
    agents = [
        _make_snapshot("healthy", status=AgentStatus.IDLE, tasks_completed=1),
        _make_snapshot(
            "broken",
            status=AgentStatus.ERROR,
            last_error_class=ErrorClass.INVALID_MODEL,
            tasks_completed=0,
            tasks_failed=1,
        ),
    ]
    state = _make_state(agents=agents)

    result = await resolver.resolve(PlayType.END_AGENT, state)

    assert result is not None
    assert result.agent_id == "broken"
    assert result.bypass_preconditions is True


@pytest.mark.asyncio
async def test_resolve_end_agent_skips_recoverable_error_agent() -> None:
    """A recoverable ERROR agent (rate_limit/unknown) is NOT a terminal target —
    it still has the TAKE_BREAK recovery path, so END_AGENT falls through to the
    normal idle-failure-rate selection (no idle past the gate here → None)."""
    resolver = _make_resolver()
    agents = [
        _make_snapshot(
            "throttled",
            status=AgentStatus.ERROR,
            last_error_class=ErrorClass.RATE_LIMIT,
            tasks_completed=0,
            tasks_failed=1,
        ),
    ]
    state = _make_state(agents=agents)

    result = await resolver.resolve(PlayType.END_AGENT, state)

    assert result is None


# ---------------------------------------------------------------------------
# INSTANTIATE_AGENT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_instantiate_agent_uses_default_tier_first() -> None:
    resolver = _make_resolver()

    result = await resolver.resolve(PlayType.INSTANTIATE_AGENT, _make_state())

    assert result == PlayParams(target_agent_type="claude_code", target_model_tier="medium")


@pytest.mark.asyncio
async def test_resolve_instantiate_agent_uses_config_order_not_claude_pin() -> None:
    cfg = RuntimeConfig(
        agents={
            "codex": AgentConfig(enabled=True),
            "claude_code": AgentConfig(enabled=True),
        }
    )
    resolver = _make_resolver(cfg)

    result = await resolver.resolve(PlayType.INSTANTIATE_AGENT, _make_state())

    assert result == PlayParams(target_agent_type="codex", target_model_tier="medium")


@pytest.mark.asyncio
async def test_resolve_instantiate_agent_spreads_default_tier_across_backends() -> None:
    resolver = _make_resolver()
    state = _make_state(agents=[_make_snapshot("agent-a", model_tier="medium")])

    result = await resolver.resolve(PlayType.INSTANTIATE_AGENT, state)

    assert result == PlayParams(target_agent_type="codex", target_model_tier="medium")


@pytest.mark.asyncio
async def test_resolve_instantiate_agent_fills_missing_model_tier() -> None:
    cfg = RuntimeConfig(
        agents={
            "claude_code": AgentConfig(enabled=True),
            "codex": AgentConfig(enabled=False),
        }
    )
    resolver = _make_resolver(cfg)
    state = _make_state(
        agents=[
            _make_snapshot("agent-a", model_tier="medium"),
        ]
    )

    result = await resolver.resolve(PlayType.INSTANTIATE_AGENT, state)

    assert result == PlayParams(target_agent_type="claude_code", target_model_tier="small")


@pytest.mark.asyncio
async def test_resolve_instantiate_agent_uses_config_index_override() -> None:
    """When the PPO config head supplies a pick, the resolver returns it directly."""
    resolver = _make_resolver()
    state = _make_state()

    result = await resolver.resolve(
        PlayType.INSTANTIATE_AGENT,
        state,
        config_index_override=("codex", "small"),
    )

    assert result == PlayParams(target_agent_type="codex", target_model_tier="small")


@pytest.mark.asyncio
async def test_resolve_instantiate_agent_rejects_override_for_provider_in_take_break() -> None:
    resolver = _make_resolver()
    state = _make_state(
        agents=[
            _make_snapshot(
                "cooling-codex",
                agent_type=AgentType.CODEX,
                status=AgentStatus.ERROR,
                model_tier="small",
                last_error_class=ErrorClass.RATE_LIMIT,
                current_play_type=PlayType.TAKE_BREAK,
            )
        ]
    )

    result = await resolver.resolve(
        PlayType.INSTANTIATE_AGENT,
        state,
        config_index_override=("codex", "small"),
    )

    assert result is None


@pytest.mark.asyncio
async def test_resolve_instantiate_agent_falls_back_when_override_is_none() -> None:
    """No override → use the configured round-robin logic."""
    resolver = _make_resolver()
    state = _make_state()

    result = await resolver.resolve(
        PlayType.INSTANTIATE_AGENT,
        state,
        config_index_override=None,
    )

    assert result == PlayParams(target_agent_type="claude_code", target_model_tier="medium")


@pytest.mark.asyncio
async def test_resolve_instantiate_agent_returns_none_when_only_config_has_idle_agent() -> None:
    cfg = RuntimeConfig(
        agents={
            "claude_code": AgentConfig(
                enabled=True,
                model_tiers={"medium": ModelTierConfig(model="m", enabled=True)},
            ),
            "codex": AgentConfig(enabled=False),
        }
    )
    resolver = _make_resolver(cfg)
    state = _make_state(agents=[_make_snapshot("idle-claude", model_tier="medium")])

    result = await resolver.resolve(PlayType.INSTANTIATE_AGENT, state)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_instantiate_agent_returns_none_when_only_config_at_per_tier_max() -> None:
    """#159: a BUSY (non-idle) agent that fills the only cell's per-tier max must
    not be re-picked. Previously the fallback excluded only IDLE cells, so the
    resolver deterministically returned a full cell that execute() then rejected
    ("at per-tier max"), spinning instantiate_agent."""
    cfg = RuntimeConfig(
        agents={
            "claude_code": AgentConfig(
                enabled=True,
                model_tiers={"medium": ModelTierConfig(model="m", enabled=True)},
            ),
            "codex": AgentConfig(enabled=False),
        }
    )
    resolver = _make_resolver(cfg)
    state = _make_state(
        agents=[_make_snapshot("busy-claude", status=AgentStatus.BUSY, model_tier="medium")]
    )

    result = await resolver.resolve(PlayType.INSTANTIATE_AGENT, state)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_instantiate_agent_ignores_terminated_agents_for_capacity() -> None:
    """A TERMINATED agent frees its cell, matching execute()'s capacity definition."""
    cfg = RuntimeConfig(
        agents={
            "claude_code": AgentConfig(
                enabled=True,
                model_tiers={"medium": ModelTierConfig(model="m", enabled=True)},
            ),
            "codex": AgentConfig(enabled=False),
        }
    )
    resolver = _make_resolver(cfg)
    state = _make_state(
        agents=[_make_snapshot("dead-claude", status=AgentStatus.TERMINATED, model_tier="medium")]
    )

    result = await resolver.resolve(PlayType.INSTANTIATE_AGENT, state)

    assert result == PlayParams(target_agent_type="claude_code", target_model_tier="medium")


@pytest.mark.asyncio
async def test_resolve_instantiate_agent_counts_error_agents_for_capacity() -> None:
    cfg = RuntimeConfig(
        agents={
            "claude_code": AgentConfig(
                enabled=True,
                model_tiers={"medium": ModelTierConfig(model="m", enabled=True, max=1)},
            ),
            "codex": AgentConfig(enabled=False),
        }
    )
    resolver = _make_resolver(cfg)
    state = _make_state(
        agents=[
            _make_snapshot(
                "error-claude",
                status=AgentStatus.ERROR,
                model_tier="medium",
                last_error_class=ErrorClass.UNKNOWN,
            )
        ]
    )

    result = await resolver.resolve(PlayType.INSTANTIATE_AGENT, state)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_instantiate_agent_skips_provider_in_take_break() -> None:
    cfg = RuntimeConfig(
        agents={
            "claude_code": AgentConfig(
                enabled=True,
                model_tiers={"medium": ModelTierConfig(model="m", enabled=True, max=3)},
            ),
            "codex": AgentConfig(
                enabled=True,
                model_tiers={"medium": ModelTierConfig(model="m", enabled=True, max=3)},
            ),
        }
    )
    resolver = _make_resolver(cfg)
    state = _make_state(
        agents=[
            _make_snapshot(
                "cooling-claude",
                status=AgentStatus.ERROR,
                model_tier="medium",
                last_error_class=ErrorClass.RATE_LIMIT,
                current_play_type=PlayType.TAKE_BREAK,
            )
        ]
    )

    result = await resolver.resolve(PlayType.INSTANTIATE_AGENT, state)

    assert result == PlayParams(target_agent_type="codex", target_model_tier="medium")


@pytest.mark.asyncio
async def test_resolve_instantiate_agent_returns_none_when_no_enabled_configs() -> None:
    cfg = RuntimeConfig(
        agents={
            "claude_code": AgentConfig(enabled=False),
            "codex": AgentConfig(enabled=False),
        }
    )
    resolver = _make_resolver(cfg)

    result = await resolver.resolve(PlayType.INSTANTIATE_AGENT, _make_state())

    assert result is None


# ---------------------------------------------------------------------------
# TAKE_BREAK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_take_break_attributes_rate_limit_trigger() -> None:
    resolver = _make_resolver()
    state = _make_state(
        agents=[
            _make_snapshot(
                "claude-unknown",
                status=AgentStatus.ERROR,
                last_error_class=ErrorClass.UNKNOWN,
            ),
            _make_snapshot(
                "grok-rate-limit",
                agent_type=AgentType.GROK,
                status=AgentStatus.ERROR,
                last_error_class=ErrorClass.RATE_LIMIT,
            ),
        ]
    )

    result = await resolver.resolve(PlayType.TAKE_BREAK, state)

    assert result == PlayParams(
        agent_id="grok-rate-limit",
        extras={
            "trigger_agent_id": "grok-rate-limit",
            "trigger_agent_type": "grok",
            "trigger_error_class": "rate_limit",
        },
    )


@pytest.mark.asyncio
async def test_resolve_take_break_leaves_no_trigger_without_error_source() -> None:
    resolver = _make_resolver()
    state = _make_state(agents=[_make_snapshot("agent-idle")])

    result = await resolver.resolve(PlayType.TAKE_BREAK, state)

    assert result == PlayParams()


@pytest.mark.asyncio
async def test_resolve_take_break_skips_agent_already_cooling_down() -> None:
    resolver = _make_resolver()
    state = _make_state(
        agents=[
            _make_snapshot(
                "grok-cooling",
                agent_type=AgentType.GROK,
                status=AgentStatus.ERROR,
                last_error_class=ErrorClass.RATE_LIMIT,
                current_play_type=PlayType.TAKE_BREAK,
            ),
            _make_snapshot(
                "codex-unknown",
                agent_type=AgentType.CODEX,
                status=AgentStatus.ERROR,
                last_error_class=ErrorClass.UNKNOWN,
            ),
        ]
    )

    result = await resolver.resolve(PlayType.TAKE_BREAK, state)

    assert result is not None
    assert result.agent_id == "codex-unknown"
    assert result.extras["trigger_agent_id"] == "codex-unknown"


# ---------------------------------------------------------------------------
# No-arg plays
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_arg_plays_return_empty_params() -> None:
    resolver = _make_resolver()
    state = _make_state()
    for play_type in (
        PlayType.END_SESSION,
        PlayType.CLEANUP,
        PlayType.GROOM_BACKLOG,
        PlayType.CALIBRATE_ALIGNMENT,
    ):
        result = await resolver.resolve(play_type, state)
        assert result == PlayParams(), f"{play_type} should return empty PlayParams"


# ---------------------------------------------------------------------------
# Timing — <100ms across 50 issues + 50 PR mocks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_timing_under_100ms() -> None:
    store = AsyncMock()
    prs = [MagicMock(pr_number=i, author_agent_type=None) for i in range(50)]
    for pr in prs:
        pr.state = "open"
        pr.labels = []
        pr.review_decision = None
        pr.status_check_summary = None
        pr.is_draft = False
        pr.mergeable = None
        pr.issue_number = None
        pr.blocked = False
        pr.github_author = None
        pr.last_reviewed_sha = None
        pr.last_review_status = None
        pr.head_sha = None
    prs[0].state = "changes_requested"
    store.list_open_pull_requests = AsyncMock(return_value=prs)
    store.list_approved_pull_requests = AsyncMock(return_value=prs)
    store.list_pending_reviews = AsyncMock(return_value=[])
    store.acquire_work_claims = AsyncMock(return_value="claim")
    store.get_pull_request = AsyncMock(return_value=None)
    store.claim_pending_review_for_pr = AsyncMock(return_value=None)
    store.get_most_recent_branch = AsyncMock(return_value="main")
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    issues = [_make_issue(i) for i in range(50)]
    state = _make_state(issues=issues)

    start = time.monotonic()
    for play_type in (
        PlayType.UNBLOCK_PR,
        PlayType.WRITE_IMPLEMENTATION_PLAN,
        PlayType.SYSTEMATIC_DEBUGGING,
        PlayType.ISSUE_PICKUP,
        PlayType.CODE_REVIEW,
        PlayType.MERGE_PR,
        PlayType.RUN_QA,
    ):
        await resolver.resolve(play_type, state)
    elapsed_ms = (time.monotonic() - start) * 1000

    assert elapsed_ms < 100, f"Resolution took {elapsed_ms:.1f}ms, expected <100ms"


# ---------------------------------------------------------------------------
# Loop B fix — resolver tier-aware filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_code_review_skips_when_only_small_tier_idle() -> None:
    """A pending review is skipped when the only idle reviewer is small-tier.

    Tier eligibility (large-only for code_review — #254) still applies; small-tier
    agents never become candidates regardless of identity.
    """
    from unittest.mock import patch

    from agentshore.data.models import ReviewQueueRecord

    store = AsyncMock()
    row = ReviewQueueRecord(
        pr_number=77,
        session_id="sess-test",
        enqueued_at="2026-01-01T00:00:00Z",
        queue_id=1,
        author_label=None,
    )
    store.list_pending_reviews = AsyncMock(return_value=[row])
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    state = _make_state(
        agents=[
            _make_snapshot(
                "a-codex-small",
                agent_type=AgentType.CODEX,
                model_tier="small",
                github_identity="user_b",
            ),
        ]
    )

    with patch(
        "agentshore.plays.candidates.PlayCandidateService._github_pr_candidates",
        return_value=[],
    ):
        result = await resolver.resolve(PlayType.CODE_REVIEW, state)

    # No large reviewer is idle; small-tier never qualifies for code_review.
    assert result is None


@pytest.mark.asyncio
async def test_resolve_code_review_uses_large_cross_identity_reviewer() -> None:
    """A large-tier reviewer with a different identity is the chosen target.

    code_review is large-only (#254), so the reviewers are large tier; the
    same-identity-as-PR-author agent is still eliminated by anti-confirmation,
    leaving the cross-identity large reviewer.
    """
    from agentshore.data.models import ReviewQueueRecord

    store = AsyncMock()
    row = ReviewQueueRecord(
        pr_number=88,
        session_id="sess-test",
        enqueued_at="2026-01-01T00:00:00Z",
        queue_id=1,
        author_label=None,
    )
    store.list_pending_reviews = AsyncMock(return_value=[row])
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    pr_snap = PullRequestSnapshot(
        pr_number=88,
        title="PR 88",
        state="open",
        branch="feature/88",
        issue_number=None,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
        github_author="user_a",
    )
    state = OrchestratorState(
        session_id="sess-test",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[
            _make_snapshot(
                "a-claude",
                agent_type=AgentType.CLAUDE_CODE,
                model_tier="large",
                github_identity="user_a",
            ),
            _make_snapshot(
                "a-codex-lg",
                agent_type=AgentType.CODEX,
                model_tier="large",
                github_identity="user_b",
            ),
        ],
        open_issues=[],
        pull_requests=[pr_snap],
        budget=BudgetSnapshot(
            total_budget=5.0, spent=0.0, remaining=5.0, estimated_cost_per_play=0.1
        ),
    )
    result = await resolver.resolve(PlayType.CODE_REVIEW, state)
    assert result is not None
    assert result.pr_number == 88
    assert result.target_agent_id == "a-codex-lg"


# ---------------------------------------------------------------------------
# _pick_reviewer_for_pr unit tests
# ---------------------------------------------------------------------------


def _candidate(agent_id: str, github_identity: str | None) -> AgentSnapshot:
    return _make_snapshot(
        agent_id,
        agent_type=AgentType.CODEX,
        model_tier="medium",
        github_identity=github_identity,
    )


def test_pick_reviewer_returns_first_candidate_when_author_is_none() -> None:
    """Unknown author (pre-session PR, deleted account) accepts any reviewer."""
    from agentshore.plays.resolver import _pick_reviewer_for_pr

    candidates = [_candidate("a-1", "user_x"), _candidate("a-2", "user_y")]
    result = _pick_reviewer_for_pr(pr_github_author=None, candidates=candidates)
    assert result is not None
    assert result.agent_id == "a-1"


def test_pick_reviewer_skips_candidate_with_matching_identity() -> None:
    """A reviewer whose GH identity matches the PR author is skipped."""
    from agentshore.plays.resolver import _pick_reviewer_for_pr

    candidates = [_candidate("a-author", "user_a"), _candidate("a-other", "user_b")]
    result = _pick_reviewer_for_pr(pr_github_author="user_a", candidates=candidates)
    assert result is not None
    assert result.agent_id == "a-other"


def test_pick_reviewer_returns_none_when_all_share_author_identity() -> None:
    """When every candidate shares the PR author's identity, no reviewer is eligible."""
    from agentshore.plays.resolver import _pick_reviewer_for_pr

    candidates = [_candidate("a-1", "user_a"), _candidate("a-2", "user_a")]
    result = _pick_reviewer_for_pr(pr_github_author="user_a", candidates=candidates)
    assert result is None


def test_pick_reviewer_returns_none_when_no_candidates() -> None:
    from agentshore.plays.resolver import _pick_reviewer_for_pr

    assert _pick_reviewer_for_pr(pr_github_author="user_a", candidates=[]) is None
    assert _pick_reviewer_for_pr(pr_github_author=None, candidates=[]) is None


@pytest.mark.asyncio
async def test_resolve_code_review_pr_106_pattern() -> None:
    """Pre-session PR with unknown github_author + multiple idle reviewers.

    Previously the resolver pinned target_agent_type arbitrarily and looped
    on anti-confirmation. Now it pins target_agent_id to a deterministic
    idle reviewer and the executor's identity check confirms eligibility.
    """
    from agentshore.data.models import ReviewQueueRecord

    store = AsyncMock()
    row = ReviewQueueRecord(
        pr_number=106,
        session_id="sess-test",
        enqueued_at="2026-01-01T00:00:00Z",
        queue_id=1,
        author_label=None,
    )
    store.list_pending_reviews = AsyncMock(return_value=[row])
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.get_most_recent_branch = AsyncMock(return_value=None)
    resolver = ParameterResolver(store=store, manager=MagicMock(), cfg=_make_cfg())

    pr_snap = PullRequestSnapshot(
        pr_number=106,
        title="PR #106",
        state="open",
        branch="feat/106",
        issue_number=None,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
        github_author=None,
    )
    state = _make_state(
        agents=[
            _make_snapshot(
                "a-claude",
                agent_type=AgentType.CLAUDE_CODE,
                model_tier="large",
                github_identity="user_a",
            ),
            _make_snapshot(
                "a-codex",
                agent_type=AgentType.CODEX,
                model_tier="medium",
                github_identity="user_b",
            ),
        ],
        pull_requests=[pr_snap],
    )
    result = await resolver.resolve(PlayType.CODE_REVIEW, state)
    assert result is not None
    assert result.pr_number == 106
    # Sorted by (agent_type, agent_id): "claude_code:a-claude" sorts first.
    assert result.target_agent_id == "a-claude"
    assert result.target_agent_type is None


# ---------------------------------------------------------------------------
# END_AGENT resolution during drain (#30)
# ---------------------------------------------------------------------------


async def test_resolve_end_agent_during_drain_retires_recoverable_error_agent() -> None:
    """During drain, a recoverable-ERROR agent must be targeted for end_agent.

    Recovery (take_break) is masked during drain, so a recoverable-ERROR agent
    (e.g. a BUSY agent reaped mid-play -> exit 143 -> ERROR/"unknown") never
    reaches IDLE or recovery_exhausted. Without this it wedges drain forever
    (#30). The resolver must retire it directly, bypassing preconditions.
    """
    resolver = _make_resolver()
    errored = _make_snapshot(
        "wedged",
        status=AgentStatus.ERROR,
        last_error_class=ErrorClass.UNKNOWN,  # IS in RECOVERABLE_ERROR_CLASSES
    )
    state = dataclasses.replace(_make_state(agents=[errored]), session_state=SessionState.DRAINING)

    result = await resolver.resolve(PlayType.END_AGENT, state)

    assert result is not None
    assert result.agent_id == "wedged"
    assert result.bypass_preconditions is True


async def test_resolve_end_agent_outside_drain_leaves_recoverable_error_for_take_break() -> None:
    """Outside drain, a recoverable-ERROR agent is NOT auto-ended.

    The drain ERROR-sweep is deliberately drain-scoped: when the session is
    RUNNING the agent should still go through take_break recovery, so
    _resolve_end_agent must not select it (no IDLE agent => None).
    """
    resolver = _make_resolver()
    errored = _make_snapshot(
        "recovering",
        status=AgentStatus.ERROR,
        last_error_class=ErrorClass.UNKNOWN,
    )
    state = _make_state(agents=[errored])  # session_state defaults to RUNNING

    result = await resolver.resolve(PlayType.END_AGENT, state)

    assert result is None


async def test_resolve_end_agent_during_drain_prefers_error_over_idle() -> None:
    """A wedged ERROR agent is retired before idle agents during drain."""
    resolver = _make_resolver()
    idle = _make_snapshot("healthy", status=AgentStatus.IDLE)
    errored = _make_snapshot(
        "wedged", status=AgentStatus.ERROR, last_error_class=ErrorClass.UNKNOWN
    )
    state = dataclasses.replace(
        _make_state(agents=[idle, errored]), session_state=SessionState.DRAINING
    )

    result = await resolver.resolve(PlayType.END_AGENT, state)

    assert result is not None
    assert result.agent_id == "wedged"
