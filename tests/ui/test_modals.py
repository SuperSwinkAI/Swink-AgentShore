"""Tests for TUI modal screens: HelpOverlay, GoalsScreen, AgentDetailScreen."""

from __future__ import annotations

from textual.app import App

from agentshore.beads import BeadStatus, EpicStatus, GraphTask, ProjectGraph
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    IssueSnapshot,
    OrchestratorState,
    SessionState,
)
from agentshore.ui.screens.agent_detail import AgentDetailScreen
from agentshore.ui.screens.goals import GoalsScreen
from agentshore.ui.screens.help import HelpOverlay
from agentshore.ui.screens.issues import IssueWorkQueueScreen

# ---------------------------------------------------------------------------
# Minimal host app
# ---------------------------------------------------------------------------


class ModalTestApp(App[None]):
    """A minimal app that can push modal screens."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_graph(closure_ratio: float = 0.7) -> ProjectGraph:
    return ProjectGraph(
        epics=[
            EpicStatus(
                bead_id="epic-1",
                title="Auth",
                total_tasks=4,
                closed_tasks=round(closure_ratio * 4),
                closure_ratio=closure_ratio,
            )
        ],
        tasks=[
            GraphTask(
                bead_id="task-1",
                title="Login",
                status=BeadStatus.OPEN,
                parent_id="epic-1",
                epic_id="epic-1",
                epic_title="Auth",
                external_ref="gh-1",
                issue_number=1,
                ready=True,
            )
        ],
        global_closure_ratio=closure_ratio,
        tasks_ready=1,
        tasks_total=4,
    )


def _make_agent(
    agent_id: str = "claude-code",
    status: AgentStatus = AgentStatus.IDLE,
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CLAUDE_CODE,
        status=status,
        context_size=45000,
        total_cost=0.42,
        total_tokens=312000,
        tasks_completed=4,
        tasks_failed=0,
    )


# ---------------------------------------------------------------------------
# HelpOverlay tests
# ---------------------------------------------------------------------------


async def test_help_overlay_composes() -> None:
    """HelpOverlay renders and contains help text."""
    app = ModalTestApp()
    async with app.run_test() as pilot:
        app.push_screen(HelpOverlay())
        await pilot.pause()
        help_text = app.screen.query_one("#help-text")
        rendered = str(help_text.render())
        assert "End session" in rendered
        assert "Agent detail" in rendered


async def test_help_overlay_dismiss_escape() -> None:
    """Pressing Escape dismisses the HelpOverlay."""
    app = ModalTestApp()
    async with app.run_test() as pilot:
        app.push_screen(HelpOverlay())
        await pilot.pause()
        assert len(app.screen_stack) == 2  # default + modal
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1


# ---------------------------------------------------------------------------
# GoalsScreen tests
# ---------------------------------------------------------------------------


async def test_goals_screen_renders_graph() -> None:
    """GoalsScreen with a graph renders global closure info."""
    graph = _make_graph(0.8)
    app = ModalTestApp()
    async with app.run_test() as pilot:
        app.push_screen(GoalsScreen(graph))
        await pilot.pause()
        text = app.screen.query("Static")
        all_text = " ".join(str(w.render()) for w in text)
        assert "Global closure" in all_text
        assert "Auth" in all_text
        assert "Ready: 1" in all_text


async def test_goals_screen_no_graph() -> None:
    """GoalsScreen with None graph shows placeholder text."""
    app = ModalTestApp()
    async with app.run_test() as pilot:
        app.push_screen(GoalsScreen(None))
        await pilot.pause()
        text = app.screen.query("Static")
        all_text = " ".join(str(w.render()) for w in text)
        assert "No beads graph detected" in all_text


async def test_goals_screen_dismiss() -> None:
    """Pressing Escape dismisses the GoalsScreen."""
    app = ModalTestApp()
    async with app.run_test() as pilot:
        app.push_screen(GoalsScreen(None))
        await pilot.pause()
        assert len(app.screen_stack) == 2
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1


# ---------------------------------------------------------------------------
# IssueWorkQueueScreen tests
# ---------------------------------------------------------------------------


async def test_issue_work_queue_renders_issue_metadata() -> None:
    """Issue queue shows URL and timestamps from the richer IPC/state contract."""
    state = OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        open_issues=[
            IssueSnapshot(
                issue_number=47,
                title="Implement budget guard",
                state="open",
                priority=1,
                labels=["backend"],
                source="github",
                url="https://github.com/org/repo/issues/47",
                created_at="2026-01-01T00:00:00+00:00",
                closed_at=None,
                bead_epic_title="Budget",
                bead_status="open",
                bead_ready=True,
            )
        ],
    )
    app = ModalTestApp()
    async with app.run_test() as pilot:
        app.push_screen(IssueWorkQueueScreen(state))
        await pilot.pause()
        text = app.screen.query_one("#issues-work-queue")
        rendered = str(text.render())
        assert "TO DO (1)" in rendered
        assert "https://github.com/org/repo/issues/47" in rendered
        assert "created=2026-01-01" in rendered


# ---------------------------------------------------------------------------
# AgentDetailScreen tests
# ---------------------------------------------------------------------------


async def test_agent_detail_renders_agent() -> None:
    """AgentDetailScreen with one agent renders the agent_id."""
    agent = _make_agent(agent_id="my-agent-1")
    app = ModalTestApp()
    async with app.run_test() as pilot:
        app.push_screen(AgentDetailScreen([agent]))
        await pilot.pause()
        info = app.screen.query_one("#agent-info")
        rendered = str(info.render())
        assert "my-agent-1" in rendered


async def test_agent_detail_switch_agents() -> None:
    """Pressing Right switches to the next agent."""
    agents = [
        _make_agent(agent_id="agent-alpha"),
        _make_agent(agent_id="agent-beta"),
    ]
    app = ModalTestApp()
    async with app.run_test() as pilot:
        app.push_screen(AgentDetailScreen(agents))
        await pilot.pause()
        info = app.screen.query_one("#agent-info")
        rendered = str(info.render())
        assert "agent-alpha" in rendered

        await pilot.press("right")
        await pilot.pause()
        rendered = str(info.render())
        assert "agent-beta" in rendered


async def test_agent_detail_empty() -> None:
    """AgentDetailScreen with no agents shows placeholder."""
    app = ModalTestApp()
    async with app.run_test() as pilot:
        app.push_screen(AgentDetailScreen([]))
        await pilot.pause()
        info = app.screen.query_one("#agent-info")
        rendered = str(info.render())
        assert "No agents available" in rendered
