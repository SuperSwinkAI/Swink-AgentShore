"""Pin the param-resolve-time live-graph check (desktop-xi9d).

Background: ``IssuePickupPlay.execute`` used to do a final live-beads-graph
check after the PPO action had already been picked. When the bead had
closed between selection and execute, the play returned a partial-failure
outcome — but that still counted as a play_completed (with reason="skipped"),
which consumed a PPO action and contributed to the 89-skip stall observed
in session 2b8729bf.

The new contract: live-graph revalidation lives in
``_DispatchMixin._dispatch_revalidation_reason`` at param-resolve time.
When the live graph reports the bead is non-OPEN, the dispatch is dropped
via ``dispatch_revalidation_blocked`` (no play_completed row, no PPO action
consumed). The skip-circuit-breaker on ``IssuePickupPlay`` is still wired
in via ``_DispatchMixin._record_live_graph_skip`` so a repeated race on
the same issue still graduates to a precondition cooldown.
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


def _state_with_issue(issue_number: int = 42, *, graph: ProjectGraph | None = None) -> OrchestratorState:
    return OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=5,
        total_cost=0.0,
        agents=[_idle_agent()],
        open_issues=[_open_issue(issue_number)],
        graph=graph,
        budget=BudgetSnapshot(
            total_budget=200.0,
            spent=0.0,
            remaining=200.0,
            estimated_cost_per_play=0.1,
        ),
    )


def _live_graph(issue_number: int, status: BeadStatus) -> ProjectGraph:
    task = GraphTask(
        bead_id=f"bd-{issue_number:04d}",
        title=f"Task for #{issue_number}",
        status=status,
        external_ref=f"gh-{issue_number}",
        issue_number=issue_number,
    )
    return ProjectGraph(tasks=[task])


@pytest.mark.asyncio
async def test_live_graph_in_progress_blocks_dispatch_without_consuming_action(
    tmp_path: Path,
) -> None:
    """desktop-xi9d: in_progress bead at param-resolve time emits
    dispatch_revalidation_blocked and does NOT consume a PPO action."""
    selector = FixedPlanSelector([])
    orch = await Orchestrator.bootstrap(cfg=RuntimeConfig(), repo_root=tmp_path, selector=selector)

    state = _state_with_issue(issue_number=42, graph=_live_graph(42, BeadStatus.OPEN))
    params = PlayParams(issue_number=42)

    live_graph = _live_graph(42, BeadStatus.IN_PROGRESS)

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

    assert dispatched is False
    execute.assert_not_awaited()
    assert history == []
    warned = [call.args[0] for call in warning.call_args_list]
    assert "dispatch_revalidation_blocked" in warned


@pytest.mark.asyncio
async def test_live_graph_closed_allows_dispatch_github_is_canonical(
    tmp_path: Path,
) -> None:
    """GitHub is the canonical source of truth. A closed bead with an open
    GitHub issue should NOT block dispatch — the bead is stale."""
    selector = FixedPlanSelector([])
    orch = await Orchestrator.bootstrap(cfg=RuntimeConfig(), repo_root=tmp_path, selector=selector)

    live_graph = _live_graph(42, BeadStatus.CLOSED)

    async with orch:
        with patch("agentshore.beads.load_graph", new=AsyncMock(return_value=live_graph)):
            reason = await orch._refresh_live_graph_for_issue(PlayType.ISSUE_PICKUP, 42)

    assert reason is None


@pytest.mark.asyncio
async def test_live_graph_blocked_emits_selected_and_revalidated_timestamps(
    tmp_path: Path,
) -> None:
    """desktop-xi9d: dispatch_revalidation_blocked carries selected_at +
    revalidated_at timestamps so the race window is queryable."""
    selector = FixedPlanSelector([])
    orch = await Orchestrator.bootstrap(cfg=RuntimeConfig(), repo_root=tmp_path, selector=selector)

    state = _state_with_issue(issue_number=7, graph=_live_graph(7, BeadStatus.OPEN))
    params = PlayParams(issue_number=7)
    live_graph = _live_graph(7, BeadStatus.IN_PROGRESS)

    async with orch:
        with (
            patch("agentshore.beads.load_graph", new=AsyncMock(return_value=live_graph)),
            patch.object(orch._executor, "execute", new=AsyncMock()),
            patch("agentshore.core._logger.warning") as warning,
        ):
            dispatched = await orch._dispatch_play(
                PlayType.ISSUE_PICKUP, params, state, revalidate=True
            )

    assert dispatched is False
    revalidation_calls = [
        call for call in warning.call_args_list if call.args == ("dispatch_revalidation_blocked",)
    ]
    assert revalidation_calls, "expected dispatch_revalidation_blocked"
    kwargs = revalidation_calls[-1].kwargs
    assert "selected_at" in kwargs
    assert "revalidated_at" in kwargs
    assert "revalidation_window_seconds" in kwargs
    assert isinstance(kwargs["revalidation_window_seconds"], float)
    assert kwargs["revalidation_window_seconds"] >= 0.0
    # Race window for an in-process live-graph mock is always sub-tick.
    assert kwargs["revalidation_window_seconds"] < 1.0


@pytest.mark.asyncio
async def test_live_graph_open_lets_dispatch_proceed(tmp_path: Path) -> None:
    """desktop-xi9d: when the live graph still shows OPEN, dispatch goes through."""
    selector = FixedPlanSelector([])
    orch = await Orchestrator.bootstrap(cfg=RuntimeConfig(), repo_root=tmp_path, selector=selector)

    live_graph = _live_graph(11, BeadStatus.OPEN)

    async with orch:
        with (
            patch("agentshore.beads.load_graph", new=AsyncMock(return_value=live_graph)),
            patch.object(orch._executor, "execute", new=AsyncMock()),
            # Skip the action-mask path entirely so we test live-graph isolation.
            patch.object(
                orch,
                "_dispatch_revalidation_reason",
                wraps=orch._dispatch_revalidation_reason,
            ) as reval,
        ):
            # Force revalidate=False so we directly exercise live-graph refresh
            # via the helper itself, isolating it from the candidate-plan path.
            reason = await orch._refresh_live_graph_for_issue(PlayType.ISSUE_PICKUP, 11)

    assert reason is None
    reval.assert_not_called()


@pytest.mark.asyncio
async def test_live_graph_refresh_records_skip_on_play_instance(tmp_path: Path) -> None:
    """desktop-xi9d: an in_progress live-graph block bumps the skip-circuit-breaker on
    IssuePickupPlay so repeated races eventually cooldown the issue at
    preconditions time."""
    selector = FixedPlanSelector([])
    orch = await Orchestrator.bootstrap(cfg=RuntimeConfig(), repo_root=tmp_path, selector=selector)
    live_graph = _live_graph(99, BeadStatus.IN_PROGRESS)

    async with orch:
        assert orch._registry is not None, "registry must be wired by bootstrap"
        with patch("agentshore.beads.load_graph", new=AsyncMock(return_value=live_graph)):
            reason = await orch._refresh_live_graph_for_issue(PlayType.ISSUE_PICKUP, 99)
            issue_pickup = orch._registry.get(PlayType.ISSUE_PICKUP)
            skip_streaks_in_context = dict(getattr(issue_pickup, "_skip_streaks", {}))

    assert reason is not None
    assert skip_streaks_in_context.get(99, 0) == 1, (
        f"expected skip streak == 1, got {skip_streaks_in_context!r}"
    )


@pytest.mark.asyncio
async def test_pr_play_skips_live_graph_refresh(tmp_path: Path) -> None:
    """desktop-xi9d: PR plays do not consult beads — keep behaviour unchanged."""
    selector = FixedPlanSelector([])
    orch = await Orchestrator.bootstrap(cfg=RuntimeConfig(), repo_root=tmp_path, selector=selector)
    live_graph = _live_graph(99, BeadStatus.CLOSED)

    async with orch:
        with patch("agentshore.beads.load_graph", new=AsyncMock(return_value=live_graph)) as loader:
            reason = await orch._refresh_live_graph_for_issue(PlayType.CODE_REVIEW, 99)

    assert reason is None
    loader.assert_not_called()
