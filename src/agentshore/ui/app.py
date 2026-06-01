"""Textual application shell for AgentShore."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import structlog
from textual.app import App, ComposeResult, ScreenStackError
from textual.message import Message
from textual.widgets import Static

from agentshore.config.models import PolicyMode
from agentshore.state import PlayType

_logger = structlog.get_logger()

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.config import RuntimeConfig
    from agentshore.core import Orchestrator
    from agentshore.plays.base import PlayParams
    from agentshore.state import AgentStatus, AgentType, OrchestratorState, PlayOutcome
    from agentshore.ui.screens.startup import SessionStartupScreen

_PLAY_SHORT_LABEL: dict[PlayType, str] = {
    PlayType.INSTANTIATE_AGENT: "Instantiate",
    PlayType.UNBLOCK_PR: "Unblock",
    PlayType.WRITE_IMPLEMENTATION_PLAN: "Plan",
    PlayType.END_AGENT: "EndAgent",
    PlayType.ISSUE_PICKUP: "Pickup",
    PlayType.CODE_REVIEW: "Review",
    PlayType.MERGE_PR: "Merge",
    PlayType.RUN_QA: "QA",
    PlayType.SYSTEMATIC_DEBUGGING: "Debug",
    PlayType.DESIGN_AUDIT: "Audit",
    PlayType.END_SESSION: "EndSession",
    PlayType.RECONCILE_STATE: "Reconcile",
    PlayType.REFINE_TASK_BREAKDOWN: "Refine",
    PlayType.CLEANUP: "Cleanup",
    PlayType.BROWSER_VERIFICATION: "Browser",
    PlayType.TAKE_BREAK: "Break",
    PlayType.GROOM_BACKLOG: "Groom",
    PlayType.SEED_PROJECT: "Seed",
    PlayType.CALIBRATE_ALIGNMENT: "Calibrate",
    PlayType.PRUNE: "Prune",
    PlayType.FUTURE_7: "Reserved",
    PlayType.FUTURE_8: "Reserved",
}


def _short_play_label(pt: PlayType) -> str:
    return _PLAY_SHORT_LABEL.get(pt, pt.value.title())


@dataclass(frozen=True, slots=True)
class AppWiring:
    """Configuration injected into OrchestratorApp at construction time."""

    cfg: RuntimeConfig
    repo_root: Path
    seed_path: Path | None = None
    policy_path: Path | None = None
    policy_mode: PolicyMode = PolicyMode.LEARNING
    session_id: str | None = None


def _title_for_policy_mode(policy_mode: PolicyMode) -> str:
    if policy_mode is PolicyMode.AUDIT_REPLAY:
        return "AgentShore [REPLAY]"
    return "AgentShore"


class OrchestratorApp(App[None]):
    """Main AgentShore TUI application."""

    CSS_PATH = "agentshore.tcss"
    TITLE = "AgentShore"
    BINDINGS = [
        ("ctrl+q", "drain_session", "End (graceful)"),
        ("ctrl+shift+q", "hard_quit", "End (hard)"),
        ("question_mark", "show_help", "Help"),
        ("g", "show_goals", "Epics"),
        ("d", "show_agent_detail", "Agent Detail"),
        ("i", "show_issues", "Issues"),
        ("p", "toggle_pause", "Pause/Resume"),
        ("l", "show_learnings", "Learnings"),
    ]

    # ---- Messages (provider -> app) ----

    class StateUpdated(Message):
        def __init__(self, state: OrchestratorState) -> None:
            super().__init__()
            self.state = state

    class PlayStarted(Message):
        def __init__(self, play_type: PlayType, params: PlayParams) -> None:
            super().__init__()
            self.play_type = play_type
            self.params = params

    class PlayCompleted(Message):
        def __init__(self, outcome: PlayOutcome) -> None:
            super().__init__()
            self.outcome = outcome

    class AgentChanged(Message):
        def __init__(self, agent_id: str, status: AgentStatus) -> None:
            super().__init__()
            self.agent_id = agent_id
            self.status = status

    class AgentSubprocessSpawned(Message):
        def __init__(self, agent_id: str, agent_type: AgentType, pid: int) -> None:
            super().__init__()
            self.agent_id = agent_id
            self.agent_type = agent_type
            self.pid = pid

    class AgentSubprocessExited(Message):
        def __init__(
            self, agent_id: str, agent_type: AgentType, pid: int, exit_code: int | None
        ) -> None:
            super().__init__()
            self.agent_id = agent_id
            self.agent_type = agent_type
            self.pid = pid
            self.exit_code = exit_code

    class FeedbackRequested(Message):
        def __init__(self, reason: str) -> None:
            super().__init__()
            self.reason = reason

    class SessionPaused(Message):
        def __init__(self, reason: str) -> None:
            super().__init__()
            self.reason = reason

    class SessionDraining(Message):
        def __init__(self, reason: str) -> None:
            super().__init__()
            self.reason = reason

    class SessionEnded(Message):
        def __init__(self, reason: str) -> None:
            super().__init__()
            self.reason = reason

    class BootstrapPhase(Message):
        def __init__(self, phase: str, status: str, elapsed_ms: float) -> None:
            super().__init__()
            self.phase = phase
            self.status = status
            self.elapsed_ms = elapsed_ms

    # ---- Lifecycle ----

    def __init__(self, wiring: AppWiring | None = None) -> None:
        super().__init__()
        self._wiring = wiring
        policy_mode = wiring.policy_mode if wiring is not None else PolicyMode.LEARNING
        self.title = _title_for_policy_mode(policy_mode)
        self._orch: Orchestrator | None = None
        self._orch_task: asyncio.Task[None] | None = None
        self._paused: bool = False
        self._latest_state: OrchestratorState | None = None
        self._startup_screen: SessionStartupScreen | None = None

    def compose(self) -> ComposeResult:
        yield Static("AgentShore starting…")

    async def on_mount(self) -> None:
        """Bootstrap the orchestrator and start the main loop."""
        if self._wiring is None:
            return

        cfg = self._wiring.cfg
        repo_root = self._wiring.repo_root
        seed_path = self._wiring.seed_path
        policy_path = self._wiring.policy_path
        policy_mode = self._wiring.policy_mode
        session_id = self._wiring.session_id

        from agentshore.ui.screens.startup import SessionStartupScreen

        startup = SessionStartupScreen()
        self.push_screen(startup)
        self._startup_screen = startup

        try:
            from agentshore.core import Orchestrator
            from agentshore.ui.provider import TuiStateProvider

            provider = TuiStateProvider(self)
            orch = await Orchestrator.bootstrap(
                cfg=cfg,
                repo_root=repo_root,
                seed_path=seed_path,
                policy_path=policy_path,
                policy_mode=policy_mode,
                state_provider=provider,
                session_id=session_id,
            )
            self._orch = orch

            startup.mark_ready(
                session_id=orch._session_id,
                project=str(repo_root),
                mode=policy_mode.value,
            )

            from agentshore.ui.screens.dashboard import MainDashboard

            self.pop_screen()
            self._startup_screen = None
            self.push_screen(MainDashboard())

            await orch.publish_initial_state()

            async def _run_loop() -> None:
                async with orch:
                    await orch.run_until_idle()

            self._orch_task = asyncio.create_task(_run_loop())
        except Exception as exc:
            _logger.error("tui_bootstrap_failed", error=str(exc), exc_info=True)
            if self._startup_screen is not None:
                self._startup_screen.add_check("Bootstrap", str(exc), "error")
            self.notify(f"Bootstrap failed: {exc}", severity="error")

    def on_orchestrator_app_bootstrap_phase(self, event: BootstrapPhase) -> None:
        if self._startup_screen is not None:
            status = "ok" if event.status == "completed" else "pending"
            detail = f"{event.elapsed_ms:.0f}ms" if event.status == "completed" else "..."
            self._startup_screen.add_check(event.phase, detail, status)

    def _forward_to_screen(self, event: Message) -> None:
        """Forward a message to the active screen so its handlers fire."""
        try:
            self.screen.post_message(event)
        except ScreenStackError:
            _logger.debug("ui_forward_skipped_no_screen", event=event.__class__.__name__)
        except Exception as exc:
            _logger.warning(
                "ui_forward_failed",
                event=event.__class__.__name__,
                error=str(exc),
                exc_info=True,
            )

    # ---- Message handlers (state tracking) ----

    def on_orchestrator_app_state_updated(self, event: StateUpdated) -> None:
        self._latest_state = event.state
        self._forward_to_screen(event)

    def on_orchestrator_app_play_started(self, event: PlayStarted) -> None:
        self._forward_to_screen(event)

    def on_orchestrator_app_play_completed(self, event: PlayCompleted) -> None:
        self._forward_to_screen(event)

    def on_orchestrator_app_agent_changed(self, event: AgentChanged) -> None:
        if self._latest_state is not None:
            self._latest_state.agents = [
                replace(agent, status=event.status) if agent.agent_id == event.agent_id else agent
                for agent in self._latest_state.agents
            ]
        self._forward_to_screen(event)
        from agentshore.ui.screens.shutdown import SessionEndScreen

        for screen in self.screen_stack:
            if isinstance(screen, SessionEndScreen) and self._latest_state is not None:
                screen.update_agents(self._latest_state.agents)
                break

    def on_orchestrator_app_agent_subprocess_spawned(self, event: AgentSubprocessSpawned) -> None:
        self._forward_to_screen(event)

    def on_orchestrator_app_agent_subprocess_exited(self, event: AgentSubprocessExited) -> None:
        self._forward_to_screen(event)

    def on_orchestrator_app_session_paused(self, event: SessionPaused) -> None:
        self._paused = True
        if self._latest_state is not None:
            from agentshore.state import SessionState

            self._latest_state.session_state = SessionState.PAUSED
        self._forward_to_screen(event)

    def on_orchestrator_app_session_draining(self, event: SessionDraining) -> None:
        from agentshore.state import SessionState
        from agentshore.ui.screens.shutdown import SessionEndScreen

        if self._latest_state is not None:
            self._latest_state.session_state = SessionState.DRAINING
            self._latest_state.drain_reason = event.reason

        if self.screen_stack and isinstance(self.screen_stack[-1], SessionEndScreen):
            return
        screen = SessionEndScreen()
        self.push_screen(screen)
        screen.set_drain_reason(event.reason)
        screen.add_teardown_step(f"Drain started: {event.reason}", "ok")
        if self._latest_state is not None:
            screen.update_agents(self._latest_state.agents)

    def on_orchestrator_app_session_ended(self, event: SessionEnded) -> None:
        from agentshore.ui.screens.shutdown import SessionEndScreen

        for screen in self.screen_stack:
            if isinstance(screen, SessionEndScreen):
                screen.set_complete(event.reason)
                break

    def on_orchestrator_app_feedback_requested(self, event: FeedbackRequested) -> None:
        self._paused = True
        from agentshore.ui.screens.escalation import EscalationModal

        if self.screen_stack and isinstance(self.screen_stack[-1], EscalationModal):
            return

        def _on_result(result: str | None) -> None:
            if not self._orch:
                return
            if result == "drain":
                self._paused = False
                self.run_worker(self._orch.begin_drain("user_tui"))
            elif result == "hard_stop":
                self.run_worker(self._orch.hard_stop())
                self.exit()
            elif result and result.startswith("adjust_budget:"):
                try:
                    delta = float(result.split(":", 1)[1])
                except ValueError:
                    delta = 0.0
                should_resume = False
                if delta > 0:
                    should_resume = self._orch.adjust_budget(delta)
                if should_resume:
                    self._paused = False
                    self.run_worker(self._orch.resume())
            else:
                # Dismissed without action — leave session paused
                self._paused = False

        self.push_screen(EscalationModal(event.reason), callback=_on_result)

    # ---- Action implementations ----

    async def action_toggle_pause(self) -> None:
        if self._orch is None:
            return
        from agentshore.state import SessionState

        if self._latest_state and self._latest_state.session_state == SessionState.DRAINING:
            return
        if self._paused:
            self._paused = False
            await self._orch.resume()
        else:
            self._paused = True
            await self._orch.pause("user_request")

    async def action_drain_session(self) -> None:
        if self._orch is None:
            self.exit()
            return
        await self._orch.begin_drain("user_tui")

    async def action_hard_quit(self) -> None:
        if self._orch is None:
            self.exit()
            return
        await self._orch.hard_stop()
        self.exit()

    async def action_show_help(self) -> None:
        from agentshore.ui.screens.help import HelpOverlay

        self.push_screen(HelpOverlay())

    def _on_dashboard(self) -> bool:
        from agentshore.ui.screens.dashboard import MainDashboard

        return bool(self.screen_stack) and isinstance(self.screen_stack[-1], MainDashboard)

    async def action_show_goals(self) -> None:
        if not self._on_dashboard():
            return
        graph = self._latest_state.graph if self._latest_state else None
        from agentshore.ui.screens.goals import GoalsScreen

        self.push_screen(GoalsScreen(graph))

    async def action_show_agent_detail(self) -> None:
        if not self._on_dashboard():
            return
        agents = self._latest_state.agents if self._latest_state else []
        from agentshore.ui.screens.agent_detail import AgentDetailScreen

        self.push_screen(AgentDetailScreen(agents))

    async def action_generate_report(self) -> None:
        """Generate a session report and notify the user."""
        if self._orch is None:
            return
        try:
            from agentshore.reports.generator import ReportGenerator

            gen = ReportGenerator(self._orch._store)
            output_dir = self._orch._repo_root / ".agentshore" / "reports"
            path = await gen.generate_session_summary(self._orch._session_id, output_dir)
            self.notify(f"Report saved: {path}")
        except (OSError, RuntimeError, ValueError) as exc:
            _logger.warning("report_generation_failed", error=str(exc))
            self.notify(f"Report generation failed: {exc}", severity="error")

    async def action_show_issues(self) -> None:
        """Show the issue/PR work queue grouped by current lifecycle phase."""
        if not self._on_dashboard():
            return
        from agentshore.ui.screens.issues import IssueWorkQueueScreen

        self.push_screen(IssueWorkQueueScreen(self._latest_state))

    async def action_show_learnings(self) -> None:
        """Show session learnings in a simple modal."""
        if self._orch is None:
            self.notify("No active session.")
            return
        try:
            from agentshore.learnings import load

            learnings_path = self._orch._repo_root / ".agentshore" / "learnings.json"
            entries = await asyncio.to_thread(load, learnings_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            _logger.warning("ui_learnings_load_failed", error=str(exc))
            self.notify("Unable to load learnings.")
            return
        if not entries:
            self.notify("No learnings recorded.")
            return

        top = sorted(entries, key=lambda e: (-e.confidence, e.id))[:20]

        play_type_by_id: dict[int, str] = {}
        try:
            plays = await self._orch._store.get_play_history(self._orch._session_id)
            play_type_by_id = {r.play_id: r.play_type for r in plays if r.play_id is not None}
        except (OSError, sqlite3.Error) as exc:
            _logger.warning("ui_learnings_play_history_failed", error=str(exc))

        header = f"  {'#':>2}  {'Pattern':<32}  {'Conf.':>5}  {'Category':<9}  {'Source':<10}"
        separator = "─" * len(header)
        lines = [header, separator]
        for i, e in enumerate(top, start=1):
            play_type_str = (
                play_type_by_id.get(e.source_play_id) if e.source_play_id is not None else None
            )
            if play_type_str is not None:
                try:
                    source = f"{_short_play_label(PlayType(play_type_str))} #{e.source_play_id}"
                except ValueError:
                    source = f"#{e.source_play_id}"
            else:
                source = "—"
            row = (
                f"  {i:>2}  {e.pattern:<32.32}  {e.confidence:>5.2f}"
                f"  {e.category:<9.9}  {source:<10.10}"
            )
            lines.append(row)

        text = "Session Learnings\n" + "\n".join(lines)
        from textual.containers import Vertical
        from textual.screen import ModalScreen
        from textual.widgets import Static as ModalStatic

        class LearningsModal(ModalScreen[None]):
            BINDINGS = [("escape", "dismiss", "Close")]

            def compose(self) -> ComposeResult:
                with Vertical():
                    yield ModalStatic(text)

        self.push_screen(LearningsModal())
