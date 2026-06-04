"""Tests for MainDashboard screen — widget composition and message routing."""

from __future__ import annotations

from textual.app import App
from textual.widgets import Header

from agentshore.beads import ProjectGraph
from agentshore.state import (
    ActivePlay,
    AgentSnapshot,
    AgentStatus,
    AgentType,
    BudgetSnapshot,
    OrchestratorState,
    PlayOutcome,
    PlayType,
    SessionState,
    loop_level_for_streak,
)
from agentshore.ui.app import OrchestratorApp
from agentshore.ui.screens.dashboard import MainDashboard
from agentshore.ui.widgets.agent_panel import AgentPanel
from agentshore.ui.widgets.alert_bar import AlertBar
from agentshore.ui.widgets.alignment import AlignmentBars
from agentshore.ui.widgets.budget import BudgetWidget
from agentshore.ui.widgets.play_history import PlayHistoryTable
from agentshore.ui.widgets.rl_state import RLStateBar
from agentshore.ui.widgets.work_queue import WorkQueueSummary

# ---------------------------------------------------------------------------
# Test app
# ---------------------------------------------------------------------------


class DashboardTestApp(App[None]):
    """Minimal host app for dashboard tests."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(
    *,
    agents: list[AgentSnapshot] | None = None,
    graph: ProjectGraph | None = None,
    budget: BudgetSnapshot | None = None,
    total_plays: int = 0,
    total_cost: float = 0.0,
    streak: int = 0,
    last_play_type: PlayType | None = None,
) -> OrchestratorState:
    state = OrchestratorState(
        session_id="test-session",
        session_state=SessionState.RUNNING,
        total_plays=total_plays,
        total_cost=total_cost,
        agents=agents or [],
        graph=graph,
        budget=budget,
        same_type_failure_streak=streak,
        loop_level=loop_level_for_streak(streak),
    )
    state.last_play_type = last_play_type
    return state


def _make_agent(
    *,
    agent_id: str = "test-agent",
    status: AgentStatus = AgentStatus.IDLE,
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
    )


def _make_outcome(
    *,
    play_type: PlayType = PlayType.ISSUE_PICKUP,
    success: bool = True,
) -> PlayOutcome:
    return PlayOutcome(
        play_type=play_type,
        agent_id="agent-1",
        success=success,
        partial=False,
        duration_seconds=10.0,
        token_cost=100,
        dollar_cost=0.05,
        artifacts=[],
        alignment_delta=0.0,
    )


def _main_dashboard(app: DashboardTestApp | OrchestratorApp) -> MainDashboard:
    screen = app.screen
    assert isinstance(screen, MainDashboard)
    return screen


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_dashboard_composes_all_widgets() -> None:
    """All 7 widget IDs are present after the dashboard mounts."""
    app = DashboardTestApp()
    async with app.run_test() as pilot:
        app.push_screen(MainDashboard())
        await pilot.pause()
        expected_ids = [
            "alert-bar",
            "agent-panel",
            "play-history",
            "alignment",
            "budget",
            "work-queue",
            "rl-state",
        ]
        for wid in expected_ids:
            assert app.screen.query_one(f"#{wid}") is not None
        assert app.screen.query_one(Header) is not None


async def test_state_updated_routes_snapshot_to_widgets() -> None:
    """A StateUpdated snapshot is routed to each dashboard data widget."""
    app = DashboardTestApp()
    async with app.run_test() as pilot:
        app.push_screen(MainDashboard())
        await pilot.pause()

        budget = BudgetSnapshot(
            total_budget=10.0,
            spent=2.0,
            remaining=8.0,
            estimated_cost_per_play=0.5,
        )
        graph = ProjectGraph(global_closure_ratio=0.8, tasks_ready=2, tasks_total=4)
        state = _make_state(
            agents=[_make_agent(agent_id="a1")],
            graph=graph,
            budget=budget,
            total_plays=3,
        )
        state.active_play = ActivePlay(
            play_type=PlayType.RUN_QA,
            agent_id="a1",
            started_at="2026-01-01T00:00:00+00:00",
            play_id=22,
            pr_number=15,
            phase="testing",
        )

        screen = _main_dashboard(app)
        screen.on_orchestrator_app_state_updated(OrchestratorApp.StateUpdated(state))

        panel = screen.query_one("#agent-panel", AgentPanel)
        assert [agent.agent_id for agent in panel.agents] == ["a1"]
        assert panel.active_play is not None
        assert panel.active_play.play_id == 22

        alignment = screen.query_one("#alignment", AlignmentBars)
        assert alignment._graph is graph

        budget_widget = screen.query_one("#budget", BudgetWidget)
        assert budget_widget.budget is budget

        work_queue = screen.query_one("#work-queue", WorkQueueSummary)
        assert work_queue.state is state


async def test_agent_changed_updates_latest_state_and_panel() -> None:
    """Eager AgentChanged events update the app cache and dashboard panel."""
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        app.push_screen(MainDashboard())
        await pilot.pause()
        state = _make_state(agents=[_make_agent(agent_id="a1", status=AgentStatus.BUSY)])
        app.post_message(OrchestratorApp.StateUpdated(state))
        await pilot.pause()

        app.post_message(OrchestratorApp.AgentChanged("a1", AgentStatus.IDLE))
        await pilot.pause()

        assert app._latest_state is not None
        assert app._latest_state.agents[0].status is AgentStatus.IDLE
        panel = app.screen.query_one("#agent-panel", AgentPanel)
        assert panel.agents[0].status is AgentStatus.IDLE


async def test_play_started_routes_to_active_play() -> None:
    """Posting PlayStarted sets the agent panel's transient active play."""
    from agentshore.plays.base import PlayParams

    app = DashboardTestApp()
    async with app.run_test() as pilot:
        app.push_screen(MainDashboard())
        await pilot.pause()
        app.screen.post_message(OrchestratorApp.PlayStarted(PlayType.ISSUE_PICKUP, PlayParams()))
        await pilot.pause()
        widget = app.screen.query_one("#agent-panel", AgentPanel)
        assert widget.active_play is not None
        assert widget.active_play.play_type == PlayType.ISSUE_PICKUP


