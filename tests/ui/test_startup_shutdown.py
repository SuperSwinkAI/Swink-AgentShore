"""Tests for SessionStartupScreen and SessionEndScreen."""

from __future__ import annotations

from textual.app import App
from textual.widgets import Static

from agentshore.ui.screens.shutdown import SessionEndScreen
from agentshore.ui.screens.startup import SessionStartupScreen

# ---------------------------------------------------------------------------
# Test app
# ---------------------------------------------------------------------------


class ScreenTestApp(App[None]):
    """Minimal host app for screen tests."""


# ---------------------------------------------------------------------------
# SessionStartupScreen tests
# ---------------------------------------------------------------------------


async def test_startup_screen_has_banner() -> None:
    """The startup screen renders the AGENTSHORE_BANNER in the #banner Static."""
    app = ScreenTestApp()
    async with app.run_test() as pilot:
        app.push_screen(SessionStartupScreen())
        await pilot.pause()
        banner = app.screen.query_one("#banner", Static)
        rendered = str(banner.render())
        assert "███████" in rendered


async def test_startup_add_check() -> None:
    """Adding a check item causes it to appear in the checklist rendering."""
    app = ScreenTestApp()
    async with app.run_test() as pilot:
        app.push_screen(SessionStartupScreen())
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SessionStartupScreen)
        screen.add_check("Config loaded", "agentshore.yaml", status="ok")
        await pilot.pause()
        checklist = app.screen.query_one("#checklist", Static)
        rendered = str(checklist.render())
        assert "Config loaded" in rendered
        assert "agentshore.yaml" in rendered


async def test_startup_mark_ready() -> None:
    """mark_ready() populates the session-info Static with session details."""
    app = ScreenTestApp()
    async with app.run_test() as pilot:
        app.push_screen(SessionStartupScreen())
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SessionStartupScreen)
        screen.mark_ready("abc-123", "/my/project", "solo")
        await pilot.pause()
        info = app.screen.query_one("#session-info", Static)
        rendered = str(info.render())
        assert "abc-123" in rendered
        assert "/my/project" in rendered
        assert "solo" in rendered


# ---------------------------------------------------------------------------
# SessionEndScreen tests
# ---------------------------------------------------------------------------


async def test_shutdown_screen_composes() -> None:
    """The shutdown screen has title and teardown Static widgets."""
    app = ScreenTestApp()
    async with app.run_test() as pilot:
        app.push_screen(SessionEndScreen())
        await pilot.pause()
        assert app.screen.query_one("#title", Static) is not None
        assert app.screen.query_one("#teardown", Static) is not None


async def test_shutdown_add_step() -> None:
    """Adding a teardown step renders it in the #teardown Static."""
    app = ScreenTestApp()
    async with app.run_test() as pilot:
        app.push_screen(SessionEndScreen())
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SessionEndScreen)
        screen.add_teardown_step("Clear agent: claude-code", status="ok")
        await pilot.pause()
        teardown = app.screen.query_one("#teardown", Static)
        rendered = str(teardown.render())
        assert "Clear agent: claude-code" in rendered
