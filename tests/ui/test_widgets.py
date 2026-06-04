"""Tests for the TUI data-display widgets: AgentPanel and PlayHistoryTable."""

from __future__ import annotations

from textual.app import App, ComposeResult

from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    PlayOutcome,
    PlayType,
)
from agentshore.ui.widgets.agent_panel import AgentPanel
from agentshore.ui.widgets.play_history import PlayHistoryTable

# ---------------------------------------------------------------------------
# Minimal host apps
# ---------------------------------------------------------------------------


class _AgentPanelApp(App[None]):
    def compose(self) -> ComposeResult:
        yield AgentPanel()


class _PlayHistoryApp(App[None]):
    def compose(self) -> ComposeResult:
        yield PlayHistoryTable()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(
    *,
    agent_id: str = "test-agent",
    status: AgentStatus = AgentStatus.IDLE,
    context_size: int = 0,
    total_cost: float = 0.0,
    current_play_type: PlayType | None = None,
    current_play_issue_number: int | None = None,
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CLAUDE_CODE,
        status=status,
        context_size=context_size,
        total_cost=total_cost,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
        current_play_type=current_play_type,
        current_play_issue_number=current_play_issue_number,
    )


def _make_outcome(
    *,
    play_type: PlayType = PlayType.ISSUE_PICKUP,
    agent_id: str | None = "agent-1",
    success: bool = True,
    dollar_cost: float = 0.05,
    duration_seconds: float = 10.0,
    play_id: int | None = None,
) -> PlayOutcome:
    return PlayOutcome(
        play_type=play_type,
        agent_id=agent_id,
        success=success,
        partial=False,
        duration_seconds=duration_seconds,
        token_cost=100,
        dollar_cost=dollar_cost,
        artifacts=[],
        alignment_delta=0.0,
        play_id=play_id,
    )


# ---------------------------------------------------------------------------
# AgentPanel tests
# ---------------------------------------------------------------------------


async def test_agent_panel_renders_idle_symbol() -> None:
    """IDLE agent produces the ● symbol in rendered output."""
    app = _AgentPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.update_agents([_make_agent(status=AgentStatus.IDLE)])
        await pilot.pause()
        assert "●" in panel.render()


async def test_agent_panel_renders_error_symbol() -> None:
    """ERROR agent produces the ✕ symbol in rendered output."""
    app = _AgentPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.update_agents([_make_agent(status=AgentStatus.ERROR)])
        await pilot.pause()
        assert "✕" in panel.render()


async def test_agent_panel_renders_busy_symbol() -> None:
    """BUSY agent produces the ◉ symbol in rendered output."""
    app = _AgentPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.update_agents([_make_agent(status=AgentStatus.BUSY)])
        await pilot.pause()
        assert "◉" in panel.render()


async def test_agent_panel_renders_terminated_symbol() -> None:
    """TERMINATED agent produces the — symbol in rendered output."""
    app = _AgentPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.update_agents([_make_agent(status=AgentStatus.TERMINATED)])
        await pilot.pause()
        assert "—" in panel.render()


async def test_agent_panel_empty_shows_no_agents() -> None:
    """Empty agent list renders the 'No agents' placeholder."""
    app = _AgentPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.update_agents([])
        await pilot.pause()
        assert "No agents" in panel.render()


async def test_agent_panel_shows_agent_id() -> None:
    """The agent_id appears in the rendered row."""
    app = _AgentPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.update_agents([_make_agent(agent_id="myagent")])
        await pilot.pause()
        assert "myagent" in panel.render()


async def test_agent_panel_shows_compact_context_size() -> None:
    """context_size is shown as a compact count in the dense agent row."""
    app = _AgentPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.update_agents([_make_agent(context_size=8192)])
        await pilot.pause()
        rendered = panel.render()
        assert "8.2k" in rendered


async def test_agent_panel_shows_active_play_target() -> None:
    """Current play and target fields appear in the dense agent row."""
    app = _AgentPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.update_agents(
            [
                _make_agent(
                    status=AgentStatus.BUSY,
                    current_play_type=PlayType.ISSUE_PICKUP,
                    current_play_issue_number=47,
                )
            ]
        )
        await pilot.pause()
        rendered = panel.render()
        assert "issue_pickup" in rendered
        assert "#47" in rendered


async def test_agent_panel_shows_cost() -> None:
    """The formatted dollar cost appears in the rendered row."""
    app = _AgentPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.update_agents([_make_agent(total_cost=1.234)])
        await pilot.pause()
        assert "$1.234" in panel.render()


async def test_agent_panel_multiple_agents() -> None:
    """Multiple agents all appear in rendered output."""
    app = _AgentPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        panel.update_agents(
            [
                _make_agent(agent_id="agent-a"),
                _make_agent(agent_id="agent-b"),
            ]
        )
        await pilot.pause()
        rendered = panel.render()
        assert "agent-a" in rendered
        assert "agent-b" in rendered


# ---------------------------------------------------------------------------
# PlayHistoryTable tests
# ---------------------------------------------------------------------------


async def test_play_history_adds_row() -> None:
    """Adding one outcome results in row_count == 1."""
    app = _PlayHistoryApp()
    async with app.run_test() as pilot:
        table = app.query_one(PlayHistoryTable)
        table.add_play_row(_make_outcome())
        await pilot.pause()
        assert table.row_count == 1


