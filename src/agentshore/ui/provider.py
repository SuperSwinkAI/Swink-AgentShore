"""TUI StateProvider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.ui.app import OrchestratorApp

if TYPE_CHECKING:
    from agentshore.plays.base import PlayParams
    from agentshore.state import (
        AgentStatus,
        AgentType,
        BudgetSnapshot,
        OrchestratorState,
        PlayOutcome,
        PlayType,
    )


class TuiStateProvider:
    """Posts Textual Messages to OrchestratorApp for each StateProvider event.

    Satisfies the :class:`agentshore.state.StateProvider` runtime-checkable
    protocol — all six async methods are present with the correct signatures.
    """

    def __init__(self, app: OrchestratorApp) -> None:
        self._app = app

    async def on_state_update(self, state: OrchestratorState) -> None:
        self._app.post_message(OrchestratorApp.StateUpdated(state))

    async def on_budget_update(self, budget: BudgetSnapshot) -> None:
        # No-op: TUI re-renders budget on every full state update; the
        # countdown heartbeat is dashboard-only.
        return None

    async def on_play_started(self, play_type: PlayType, params: PlayParams) -> None:
        self._app.post_message(OrchestratorApp.PlayStarted(play_type, params))

    async def on_play_completed(self, outcome: PlayOutcome) -> None:
        self._app.post_message(OrchestratorApp.PlayCompleted(outcome))

    async def on_agent_changed(self, agent_id: str, status: AgentStatus) -> None:
        self._app.post_message(OrchestratorApp.AgentChanged(agent_id, status))

    async def on_agent_subprocess_spawned(
        self, agent_id: str, agent_type: AgentType, pid: int
    ) -> None:
        self._app.post_message(OrchestratorApp.AgentSubprocessSpawned(agent_id, agent_type, pid))

    async def on_agent_subprocess_exited(
        self, agent_id: str, agent_type: AgentType, pid: int, exit_code: int | None
    ) -> None:
        self._app.post_message(
            OrchestratorApp.AgentSubprocessExited(agent_id, agent_type, pid, exit_code)
        )

    async def on_feedback_requested(self, reason: str) -> None:
        self._app.post_message(OrchestratorApp.FeedbackRequested(reason))

    async def on_session_paused(self, reason: str) -> None:
        self._app.post_message(OrchestratorApp.SessionPaused(reason))

    async def on_session_draining(self, reason: str) -> None:
        self._app.post_message(OrchestratorApp.SessionDraining(reason))

    async def on_session_ended(self, reason: str) -> None:
        self._app.post_message(OrchestratorApp.SessionEnded(reason))

    async def on_bootstrap_phase(self, phase: str, status: str, elapsed_ms: float) -> None:
        self._app.post_message(OrchestratorApp.BootstrapPhase(phase, status, elapsed_ms))
