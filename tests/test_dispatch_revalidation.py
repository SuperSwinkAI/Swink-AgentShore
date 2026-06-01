"""Live-drift confirmation moved from dispatch revalidation to the authority.

Background: ``IssuePickupPlay.execute`` used to do a final live-beads-graph
check after the PPO action had already been picked. When the bead had closed
between selection and execute, the play returned a partial-failure outcome —
counted as a play_completed (reason="skipped"), consuming a PPO action and
contributing to the skip stalls observed in production.

A later iteration moved that check to a dispatch-time revalidation pass
(``_DispatchMixin._dispatch_revalidation_reason`` /
``_refresh_live_graph_for_issue``, emitting ``dispatch_revalidation_blocked``).

Eligibility refactor: that pass is gone too. ``EligibilityAuthority.confirm()``
is now the single source of truth — it does ONE live re-derivation of the
candidate plan from the freshly-refreshed ``state``. An issue whose bead flipped
to ``in_progress`` drops out of the live candidate set, so ``confirm`` returns
``valid=False`` and the selector cleanly re-picks: no plays-table skip row, no
RL experience sample. A closed bead with an open GitHub issue stays valid
(GitHub is canonical). PR-target plays are confirmed against the live PR set,
not the beads graph.
"""

from __future__ import annotations

import pytest

from agentshore.beads import BeadStatus, GraphTask, ProjectGraph
from agentshore.plays.base import PlayParams
from agentshore.plays.registry import build_default_registry
from agentshore.rl.eligibility import EligibilityAuthority
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    BudgetSnapshot,
    IssueSnapshot,
    OrchestratorState,
    PlayType,
    SessionState,
)


def _idle_agent(agent_id: str = "agent-1") -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.IDLE,
        context_size=10_000,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
        model_tier="large",
    )


def _open_issue(issue_number: int = 42) -> IssueSnapshot:
    return IssueSnapshot(
        issue_number=issue_number,
        title=f"Issue {issue_number}",
        state="open",
        priority=None,
        labels=[],
        source=None,
    )


def _graph(issue_number: int, status: BeadStatus) -> ProjectGraph:
    task = GraphTask(
        bead_id=f"bd-{issue_number:04d}",
        title=f"Task for #{issue_number}",
        status=status,
        external_ref=f"gh-{issue_number}",
        issue_number=issue_number,
    )
    return ProjectGraph(tasks=[task])


def _state(issue_number: int, status: BeadStatus) -> OrchestratorState:
    return OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=5,
        total_cost=0.0,
        agents=[_idle_agent()],
        open_issues=[_open_issue(issue_number)],
        graph=_graph(issue_number, status),
        budget=BudgetSnapshot(
            total_budget=200.0,
            spent=0.0,
            remaining=200.0,
            estimated_cost_per_play=0.1,
        ),
    )


@pytest.mark.asyncio
async def test_live_in_progress_invalidates_confirm_clean_repick() -> None:
    """in_progress bead at confirm time → valid=False (clean re-pick)."""
    state = _state(42, BeadStatus.IN_PROGRESS)
    authority = EligibilityAuthority(state, build_default_registry())

    verdict = await authority.confirm(PlayType.ISSUE_PICKUP, PlayParams(issue_number=42), state)

    assert verdict.valid is False
    assert verdict.reason is not None
    # Clean re-pick: the picked issue is no longer a live candidate.
    assert all(c.params.issue_number != 42 for c in verdict.candidates)


@pytest.mark.asyncio
async def test_live_closed_allows_confirm_github_is_canonical() -> None:
    """GitHub is canonical: a closed bead with an open GitHub issue stays valid."""
    state = _state(42, BeadStatus.CLOSED)
    authority = EligibilityAuthority(state, build_default_registry())

    verdict = await authority.confirm(PlayType.ISSUE_PICKUP, PlayParams(issue_number=42), state)

    assert verdict.valid is True
    assert verdict.reason is None


@pytest.mark.asyncio
async def test_live_open_lets_confirm_proceed() -> None:
    """When the live graph still shows OPEN, confirm passes."""
    state = _state(11, BeadStatus.OPEN)
    authority = EligibilityAuthority(state, build_default_registry())

    verdict = await authority.confirm(PlayType.ISSUE_PICKUP, PlayParams(issue_number=11), state)

    assert verdict.valid is True
    assert verdict.reason is None


@pytest.mark.asyncio
async def test_pr_play_confirms_against_live_pr_set_not_beads() -> None:
    """PR-target plays are confirmed against the live PR set, not the beads graph.

    A CODE_REVIEW pinned to a PR that no longer exists in the live plan is a
    clean re-pick; the beads graph status is irrelevant to PR confirmation.
    """
    # No PRs in state → CODE_REVIEW has no live candidate for PR #99.
    state = _state(99, BeadStatus.CLOSED)
    authority = EligibilityAuthority(state, build_default_registry())

    verdict = await authority.confirm(PlayType.CODE_REVIEW, PlayParams(pr_number=99), state)

    assert verdict.valid is False
    assert all(c.params.pr_number != 99 for c in verdict.candidates)
