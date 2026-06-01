"""Bead closes/flips between selector and confirm (eligibility refactor).

Mirrors the production race window: PPO selects ``ISSUE_PICKUP`` for issue
gh-N at tick T; between selector and dispatch, ``groom_backlog`` (or a separate
session) flips the bead to ``in_progress``; the play should be cleanly
re-picked without consuming a PPO action.

Eligibility refactor: the dispatch-time live-graph revalidation pass
(``_refresh_live_graph_for_issue`` / ``_dispatch_revalidation_reason`` /
``dispatch_revalidation_blocked``) is gone. The single source of truth is now
``EligibilityAuthority.confirm()``: it does one live re-derivation of the
candidate plan from the freshly-refreshed ``state``. An issue whose bead has
flipped to ``in_progress`` drops out of the live ISSUE_PICKUP candidate set, so
``confirm`` returns ``valid=False`` and the selector cleanly re-picks — never a
plays-table skip row, never an RL experience sample. A closed bead with an open
GitHub issue stays valid because GitHub is canonical.
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


def _idle_agent() -> AgentSnapshot:
    return AgentSnapshot(
        agent_id="agent-1",
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.IDLE,
        context_size=10_000,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
        model_tier="large",
    )


def _state(issue_number: int, status: BeadStatus, *, total_plays: int = 5) -> OrchestratorState:
    task = GraphTask(
        bead_id=f"bd-{issue_number:04d}",
        title="Task",
        status=status,
        external_ref=f"gh-{issue_number}",
        issue_number=issue_number,
    )
    return OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=total_plays,
        total_cost=0.0,
        agents=[_idle_agent()],
        open_issues=[
            IssueSnapshot(
                issue_number=issue_number,
                title=f"Issue {issue_number}",
                state="open",
                priority=None,
                labels=[],
                source=None,
            )
        ],
        graph=ProjectGraph(tasks=[task]),
        budget=BudgetSnapshot(
            total_budget=200.0,
            spent=0.0,
            remaining=200.0,
            estimated_cost_per_play=0.1,
        ),
    )


@pytest.mark.asyncio
async def test_bead_in_progress_between_selector_and_confirm_is_clean_repick() -> None:
    """Race window: bead OPEN at selector time, IN_PROGRESS at confirm time.

    confirm() sees the issue dropped from the live ISSUE_PICKUP candidate set
    and returns valid=False — the caller cleanly re-picks. No skip row, no RL
    step.
    """
    state = _state(123, BeadStatus.IN_PROGRESS)
    authority = EligibilityAuthority(state, build_default_registry())

    verdict = await authority.confirm(PlayType.ISSUE_PICKUP, PlayParams(issue_number=123), state)

    assert verdict.valid is False, "in_progress bead must invalidate the picked issue"
    assert verdict.reason is not None
    assert "in_progress" in str(verdict.reason)
    # The clean-re-pick contract: the live candidate set no longer carries 123.
    assert all(c.params.issue_number != 123 for c in verdict.candidates)


@pytest.mark.asyncio
async def test_bead_closed_stays_valid_github_is_canonical() -> None:
    """GitHub is canonical: a closed bead with an open GitHub issue stays valid."""
    state = _state(123, BeadStatus.CLOSED)
    authority = EligibilityAuthority(state, build_default_registry())

    verdict = await authority.confirm(PlayType.ISSUE_PICKUP, PlayParams(issue_number=123), state)

    assert verdict.valid is True
    assert verdict.reason is None


@pytest.mark.asyncio
async def test_open_bead_confirms_valid() -> None:
    """An OPEN bead still in the live plan confirms valid (no drift)."""
    state = _state(789, BeadStatus.OPEN)
    authority = EligibilityAuthority(state, build_default_registry())

    verdict = await authority.confirm(PlayType.ISSUE_PICKUP, PlayParams(issue_number=789), state)

    assert verdict.valid is True
    assert verdict.reason is None
    assert any(c.params.issue_number == 789 for c in verdict.candidates)
