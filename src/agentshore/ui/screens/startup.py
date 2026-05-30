"""SessionStartupScreen — pre-flight checklist displayed during bootstrap."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

AGENTSHORE_BANNER = r"""
   █████╗  ██████╗ ███████╗███╗   ██╗████████╗███████╗██╗  ██╗ ██████╗ ██████╗ ███████╗
  ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝██╔════╝██║  ██║██╔═══██╗██╔══██╗██╔════╝
  ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   ███████╗███████║██║   ██║██████╔╝█████╗
  ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   ╚════██║██╔══██║██║   ██║██╔══██╗██╔══╝
  ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   ███████║██║  ██║╚██████╔╝██║  ██║███████╗
  ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝   ╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝

  RL-based multi-agent coding orchestrator
""".strip("\n")

_STATUS_ICONS: dict[str, str] = {
    "ok": "✓",  # ✓
    "pending": "◷",  # ◷
    "error": "✗",  # ✗
    "skip": "–",  # –
}

_logger = structlog.get_logger(__name__)


class SessionStartupScreen(Screen[None]):
    """Pre-flight checklist displayed during bootstrap."""

    _checks: reactive[list[tuple[str, str, str]]] = reactive(list, layout=True)
    _session_info: reactive[str] = reactive("", layout=True)

    def compose(self) -> ComposeResult:
        yield Static(AGENTSHORE_BANNER, id="banner")
        yield Static("", id="checklist")
        yield Static("", id="session-info")

    def add_check(self, label: str, detail: str, status: str = "pending") -> None:
        """Add or update a check item.

        Parameters
        ----------
        label:
            Short name of the check (e.g. "Config loaded").
        detail:
            Explanatory text shown to the right of the icon + label.
        status:
            One of ``'ok'``, ``'pending'``, ``'error'``, ``'skip'``.
        """
        icon = _STATUS_ICONS.get(status, "?")
        # Replace existing check with same label, or append.
        new_checks = list(self._checks)
        for idx, (_, existing_label, _) in enumerate(new_checks):
            if existing_label == label:
                new_checks[idx] = (icon, label, detail)
                break
        else:
            new_checks.append((icon, label, detail))
        self._checks = new_checks

    def mark_ready(self, session_id: str, project: str, mode: str) -> None:
        """Show session info and indicate readiness to transition."""
        self._session_info = (
            f"  Session ID:  {session_id}\n  Project:     {project}\n  Mode:        {mode}"
        )

    # ---- watchers ----

    def watch__checks(self, checks: list[tuple[str, str, str]]) -> None:
        from textual.css.query import QueryError

        try:
            widget = self.query_one("#checklist", Static)
        except QueryError as exc:
            _logger.debug("startup_widget_query_failed", widget="checklist", error=str(exc))
            return
        widget.update(self._render_checklist(checks))

    def watch__session_info(self, value: str) -> None:
        from textual.css.query import QueryError

        try:
            widget = self.query_one("#session-info", Static)
        except QueryError as exc:
            _logger.debug("startup_widget_query_failed", widget="session-info", error=str(exc))
            return
        widget.update(value)

    @staticmethod
    def _render_checklist(checks: list[tuple[str, str, str]]) -> str:
        if not checks:
            return ""
        lines: list[str] = ["  Pre-flight Checks", "  " + "─" * 50]
        for icon, label, detail in checks:
            lines.append(f"  {icon} {label:<30} {detail}")
        return "\n".join(lines)