async def test_play_completed_clears_active_and_adds_history() -> None:
    """Posting PlayCompleted clears the agent hint and adds a history row."""
    app = DashboardTestApp()
    async with app.run_test() as pilot:
        app.push_screen(MainDashboard())
        await pilot.pause()
        panel = app.screen.query_one("#agent-panel", AgentPanel)
        from agentshore.plays.base import PlayParams

        panel.set_play_started(PlayType.ISSUE_PICKUP, PlayParams())
        outcome = _make_outcome()
        app.screen.post_message(OrchestratorApp.PlayCompleted(outcome))
        await pilot.pause()
        assert panel.active_play is None
        table = app.screen.query_one("#play-history", PlayHistoryTable)
        assert table.row_count == 1


async def test_alert_events_show_alert_bar() -> None:
    """Feedback and pause events surface through the dashboard alert bar."""
    app = DashboardTestApp()
    async with app.run_test() as pilot:
        app.push_screen(MainDashboard())
        await pilot.pause()

        screen = _main_dashboard(app)
        screen.on_orchestrator_app_feedback_requested(
            OrchestratorApp.FeedbackRequested("Need input")
        )
        alert = screen.query_one("#alert-bar", AlertBar)
        assert alert.display is True
        assert "Need input" in alert._message

        screen.on_orchestrator_app_session_paused(OrchestratorApp.SessionPaused("Budget low"))
        assert alert.display is True
        assert "Budget low" in alert._message


async def test_layout_standard_class() -> None:
    """Resizing to >=100 width applies the 'layout-standard' CSS class."""
    app = DashboardTestApp()
    async with app.run_test(size=(120, 40)) as pilot:
        app.push_screen(MainDashboard())
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, MainDashboard)
        assert "layout-standard" in screen.classes
        table = app.screen.query_one("#play-history", PlayHistoryTable)
        assert table.visible_row_limit == 5


