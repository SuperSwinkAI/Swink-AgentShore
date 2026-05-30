"""desktop-xi9d: bead closes between selector and dispatch.

Simulation that mirrors the production race window: PPO selects
``ISSUE_PICKUP`` for issue gh-N at tick T; between selector and dispatch,
``groom_backlog`` (or a separate session) closes the bead; the dispatch
should be dropped without consuming a PPO action.

Prior to desktop-xi9d the in-execute() live-graph check would catch this
but still spend a play_completed: skipped row. After the fix, the check
fires at param-resolve time and emits ``dispatch_revalidation_blocked``
instead — the PPO step is preserved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from agentshore.beads import BeadStatus, GraphTask, ProjectGraph
from agentshore.config import RuntimeConfig
from agentshore.core import Orchestrator
from agentshore.plays.base import PlayParams
from agentshore.plays.selector import FixedPlanSelector
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

if TYPE_CHECKING:
    from pathlib import Path


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


def _build_state(issue_number: int, *, total_plays: int = 5) -> OrchestratorState:
    """State the selector saw: bead still OPEN in the cached snapshot."""
    snapshot_task = GraphTask(
        bead_id=f"bd-{issue_number:04d}",
        title="Task",
        status=BeadStatus.OPEN,
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
        graph=ProjectGraph(tasks=[snapshot_task]),
        budget=BudgetSnapshot(
            total_budget=200.0,
            spent=0.0,
            remaining=200.0,
            estimated_cost_per_play=0.1,
        ),
    )


def _live_graph_closed(issue_number: int) -> ProjectGraph:
    """The bead-closed state that fires AFTER the selector picked it."""
    task = GraphTask(
        bead_id=f"bd-{issue_number:04d}",
        title="Task",
        status=BeadStatus.CLOSED,
        external_ref=f"gh-{issue_number}",
        issue_number=issue_number,
    )
    return ProjectGraph(tasks=[task])


def _live_graph_in_progress(issue_number: int) -> ProjectGraph:
    task = GraphTask(
        bead_id=f"bd-{issue_number:04d}",
        title="Task",
        status=BeadStatus.IN_PROGRESS,
        external_ref=f"gh-{issue_number}",
        issue_number=issue_number,
    )
    return ProjectGraph(tasks=[task])


@pytest.mark.asyncio
async def test_bead_in_progress_between_selector_and_dispatch_does_not_consume_action(
    tmp_path: Path,
) -> None:
    """Race window: bead is OPEN at selector time, IN_PROGRESS at dispatch time.

    Pin: dispatch is dropped via dispatch_revalidation_blocked, executor
    .execute() is never invoked, and the action-accounting (play row) does
    NOT advance.
    """
    selector = FixedPlanSelector([])
    orch = await Orchestrator.bootstrap(cfg=RuntimeConfig(), repo_root=tmp_path, selector=selector)
    state = _build_state(issue_number=123)
    params = PlayParams(issue_number=123)
    live_graph = _live_graph_in_progress(123)

    async with orch:
        with (
            patch("agentshore.beads.load_graph", new=AsyncMock(return_value=live_graph)),
            patch.object(orch._executor, "execute", new=AsyncMock()) as execute,
            patch("agentshore.core._logger.warning") as warning,
        ):
            dispatched = await orch._dispatch_play(
                PlayType.ISSUE_PICKUP, params, state, revalidate=True
            )

        history = await orch._store.get_play_history(orch._session_id)

    assert dispatched is False, "in_progress bead should block dispatch"
    execute.assert_not_awaited()
    assert history == []

    revalidation_warned = [
        call for call in warning.call_args_list if call.args == ("dispatch_revalidation_blocked",)
    ]
    assert revalidation_warned, "dispatch_revalidation_blocked must fire"
    skipped_warned = [call for call in warning.call_args_list if call.args == ("play_skipped",)]
    assert not skipped_warned, "must not double-charge via play_skipped"


@pytest.mark.asyncio
async def test_bead_closed_allows_dispatch_github_is_canonical(
    tmp_path: Path,
) -> None:
    """GitHub is canonical: a closed bead with an open GitHub issue should
    NOT block dispatch."""
    selector = FixedPlanSelector([])
    orch = await Orchestrator.bootstrap(cfg=RuntimeConfig(), repo_root=tmp_path, selector=selector)
    live_graph = _live_graph_closed(123)

    async with orch:
        with patch("agentshore.beads.load_graph", new=AsyncMock(return_value=live_graph)):
            reason = await orch._refresh_live_graph_for_issue(PlayType.ISSUE_PICKUP, 123)

    assert reason is None


@pytest.mark.asyncio
async def test_repeated_race_eventually_engages_skip_circuit_breaker(
    tmp_path: Path,
) -> None:
    """desktop-xi9d: the existing skip-circuit-breaker still graduates an
    issue to a precondition cooldown after _SKIP_CIRCUIT_THRESHOLD races."""
    from agentshore.plays.skill_backed.issue_pickup import _SKIP_CIRCUIT_THRESHOLD

    selector = FixedPlanSelector([])
    orch = await Orchestrator.bootstrap(cfg=RuntimeConfig(), repo_root=tmp_path, selector=selector)
    live_graph = _live_graph_in_progress(456)

    async with orch:
        issue_pickup = orch._registry.get(PlayType.ISSUE_PICKUP)
        with patch("agentshore.beads.load_graph", new=AsyncMock(return_value=live_graph)):
            for _ in range(_SKIP_CIRCUIT_THRESHOLD):
                await orch._refresh_live_graph_for_issue(PlayType.ISSUE_PICKUP, 456)
        on_cooldown = dict(getattr(issue_pickup, "_skip_until", {}))

    assert 456 in on_cooldown, (
        f"issue 456 should be on cooldown after {_SKIP_CIRCUIT_THRESHOLD} races, "
        f"got _skip_until={on_cooldown!r}"
    )


@pytest.mark.asyncio
async def test_open_bead_does_not_drive_skip_streak(tmp_path: Path) -> None:
    """desktop-xi9d: an OPEN bead in the live graph keeps the streak at 0."""
    selector = FixedPlanSelector([])
    orch = await Orchestrator.bootstrap(cfg=RuntimeConfig(), repo_root=tmp_path, selector=selector)
    open_task = GraphTask(
        bead_id="bd-0789",
        title="Task",
        status=BeadStatus.OPEN,
        external_ref="gh-789",
        issue_number=789,
    )
    live_graph = ProjectGraph(tasks=[open_task])

    async with orch:
        issue_pickup = orch._registry.get(PlayType.ISSUE_PICKUP)
        with patch("agentshore.beads.load_graph", new=AsyncMock(return_value=live_graph)):
            reason = await orch._refresh_live_graph_for_issue(PlayType.ISSUE_PICKUP, 789)
        streaks_in_context = dict(getattr(issue_pickup, "_skip_streaks", {}))

    assert reason is None
    assert 789 not in streaks_in_context
