"""EscalationModal -- shown when human feedback is requested."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from textual.containers import Vertical
from textual.css.query import QueryError
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult


class EscalationModal(ModalScreen[str]):
    """Shows when human feedback is requested. Returns the chosen action."""

    BINDINGS = [("escape", "dismiss_modal", "Dismiss")]

    def __init__(self, reason: str) -> None:
        super().__init__()
        self._reason = reason

    def compose(self) -> ComposeResult:
        with Vertical(id="escalation-container"):
            yield Static(f"Attention Required: {self._reason}", id="escalation-title")
            yield Button("Add Budget", id="btn-budget", variant="primary")
            yield Static("", id="budget-input-row", classes="hidden")
            yield Button("Stop (graceful)", id="btn-stop", variant="warning")
            yield Button("Hard Stop", id="btn-hard-stop", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "btn-budget":
            self._show_budget_input()
        elif bid == "btn-stop":
            self.dismiss("drain")
        elif bid == "btn-hard-stop":
            self.dismiss("hard_stop")
        elif bid == "btn-budget-confirm":
            self._submit_budget()
        elif bid == "btn-budget-cancel":
            self._hide_budget_input()

    def _show_budget_input(self) -> None:
        try:
            self.query_one("#budget-amount")
            return  # already mounted
        except QueryError:
            pass
        try:
            row = self.query_one("#budget-input-row", Static)
        except QueryError:
            return
        row.remove_class("hidden")
        row.update("")
        self.mount(
            Input(placeholder="Amount in USD (e.g. 5)", id="budget-amount", type="number"),
            Button("Add", id="btn-budget-confirm", variant="success"),
            Button("Cancel", id="btn-budget-cancel", variant="default"),
            after="#budget-input-row",
        )

    def _hide_budget_input(self) -> None:
        for widget_id in ("budget-amount", "btn-budget-confirm", "btn-budget-cancel"):
            with contextlib.suppress(QueryError):
                self.query_one(f"#{widget_id}").remove()

    def _submit_budget(self) -> None:
        try:
            inp = self.query_one("#budget-amount", Input)
        except QueryError:
            self.dismiss(None)
            return
        try:
            amount = float(inp.value)
        except ValueError:
            amount = 0.0
        if amount > 0:
            self.dismiss(f"adjust_budget:{amount}")
        else:
            inp.border_title = "Must be > 0"
            inp.focus()

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)
