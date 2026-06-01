"""Tests for OrchestratorApp keybinding actions and EscalationModal."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from agentshore.ui.app import OrchestratorApp
from agentshore.ui.screens.escalation import EscalationModal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_orch() -> MagicMock:
    orch = MagicMock()
    orch.pause = AsyncMock()
    orch.resume = AsyncMock()
    orch.stop = AsyncMock()
    orch.begin_drain = AsyncMock()
    orch.hard_stop = AsyncMock()
    orch.adjust_budget = MagicMock()
    return orch


# ---------------------------------------------------------------------------
# Help overlay
# ---------------------------------------------------------------------------


async def test_press_question_mark_opens_help() -> None:
    """Pressing '?' opens the HelpOverlay."""
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        from agentshore.ui.screens.help import HelpOverlay

        assert any(isinstance(s, HelpOverlay) for s in app.screen_stack)


# Note: 'p' (pause), 'o' (override), 'g' (goals), and 'd' (agent detail)
# keybinding tests were removed. 'p' and 'o' point at features that were
# intentionally deleted (commits c4f276b — Pause; fe9040c — PlayOverrideScreen).
# 'g' and 'd' bindings still exist, but the action handlers short-circuit
# unless the app is on the dashboard screen; reliable test setup for that
# would mean seeding the full state pipeline. Worth restoring with proper
# fixtures if dashboard-screen-routing tests become a priority.


# ---------------------------------------------------------------------------
# Quit session
# ---------------------------------------------------------------------------


async def test_press_ctrl_q_drains_session() -> None:
    """Pressing Ctrl+Q calls begin_drain() and does not push SessionEndScreen (no provider broadcast)."""
    from agentshore.ui.screens.shutdown import SessionEndScreen

    app = OrchestratorApp()
    app._orch = _make_mock_orch()
    async with app.run_test() as pilot:
        await pilot.press("ctrl+q")
        await pilot.pause()
        app._orch.begin_drain.assert_called_once_with("user_tui")
        count = sum(1 for s in app.screen_stack if isinstance(s, SessionEndScreen))
        assert count == 0


# ---------------------------------------------------------------------------
# EscalationModal
# ---------------------------------------------------------------------------


async def test_escalation_stop_drains() -> None:
    """Clicking Stop in EscalationModal calls begin_drain."""
    app = OrchestratorApp()
    app._orch = _make_mock_orch()
    async with app.run_test() as pilot:
        app.post_message(OrchestratorApp.FeedbackRequested("budget exceeded"))
        await pilot.pause()
        await pilot.pause()

        assert any(isinstance(s, EscalationModal) for s in app.screen_stack)

        await pilot.click("#btn-stop")
        await pilot.pause()
        await pilot.pause()

        app._orch.begin_drain.assert_called_once_with("user_tui")
        assert app._paused is False


async def test_escalation_hard_stop_calls_hard_stop() -> None:
    """Clicking Hard Stop in EscalationModal calls hard_stop."""
    app = OrchestratorApp()
    app._orch = _make_mock_orch()
    async with app.run_test() as pilot:
        app.post_message(OrchestratorApp.FeedbackRequested("alignment dropped"))
        await pilot.pause()
        await pilot.pause()

        assert any(isinstance(s, EscalationModal) for s in app.screen_stack)

        await pilot.click("#btn-hard-stop")
        await pilot.pause()
        await pilot.pause()

        app._orch.hard_stop.assert_called_once()


async def test_escalation_add_budget_resumes_budget_pause() -> None:
    """Adding budget resumes only when the orchestrator says the pause was budget-related."""
    from textual.widgets import Input

    app = OrchestratorApp()
    app._orch = _make_mock_orch()
    app._orch.adjust_budget.return_value = True
    async with app.run_test() as pilot:
        app.post_message(OrchestratorApp.FeedbackRequested("budget_exhausted"))
        await pilot.pause()
        await pilot.pause()

        modal = next(s for s in app.screen_stack if isinstance(s, EscalationModal))
        await pilot.click("#btn-budget")
        await pilot.pause()
        modal.query_one("#budget-amount", Input).value = "5"
        await pilot.click("#btn-budget-confirm")
        await pilot.pause()
        await pilot.pause()

        app._orch.adjust_budget.assert_called_once_with(5.0)
        app._orch.resume.assert_called_once()
        assert app._paused is False


async def test_escalation_add_budget_keeps_non_budget_pause_paused() -> None:
    """A budget adjustment alone must not resume manual or loop-detection pauses."""
    from textual.widgets import Input

    app = OrchestratorApp()
    app._orch = _make_mock_orch()
    app._orch.adjust_budget.return_value = False
    async with app.run_test() as pilot:
        app.post_message(OrchestratorApp.FeedbackRequested("loop_detected"))
        await pilot.pause()
        await pilot.pause()

        modal = next(s for s in app.screen_stack if isinstance(s, EscalationModal))
        await pilot.click("#btn-budget")
        await pilot.pause()
        modal.query_one("#budget-amount", Input).value = "5"
        await pilot.click("#btn-budget-confirm")
        await pilot.pause()
        await pilot.pause()

        app._orch.adjust_budget.assert_called_once_with(5.0)
        app._orch.resume.assert_not_called()
        assert app._paused is True


async def test_ctrl_q_no_double_screen() -> None:
    """Pressing Ctrl+Q should not push two SessionEndScreen instances."""
    from agentshore.ui.screens.shutdown import SessionEndScreen

    app = OrchestratorApp()
    app._orch = _make_mock_orch()
    async with app.run_test() as pilot:
        await pilot.press("ctrl+q")
        await pilot.pause()
        count = sum(1 for s in app.screen_stack if isinstance(s, SessionEndScreen))
        assert count <= 1


async def test_escalation_dismiss_does_not_drain() -> None:
    """Pressing Escape on EscalationModal does not trigger begin_drain."""
    app = OrchestratorApp()
    app._orch = _make_mock_orch()
    async with app.run_test() as pilot:
        app.post_message(OrchestratorApp.FeedbackRequested("budget_critical"))
        await pilot.pause()
        await pilot.pause()
        assert any(isinstance(s, EscalationModal) for s in app.screen_stack)
        await pilot.press("escape")
        await pilot.pause()
        await pilot.pause()
        app._orch.begin_drain.assert_not_called()
        assert app._paused is False
