"""Regression tests for OrchestratorApp._forward_to_screen error logging.

Issue #129: bare `except Exception` was swallowing all dispatch errors at DEBUG
level, hiding real bugs in screen message handlers. The fix preserves the
benign no-screen-mounted case at DEBUG but surfaces every other exception at
WARNING with the exception bound and a traceback captured via `exc_info=True`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from textual.app import ScreenStackError
from textual.message import Message

from agentshore.ui.app import OrchestratorApp


class _Probe(Message):
    pass


@pytest.mark.asyncio()
async def test_forward_to_screen_logs_unexpected_error_at_warning() -> None:
    """An arbitrary exception from screen.post_message is logged at WARNING with traceback."""
    app = OrchestratorApp()
    async with app.run_test():
        boom = RuntimeError("kaboom")
        fake_screen = MagicMock()
        fake_screen.post_message.side_effect = boom

        with (
            patch.object(type(app), "screen", new=fake_screen),
            patch("agentshore.ui.app._logger") as mock_logger,
        ):
            app._forward_to_screen(_Probe())

        mock_logger.warning.assert_called_once()
        args, kwargs = mock_logger.warning.call_args
        assert args == ("ui_forward_failed",)
        assert kwargs["event"] == "_Probe"
        assert kwargs["error"] == "kaboom"
        assert kwargs["exc_info"] is True
        mock_logger.debug.assert_not_called()


@pytest.mark.asyncio()
async def test_forward_to_screen_logs_missing_screen_at_debug() -> None:
    """ScreenStackError (no screen mounted) stays at DEBUG — it's an expected no-op."""
    app = OrchestratorApp()
    async with app.run_test():
        fake_screen = MagicMock()
        fake_screen.post_message.side_effect = ScreenStackError("no screens")

        with (
            patch.object(type(app), "screen", new=fake_screen),
            patch("agentshore.ui.app._logger") as mock_logger,
        ):
            app._forward_to_screen(_Probe())

        mock_logger.debug.assert_called_once()
        args, kwargs = mock_logger.debug.call_args
        assert args == ("ui_forward_skipped_no_screen",)
        assert kwargs["event"] == "_Probe"
        mock_logger.warning.assert_not_called()
