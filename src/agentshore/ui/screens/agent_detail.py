"""AgentDetailScreen -- expanded agent view with resource usage and navigation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from agentshore.state import AgentSnapshot

_STATUS_SYMBOLS: dict[str, str] = {
    "idle": "●",  # ●
    "busy": "◉",  # ◉
    "error": "✕",  # ✕
    "terminated": "—",  # —
}


class AgentDetailScreen(ModalScreen[None]):
    """Expanded agent view with resource usage and task history."""

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("left", "prev_agent", "Previous"),
        ("right", "next_agent", "Next"),
    ]

    def __init__(
        self,
        agents: list[AgentSnapshot],
        selected_index: int = 0,
    ) -> None:
        super().__init__()
        self._agents = agents
        self._selected_index = selected_index

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-detail-container"):
            yield Static("", id="agent-info")

    def on_mount(self) -> None:
        """Populate the display once the widget tree is ready."""
        self._refresh_display()

    def _refresh_display(self) -> None:
        if not self._agents:
            self.query_one("#agent-info", Static).update("No agents available.")
            return
        agent = self._agents[self._selected_index]
        sym = _STATUS_SYMBOLS.get(agent.status.value, "?")
        ctx_pct = agent.context_size / 200_000 * 100 if agent.context_size else 0.0
        filled = min(20, round(ctx_pct / 5))
        bar = "█" * filled + "░" * (20 - filled)

        text = (
            f"Agent Detail ({self._selected_index + 1}/{len(self._agents)})\n"
            f"{'--' * 20}\n\n"
            f"  Agent:     {agent.agent_id}\n"
            f"  Type:      {agent.agent_type.value}\n"
            f"  Tier:      {agent.model_tier or 'unknown'}\n"
            f"  Model:     {agent.model or 'default'}\n"
            f"  Status:    {sym} {agent.status.value.upper()}\n\n"
            f"  Context:   {bar} {agent.context_size:,} tokens ({ctx_pct:.0f}%)\n"
            f"  Cost:      ${agent.total_cost:.3f}\n"
            f"  Tokens:    {agent.total_tokens:,}\n"
            f"  Completed: {agent.tasks_completed}\n"
            f"  Failed:    {agent.tasks_failed}\n\n"
            f"  [Left/Right switch agent    Esc close]"
        )
        self.query_one("#agent-info", Static).update(text)

    def action_prev_agent(self) -> None:
        if self._agents:
            self._selected_index = (self._selected_index - 1) % len(self._agents)
            self._refresh_display()

    def action_next_agent(self) -> None:
        if self._agents:
            self._selected_index = (self._selected_index + 1) % len(self._agents)
            self._refresh_display()
