"""HelpOverlay -- modal screen showing keybinding reference."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

HELP_TEXT = """
AgentShore -- Keyboard Shortcuts

  ^q          End session (graceful drain)
  shift+^q    End session (hard stop)
  g           View epic closure
  d           Agent detail view
  i           Issues overview
  ?           This help screen

Navigation:
  Up/Down     Scroll / select
  Tab         Switch sections
  Esc         Close modal / cancel
  Enter       Confirm selection
"""


class HelpOverlay(ModalScreen[None]):
    """Full-screen modal displaying keyboard shortcuts."""

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("question_mark", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-container"):
            yield Static(HELP_TEXT, id="help-text")
