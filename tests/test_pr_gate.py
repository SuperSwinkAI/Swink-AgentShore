"""Tests for pre-session PR gate + unblock_pr collision fix (v0.8.3)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agentshore.config import TrustedIdsConfig
from agentshore.data.models import PullRequestRecord
from agentshore.plays.resolver import ParameterResolver
from agentshore.plays.skill_backed.unblock_pr import UnblockPrPlay
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pr_snapshot(
    pr_number: int, *, state: str = "open", blocked: bool = False
) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        pr_number=pr_number,
        title=f"PR #{pr_number}",
        state=state,
        branch=f"branch-{pr_number}",
        issue_number=None,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=blocked,
        blocked_reasons=[],
    )


def _make_agent_snapshot(
    *,
    agent_id: str = "agent-1",
    status: AgentStatus = AgentStatus.IDLE,
    current_play_type: PlayType | None = None,
    current_play_pr_number: int | None = None,
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CLAUDE_CODE,
        status=status,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
        current_play_type=current_play_type,
        current_play_pr_number=current_play_pr_number,
    )


def _make_issue() -> IssueSnapshot:
    return IssueSnapshot(
        issue_number=1,
        title="Test issue",
        state="OPEN",
        priority=None,
        labels=[],
        source=None,
    )


def _make_resolver(*, open_prs: list[PullRequestRecord] | None = None) -> ParameterResolver:
    mock_store = AsyncMock()
    mock_store.list_open_pull_requests = AsyncMock(return_value=open_prs or [])
    mock_manager = MagicMock()
    mock_cfg = MagicMock()
    mock_cfg.intake.seed_paths = []
    return ParameterResolver(
        store=mock_store,
        manager=mock_manager,
        cfg=mock_cfg,
        github=None,
    )


def _make_pr_record(
    pr_number: int,
    *,
    mergeable: str = "CONFLICTING",
    labels: list[str] | None = None,
    github_author: str | None = "trusted",
) -> PullRequestRecord:
    return PullRequestRecord(
        pr_number=pr_number,
        session_id="test",
        state="open",
        created_at="2026-01-01T00:00:00Z",
        mergeable=mergeable,
        labels=labels or [],
        github_author=github_author,
    )


# ---------------------------------------------------------------------------
# _project_pull_requests: mergeable=CONFLICTING must set blocked=True
# ---------------------------------------------------------------------------


def test_project_pull_requests_conflicting_sets_blocked() -> None:
    """Regression: mergeable=CONFLICTING must propagate to blocked=True.

    The call to blocked_reasons() in _project_pull_requests was missing the
    mergeable= argument, so merge conflicts never surfaced as blocked PRs and
    unblock_pr's precondition always saw an empty available_blocked list.
    """
    from agentshore.core import Orchestrator

    records = [_make_pr_record(42, mergeable="CONFLICTING")]
    snapshots = Orchestrator._project_pull_requests(records)
    assert len(snapshots) == 1
    assert snapshots[0].blocked is True
    assert "merge_conflicts" in snapshots[0].blocked_reasons


def test_project_pull_requests_mergeable_pr_not_blocked() -> None:
    from agentshore.core import Orchestrator

    records = [_make_pr_record(43, mergeable="MERGEABLE")]
    snapshots = Orchestrator._project_pull_requests(records)
    assert snapshots[0].blocked is False
    assert "merge_conflicts" not in snapshots[0].blocked_reasons


# ---------------------------------------------------------------------------
# _phase_fetch_github snapshots open PRs at bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_ensure_labels_includes_required_workflow_labels() -> None:
    from agentshore.config import RuntimeConfig
    from agentshore.core.phases import _phase_ensure_labels

    mock_gh = AsyncMock()
    mock_gh.available = True
    mock_gh.ensure_labels = AsyncMock()

    await _phase_ensure_labels(
        gh=mock_gh,
        cfg=RuntimeConfig(trusted_ids=TrustedIdsConfig(github_logins=("trusted",))),
    )
    ensured = mock_gh.ensure_labels.await_args.args[0]
    assert ("agentshore/approved", "2ea44f") in ensured
    assert ("agentshore/blocked", "d73a4a") in ensured
    assert ("agentshore/disallowed", "b60205") in ensured
    assert ("agentshore/debug-needed", "d4c5f9") in ensured
    assert ("agentshore/root-cause-found", "5319e7") in ensured
    assert ("agentshore/manual-required", "fbca04") in ensured
    assert ("blocked", "d73a4a") in ensured


@pytest.mark.asyncio
async def test_phase_ensure_labels_skipped_when_gh_unavailable() -> None:
    from agentshore.config import RuntimeConfig
    from agentshore.core.phases import _phase_ensure_labels

    mock_gh = AsyncMock()
    mock_gh.available = False
    mock_gh.ensure_labels = AsyncMock()

    await _phase_ensure_labels(gh=mock_gh, cfg=RuntimeConfig())
    mock_gh.ensure_labels.assert_not_awaited()


# ---------------------------------------------------------------------------
# unblock_pr resolver: in-flight collision dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unblock_pr_resolver_skips_in_flight_pr() -> None:
    """unblock_pr resolution returns None when the only blocked PR is already in flight."""
    resolver = _make_resolver(open_prs=[_make_pr_record(20)])

    state = OrchestratorState(
        session_id="test",
        session_state=SessionState.RUNNING,
        total_plays=1,
        total_cost=0.01,
        agents=[
            _make_agent_snapshot(
                agent_id="agent-1",
                status=AgentStatus.BUSY,
                current_play_type=PlayType.UNBLOCK_PR,
                current_play_pr_number=20,
            )
        ],
    )
    result = await resolver._resolve_via_candidates(PlayType.UNBLOCK_PR, state)
    assert result is None, f"Expected None (all in flight); got {result}"


@pytest.mark.asyncio
async def test_unblock_pr_resolver_picks_other_blocked_pr() -> None:
    """unblock_pr resolution skips the in-flight PR and picks the other available one."""
    resolver = _make_resolver(open_prs=[_make_pr_record(20), _make_pr_record(21)])

    state = OrchestratorState(
        session_id="test",
        session_state=SessionState.RUNNING,
        total_plays=1,
        total_cost=0.01,
        agents=[
            _make_agent_snapshot(
                agent_id="agent-1",
                status=AgentStatus.BUSY,
                current_play_type=PlayType.UNBLOCK_PR,
                current_play_pr_number=20,
            )
        ],
    )
    result = await resolver._resolve_via_candidates(PlayType.UNBLOCK_PR, state)
    assert result is not None, "Expected PR #21 to be returned"
    assert result.pr_number == 21, f"Expected pr_number=21; got {result.pr_number}"


# ---------------------------------------------------------------------------
# UnblockPrPlay preconditions: all-in-flight defense
# ---------------------------------------------------------------------------


def test_unblock_pr_preconditions_fail_when_all_in_flight() -> None:
    """UnblockPrPlay preconditions block when every blocked PR is already in flight."""
    state = OrchestratorState(
        session_id="test",
        session_state=SessionState.RUNNING,
        total_plays=1,
        total_cost=0.01,
        agents=[
            _make_agent_snapshot(
                agent_id="agent-1",
                status=AgentStatus.BUSY,
                current_play_type=PlayType.UNBLOCK_PR,
                current_play_pr_number=20,
            ),
            _make_agent_snapshot(
                agent_id="agent-2",
                status=AgentStatus.IDLE,
            ),
        ],
        pull_requests=[_make_pr_snapshot(20, state="open", blocked=True)],
    )
    play = UnblockPrPlay()
    failures = play.preconditions(state)
    assert failures, "Expected preconditions to fail when all blocked PRs in flight"
    assert any("in flight" in f for f in failures), f"Expected 'in flight' message; got: {failures}"


@pytest.mark.asyncio
async def test_unblock_pr_resolver_github_fallback_excludes_exhausted() -> None:
    """Regression: exhausted PRs must be excluded from the GitHub fallback path.

    Before this fix, the GitHub blocked-PR fallback only excluded in-flight
    PRs, so a PR exhausted by the DB loop was immediately re-selected via the
    fallback, producing an infinite retry cycle.
    """
    resolver = _make_resolver(open_prs=[_make_pr_record(187)])
    # Simulate 3 failures (exhaustion threshold).
    for _ in range(3):
        resolver.record_unblock_pr_failure(187)

    state = OrchestratorState(
        session_id="test",
        session_state=SessionState.RUNNING,
        total_plays=10,
        total_cost=0.5,
        agents=[_make_agent_snapshot(agent_id="agent-1", status=AgentStatus.IDLE)],
    )
    result = await resolver._resolve_via_candidates(PlayType.UNBLOCK_PR, state)
    assert result is None, (
        f"Exhausted PR #187 should not be re-selected via GitHub fallback; got {result}"
    )


@pytest.mark.asyncio
async def test_unblock_pr_resolver_skips_manual_required_pr() -> None:
    resolver = _make_resolver(
        open_prs=[_make_pr_record(187, labels=["agentshore/manual-required"])]
    )
    state = OrchestratorState(
        session_id="test",
        session_state=SessionState.RUNNING,
        total_plays=10,
        total_cost=0.5,
        agents=[_make_agent_snapshot(agent_id="agent-1", status=AgentStatus.IDLE)],
    )

    result = await resolver._resolve_via_candidates(PlayType.UNBLOCK_PR, state)

    assert result is None


def test_unblock_pr_preconditions_pass_when_available_pr_exists() -> None:
    """UnblockPrPlay preconditions clear when there is a blocked PR not in flight."""
    state = OrchestratorState(
        session_id="test",
        session_state=SessionState.RUNNING,
        total_plays=1,
        total_cost=0.01,
        agents=[
            _make_agent_snapshot(
                agent_id="agent-1",
                status=AgentStatus.IDLE,
            ),
        ],
        pull_requests=[_make_pr_snapshot(20, state="open", blocked=True)],
    )
    play = UnblockPrPlay()
    failures = play.preconditions(state)
    assert not failures, f"Expected clear preconditions; got: {failures}"