async def test_play_history_multiple_rows() -> None:
    """Adding three outcomes results in row_count == 3."""
    app = _PlayHistoryApp()
    async with app.run_test() as pilot:
        table = app.query_one(PlayHistoryTable)
        for _ in range(3):
            table.add_play_row(_make_outcome())
        await pilot.pause()
        assert table.row_count == 3


async def test_play_history_limits_visible_rows_to_recent_five() -> None:
    """The main dashboard history only renders the five most recent rows."""
    app = _PlayHistoryApp()
    async with app.run_test() as pilot:
        table = app.query_one(PlayHistoryTable)
        for play_id in range(7):
            table.add_play_row(_make_outcome(play_id=play_id))
        await pilot.pause()

        assert table.row_count == 5
        assert "2" in table.get_row_at(0)
        assert "6" in table.get_row_at(4)


async def test_play_history_can_use_narrow_limit() -> None:
    """The responsive dashboard can reduce visible history to three rows."""
    app = _PlayHistoryApp()
    async with app.run_test() as pilot:
        table = app.query_one(PlayHistoryTable)
        for play_id in range(5):
            table.add_play_row(_make_outcome(play_id=play_id))
        table.set_visible_row_limit(3)
        await pilot.pause()

        assert table.row_count == 3
        assert table.visible_row_limit == 3
        assert "2" in table.get_row_at(0)
        assert "4" in table.get_row_at(2)


async def test_play_history_success_icon() -> None:
    """A successful outcome stores the ✓ icon in the row."""
    app = _PlayHistoryApp()
    async with app.run_test() as pilot:
        table = app.query_one(PlayHistoryTable)
        table.add_play_row(_make_outcome(success=True))
        await pilot.pause()
        row = table.get_row_at(0)
        assert "✓" in row


async def test_play_history_failure_icon() -> None:
    """A failed outcome stores the ✗ icon in the row."""
    app = _PlayHistoryApp()
    async with app.run_test() as pilot:
        table = app.query_one(PlayHistoryTable)
        table.add_play_row(_make_outcome(success=False))
        await pilot.pause()
        row = table.get_row_at(0)
        assert "✗" in row


async def test_play_history_play_type_value() -> None:
    """The play type's string value is stored in the first column."""
    app = _PlayHistoryApp()
    async with app.run_test() as pilot:
        table = app.query_one(PlayHistoryTable)
        table.add_play_row(_make_outcome(play_type=PlayType.CODE_REVIEW))
        await pilot.pause()
        row = table.get_row_at(0)
        assert "code_review" in row


async def test_play_history_cost_formatted() -> None:
    """The dollar cost is formatted with a $ prefix and three decimal places."""
    app = _PlayHistoryApp()
    async with app.run_test() as pilot:
        table = app.query_one(PlayHistoryTable)
        table.add_play_row(_make_outcome(dollar_cost=0.123))
        await pilot.pause()
        row = table.get_row_at(0)
        assert "$0.123" in row


async def test_play_history_no_agent_id() -> None:
    """An outcome with agent_id=None results in an empty agent column (no error)."""
    app = _PlayHistoryApp()
    async with app.run_test() as pilot:
        table = app.query_one(PlayHistoryTable)
        table.add_play_row(_make_outcome(agent_id=None))
        await pilot.pause()
        assert table.row_count == 1


async def test_play_history_duration_formatted() -> None:
    """Duration is formatted as '{value:.1f}s' in the row."""
    app = _PlayHistoryApp()
    async with app.run_test() as pilot:
        table = app.query_one(PlayHistoryTable)
        table.add_play_row(_make_outcome(duration_seconds=42.5))
        await pilot.pause()
        row = table.get_row_at(0)
        assert "42.5s" in row


async def test_play_history_records_alignment_delta() -> None:
    """History rows include compact outcome metadata."""
    app = _PlayHistoryApp()
    async with app.run_test() as pilot:
        table = app.query_one(PlayHistoryTable)
        table.add_play_row(
            PlayOutcome(
                play_type=PlayType.CODE_REVIEW,
                agent_id="agent-1",
                success=True,
                partial=False,
                duration_seconds=3.0,
                token_cost=10,
                dollar_cost=0.01,
                artifacts=["https://example.test/pr/1"],
                alignment_delta=0.25,
                play_id=99,
            )
        )
        await pilot.pause()
        row = table.get_row_at(0)
        assert "99" in row
        assert "+0.25" in row
        assert "$0.010" in row


async def test_play_history_zebra_stripes_enabled() -> None:
    """PlayHistoryTable initialises with zebra_stripes set to True."""
    app = _PlayHistoryApp()
    async with app.run_test() as pilot:
        table = app.query_one(PlayHistoryTable)
        await pilot.pause()
        assert table.zebra_stripes is True


async def test_play_history_cursor_type_row() -> None:
    """PlayHistoryTable initialises with cursor_type set to 'row'."""
    app = _PlayHistoryApp()
    async with app.run_test() as pilot:
        table = app.query_one(PlayHistoryTable)
        await pilot.pause()
        assert table.cursor_type == "row"
