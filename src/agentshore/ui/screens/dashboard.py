"""MainDashboard screen — composes all 7 widgets and routes OrchestratorApp messages."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Footer, Header

from agentshore.ui.play_labels import play_label
from agentshore.ui.widgets.agent_panel import AgentPanel
from agentshore.ui.widgets.alert_bar import AlertBar
from agentshore.ui.widgets.alignment import AlignmentBars
from agentshore.ui.widgets.budget import BudgetWidget
from agentshore.ui.widgets.play_history import (
    DEFAULT_VISIBLE_ROW_LIMIT,
    NARROW_VISIBLE_ROW_LIMIT,
    PlayHistoryTable,
)
from agentshore.ui.widgets.rl_state import RLStateBar, loop_level_for_streak
from agentshore.ui.widgets.work_queue import WorkQueueSummary

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.binding import BindingType
    from textual.events import Resize

    from agentshore.ui.app import OrchestratorApp


class MainDashboard(Screen[None]):
    """Primary dashboard — composes all 7 widgets, routes OrchestratorApp messages."""

    BINDINGS: ClassVar[list[BindingType]] = []

    # Tracks agent_id → subprocess PID for all currently-running agent subprocesses.
    # Populated by AgentSubprocessSpawned / cleared by AgentSubprocessExited.
    # Issue #154's Kill-all control and crash-recovery screen will read this dict.
    _subprocess_pids: dict[str, int]
    _loop_alert_active: bool

    def on_mount(self) -> None:
        self._subprocess_pids = {}
        self._loop_alert_active = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield AlertBar(id="alert-bar")
        yield AgentPanel(id="agent-panel")
        yield PlayHistoryTable(id="play-history")
        with Horizontal(id="bottom-row"):
            yield AlignmentBars(id="alignment")
            yield BudgetWidget(id="budget")
            yield WorkQueueSummary(id="work-queue")
        yield RLStateBar(id="rl-state")
        yield Footer()

    # ---- Message routing ----

    def on_orchestrator_app_state_updated(self, event: OrchestratorApp.StateUpdated) -> None:
        """Route state snapshot to all data-display widgets."""
        self.query_one("#agent-panel", AgentPanel).update_agents(
            event.state.agents,
            active_play=event.state.active_play,
        )
        self.query_one("#alignment", AlignmentBars).update_clusters(event.state.graph)
        self.query_one("#budget", BudgetWidget).update_budget(
            event.state.budget,
            event.state.trajectory,
        )
        self.query_one("#work-queue", WorkQueueSummary).update_state(event.state)
        self.query_one("#rl-state", RLStateBar).update_state(event.state)
        alert = self.query_one("#alert-bar", AlertBar)
        loop_level = loop_level_for_streak(event.state.same_type_failure_streak)
        if loop_level == 3 and event.state.last_play_type is not None:
            alert.show_loop(
                play_label(event.state.last_play_type),
                event.state.same_type_failure_streak,
            )
            self._loop_alert_active = True
        elif loop_level < 3 and self._loop_alert_active:
            alert.hide()
            self._loop_alert_active = False

    def on_orchestrator_app_play_started(self, event: OrchestratorApp.PlayStarted) -> None:
        """Set the active play widget to show the current play."""
        self.query_one("#agent-panel", AgentPanel).set_play_started(event.play_type, event.params)

    def on_orchestrator_app_play_completed(self, event: OrchestratorApp.PlayCompleted) -> None:
        """Clear active play and log the outcome in the history table."""
        play_id = getattr(event.outcome, "play_id", None)
        if play_id is not None and play_id == getattr(self, "_last_completed_play_id", None):
            return
        self._last_completed_play_id = play_id
        self.query_one("#agent-panel", AgentPanel).clear_active_play()
        agent_name = self._resolve_agent_name(event.outcome.agent_id)
        self.query_one("#play-history", PlayHistoryTable).add_play_row(
            event.outcome, agent_display_name=agent_name
        )

    def on_orchestrator_app_agent_changed(self, event: OrchestratorApp.AgentChanged) -> None:
        """Refresh agent-dependent widgets after an eager status hint."""
        state = getattr(self.app, "_latest_state", None)
        if state is None:
            return
        self.query_one("#agent-panel", AgentPanel).update_agents(
            state.agents,
            active_play=state.active_play,
        )

    def on_orchestrator_app_agent_subprocess_spawned(
        self, event: OrchestratorApp.AgentSubprocessSpawned
    ) -> None:
        """Record subprocess PID so Kill-all / crash-recovery (issue #154) can act on it."""
        self._subprocess_pids[event.agent_id] = event.pid

    def on_orchestrator_app_agent_subprocess_exited(
        self, event: OrchestratorApp.AgentSubprocessExited
    ) -> None:
        """Remove the subprocess PID entry when the process has exited."""
        self._subprocess_pids.pop(event.agent_id, None)

    def _resolve_agent_name(self, agent_id: str | None) -> str | None:
        """Look up display name from latest state snapshot."""
        if agent_id is None:
            return None
        state = getattr(self.app, "_latest_state", None)
        if state is None:
            return agent_id[:12]
        for a in state.agents:
            if a.agent_id == agent_id:
                return a.display_name or agent_id[:12]
        return agent_id[:12]

    def on_orchestrator_app_feedback_requested(
        self, event: OrchestratorApp.FeedbackRequested
    ) -> None:
        """Show feedback-needed alert."""
        self.query_one("#alert-bar", AlertBar).show(f"Feedback needed: {event.reason}", "warning")

    def on_orchestrator_app_session_paused(self, event: OrchestratorApp.SessionPaused) -> None:
        """Show session-paused alert."""
        if not event.reason.startswith("loop_detected"):
            self.query_one("#alert-bar", AlertBar).show(f"Session paused: {event.reason}", "info")
        state = getattr(self.app, "_latest_state", None)
        if state is not None:
            self.query_one("#rl-state", RLStateBar).update_state(state)

    # ---- Responsive layout ----

    def on_resize(self, event: Resize) -> None:
        """Apply layout class based on terminal width breakpoints."""
        width = event.size.width
        self._clear_layout_classes()
        if width >= 100:
            self._apply_standard_layout()
        elif width >= 60:
            self._apply_narrow_layout()
        elif width >= 40:
            self._apply_minimal_layout()
        else:
            self._apply_error_layout()

    def _clear_layout_classes(self) -> None:
        self.remove_class("layout-standard", "layout-narrow", "layout-minimal", "layout-error")

    def _apply_standard_layout(self) -> None:
        self.add_class("layout-standard")
        self._set_recent_play_limit(DEFAULT_VISIBLE_ROW_LIMIT)

    def _apply_narrow_layout(self) -> None:
        self.add_class("layout-narrow")
        self._set_recent_play_limit(NARROW_VISIBLE_ROW_LIMIT)

    def _apply_minimal_layout(self) -> None:
        self.add_class("layout-minimal")
        self._set_recent_play_limit(NARROW_VISIBLE_ROW_LIMIT)

    def _apply_error_layout(self) -> None:
        self.add_class("layout-error")
        self.query_one("#alert-bar", AlertBar).show(
            "Terminal too narrow. Resize to at least 40 columns.", "error"
        )

    def _set_recent_play_limit(self, limit: int) -> None:
        self.query_one("#play-history", PlayHistoryTable).set_visible_row_limit(limit)
