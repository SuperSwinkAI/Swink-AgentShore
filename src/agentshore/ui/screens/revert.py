"""RevertConfirmModal -- confirmation dialog for revert operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult


class RevertConfirmModal(ModalScreen[bool]):
    """Confirms a revert operation. Returns True on confirm, False on cancel."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="revert-container"):
            yield Static("Confirm Revert", id="revert-title")
            yield Static("This will revert the last commit. This action cannot be undone.")
            with Horizontal():
                yield Button("Confirm Revert", id="btn-confirm", variant="error")
                yield Button("Cancel", id="btn-cancel", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-confirm":
            self.dismiss(True)
        else:
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)
