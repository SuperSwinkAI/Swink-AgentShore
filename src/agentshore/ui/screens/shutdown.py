"""SessionEndScreen — teardown progress and session summary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

import structlog
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from agentshore.state import AgentSnapshot

_STATUS_ICONS: dict[str, str] = {
    "ok": "✓",
    "pending": "◷",
    "error": "✗",
    "skip": "–",
}

_DRAIN_REASON_LABELS: dict[str, str] = {
    "budget_reserve_reached": "Budget reserve reached",
    "max_plays_reached": "Maximum play count reached",
    "user_tui": "User requested stop",
    "user_api": "Stop via API",
    "timeout": "Session timeout",
    "idle_limit": "Idle limit reached",
}

_logger = structlog.get_logger(__name__)


class _ReportCapableApp(Protocol):
    async def action_generate_report(self) -> None: ...


class SessionEndScreen(Screen[None]):
    """Teardown progress + session summary."""

    BINDINGS = [
        ("r", "generate_report", "Report"),
        ("q", "quit_app", "Quit"),
        ("ctrl+shift+q", "quit_app", "End (hard)"),
    ]

    _drain_reason: str = ""
    _teardown_steps: reactive[list[tuple[str, str]]] = reactive(list, layout=True)
    _agents: reactive[list[AgentSnapshot]] = reactive(list, layout=True)
    _complete: reactive[str | None] = reactive(None, layout=True)

    def compose(self) -> ComposeResult:
        yield Static("Draining — waiting for agents to finish...", id="title")
        yield Static("  Ctrl+Shift+Q to force-quit immediately", id="drain-hint")
        yield Static("", id="agents")
        yield Static("", id="teardown")

    # ---- public API ----

    def set_drain_reason(self, reason: str) -> None:
        """Set the drain reason and update the title."""
        import contextlib

        self._drain_reason = reason
        label = _DRAIN_REASON_LABELS.get(reason, reason)
        from textual.css.query import QueryError

        with contextlib.suppress(QueryError):
            self.query_one("#title", Static).update(
                f"Draining: {label} — waiting for agents to finish..."
            )

    def update_agents(self, agents: list[AgentSnapshot]) -> None:
        """Update the live agent list during drain."""
        self._agents = list(agents)

    def add_teardown_step(self, description: str, status: str = "pending") -> None:
        icon = _STATUS_ICONS.get(status, "?")
        new_steps = list(self._teardown_steps)
        for idx, (_, existing_desc) in enumerate(new_steps):
            if existing_desc == description:
                new_steps[idx] = (icon, description)
                break
        else:
            new_steps.append((icon, description))
        self._teardown_steps = new_steps

    def set_complete(self, reason: str) -> None:
        self._complete = reason

    # ---- actions ----

    async def action_generate_report(self) -> None:
        await cast("_ReportCapableApp", self.app).action_generate_report()

    async def action_quit_app(self) -> None:
        if hasattr(self.app, "action_hard_quit"):
            await self.app.action_hard_quit()
        else:
            self.app.exit()

    # ---- watchers ----

    def watch__agents(self, agents: list[AgentSnapshot]) -> None:
        from textual.css.query import QueryError

        try:
            widget = self.query_one("#agents", Static)
        except QueryError:
            return
        widget.update(self._render_agents(agents))

    def watch__teardown_steps(self, steps: list[tuple[str, str]]) -> None:
        from textual.css.query import QueryError

        try:
            widget = self.query_one("#teardown", Static)
        except QueryError as exc:
            _logger.debug("shutdown_widget_query_failed", widget="teardown", error=str(exc))
            return
        widget.update(self._render_teardown(steps))

    def watch__complete(self, reason: str | None) -> None:
        if reason is None:
            return
        from textual.css.query import QueryError

        label = _DRAIN_REASON_LABELS.get(self._drain_reason, self._drain_reason) or reason
        try:
            self.query_one("#title", Static).update(
                f"Session ended: {label} — q to quit, r for report"
            )
            self.query_one("#drain-hint", Static).update("")
        except QueryError:
            pass

    @staticmethod
    def _render_agents(agents: list[AgentSnapshot]) -> str:
        if not agents:
            return ""
        lines: list[str] = ["\n  Active Agents", "  " + "─" * 60]
        for agent in agents:
            status = agent.status.value if hasattr(agent.status, "value") else str(agent.status)
            play = ""
            if agent.current_play_type is not None:
                play = f"  [{agent.current_play_type.value}]"
            name = agent.display_name or agent.agent_id[:12]
            tier = agent.model_tier or ""
            lines.append(f"  {name:<30} {status:<8} {tier:<8}{play}")
        return "\n".join(lines)

    @staticmethod
    def _render_teardown(steps: list[tuple[str, str]]) -> str:
        if not steps:
            return ""
        lines: list[str] = ["\n  Teardown Checklist", "  " + "─" * 60]
        for icon, description in steps:
            lines.append(f"  {icon} {description}")
        return "\n".join(lines)