async def test_layout_narrow_class() -> None:
    """Resizing to 60-99 width applies the 'layout-narrow' CSS class."""
    app = DashboardTestApp()
    async with app.run_test(size=(80, 40)) as pilot:
        app.push_screen(MainDashboard())
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, MainDashboard)
        assert "layout-narrow" in screen.classes
        table = app.screen.query_one("#play-history", PlayHistoryTable)
        assert table.visible_row_limit == 3


async def test_agent_subprocess_events_update_pid_registry() -> None:
    """Subprocess spawn and exit events maintain the dashboard PID registry."""
    app = DashboardTestApp()
    async with app.run_test() as pilot:
        app.push_screen(MainDashboard())
        await pilot.pause()
        screen = _main_dashboard(app)

        screen.on_orchestrator_app_agent_subprocess_spawned(
            OrchestratorApp.AgentSubprocessSpawned(
                agent_id="agent-y",
                agent_type=AgentType.CLAUDE_CODE,
                pid=99999,
            )
        )
        assert screen._subprocess_pids == {"agent-y": 99999}

        screen.on_orchestrator_app_agent_subprocess_exited(
            OrchestratorApp.AgentSubprocessExited(
                agent_id="agent-y",
                agent_type=AgentType.CLAUDE_CODE,
                pid=99999,
                exit_code=0,
            )
        )
        assert screen._subprocess_pids == {}


async def test_loop_detection_warning_at_streak_3() -> None:
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        app.push_screen(MainDashboard())
        await pilot.pause()

        state = _make_state(streak=3, last_play_type=PlayType.ISSUE_PICKUP)
        app.post_message(OrchestratorApp.StateUpdated(state))
        await pilot.pause()

        screen = _main_dashboard(app)
        alert = screen.query_one("#alert-bar", AlertBar)
        assert alert.display is False
        rl_state = screen.query_one("#rl-state", RLStateBar)
        assert "Loop: Issue Pickup failed 3x" in rl_state.render()
        assert rl_state.has_class("loop--warning")


async def test_loop_detection_force_at_streak_5() -> None:
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        app.push_screen(MainDashboard())
        await pilot.pause()

        state = _make_state(streak=5, last_play_type=PlayType.ISSUE_PICKUP)
        app.post_message(OrchestratorApp.StateUpdated(state))
        await pilot.pause()

        screen = _main_dashboard(app)
        alert = screen.query_one("#alert-bar", AlertBar)
        assert alert.display is False
        rl_state = screen.query_one("#rl-state", RLStateBar)
        assert "Loop: Issue Pickup blocked (5x fail)" in rl_state.render()
        assert rl_state.has_class("loop--force")


async def test_loop_detection_escalation_at_streak_7() -> None:
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        app.push_screen(MainDashboard())
        await pilot.pause()

        state = _make_state(streak=7, last_play_type=PlayType.ISSUE_PICKUP)
        app.post_message(OrchestratorApp.StateUpdated(state))
        await pilot.pause()

        screen = _main_dashboard(app)
        alert = screen.query_one("#alert-bar", AlertBar)
        assert alert.display is True
        assert "LOOP DETECTED — Issue Pickup failed 7x" in alert.render()
        assert alert.has_class("alert--loop")


async def test_loop_detection_resets_when_streak_drops() -> None:
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        app.push_screen(MainDashboard())
        await pilot.pause()

        app.post_message(
            OrchestratorApp.StateUpdated(
                _make_state(streak=7, last_play_type=PlayType.ISSUE_PICKUP)
            )
        )
        await pilot.pause()
        app.post_message(
            OrchestratorApp.StateUpdated(
                _make_state(streak=0, last_play_type=PlayType.ISSUE_PICKUP)
            )
        )
        await pilot.pause()

        screen = _main_dashboard(app)
        alert = screen.query_one("#alert-bar", AlertBar)
        assert alert.display is False
        rl_state = screen.query_one("#rl-state", RLStateBar)
        assert not rl_state.has_class("loop--warning")
        assert not rl_state.has_class("loop--force")


# Loop-alert 'o' override and the 'r'/'o' delegate tests previously exercised the
# PlayOverrideScreen / loop-alert flow, both intentionally deleted in commits
# 0a1e034 (dead Override binding removed) and fe9040c (PlayOverrideScreen and all
# UI override plumbing removed). Their corresponding tests are gone with them.
