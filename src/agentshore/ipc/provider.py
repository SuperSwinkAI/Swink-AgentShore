"""IPC StateProvider — writes serialized state to the file-backed sink.

The orchestrator dispatches every state snapshot and lifecycle event
through this provider. State snapshots overwrite
``dashboard_state.json`` atomically; events append to
``dashboard_events.ndjson``. The dashboard sidecar tails both files —
see :class:`agentshore.ipc.state_writer.StateWriter` and
:class:`agentshore.dashboard.bridge.DashboardBridge`.

The streaming IPC socket (:class:`agentshore.ipc.server.IpcServer`) is now
command-inbound only: it parses NDJSON commands sent by the dashboard
and places them on a queue for the orchestrator. The provider no longer
broadcasts state back through it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

from agentshore.ipc.serializer import (
    make_message,
    serialize_feedback_requested,
    serialize_play_event,
    serialize_state,
)
from agentshore.state import AgentStatus, AgentType, OrchestratorState, PlayOutcome, PlayType

if TYPE_CHECKING:
    from agentshore.plays.base import PlayParams


class _WriterProtocol(Protocol):
    async def write_state(self, message: str) -> None: ...
    async def append_event(self, message: str) -> None: ...


class _ServerProtocol(Protocol):
    def set_cached_state(self, message: str) -> None: ...


class IpcStateProvider:
    """Persist serialized AgentShore state to disk for the dashboard to tail.

    Satisfies the :class:`~agentshore.state.StateProvider` protocol. State
    snapshots are written via ``writer.write_state``; lifecycle events
    are appended via ``writer.append_event``. When an ``IpcServer`` is
    wired in, every state snapshot is also published into the server's
    in-memory cache so on-demand ``get_state`` commands can reply
    directly without waiting for the next heartbeat.
    """

    def __init__(self, writer: _WriterProtocol, server: _ServerProtocol | None = None) -> None:
        self._writer = writer
        self._server = server

    # -- StateProvider hooks --------------------------------------------------

    async def on_state_update(self, state: OrchestratorState) -> None:
        """Persist the latest full state snapshot."""
        msg = make_message("state_update", serialize_state(state))
        await self._writer.write_state(msg)
        if self._server is not None:
            self._server.set_cached_state(msg)

    async def on_play_started(self, play_type: PlayType, params: PlayParams) -> None:
        """Append a play-started event with a partial payload."""
        payload: dict[str, object] = {
            "play_type": play_type.value,
            "status": "started",
            "agent_id": params.agent_id,
            "issue_number": params.issue_number,
            "pr_number": params.pr_number,
            "branch": params.branch,
            "play_id": params.extras.get("play_id"),
            "started_at": params.extras.get("started_at"),
            "trigger_agent_id": params.extras.get("trigger_agent_id"),
            "trigger_agent_type": params.extras.get("trigger_agent_type"),
            "trigger_error_class": params.extras.get("trigger_error_class"),
        }
        await self._writer.append_event(make_message("play_event", payload))

    async def on_play_completed(self, play: PlayOutcome) -> None:
        """Append a play-completed (or failed) event."""
        status: Literal["completed", "failed"] = "completed" if play.success else "failed"
        await self._writer.append_event(
            make_message("play_event", serialize_play_event(play, status))
        )

    async def on_agent_changed(self, agent_id: str, status: AgentStatus) -> None:
        """Append an agent status change event."""
        await self._writer.append_event(
            make_message(
                "agent_changed",
                {"agent_id": agent_id, "status": status.value},
            )
        )

    async def on_agent_subprocess_spawned(
        self, agent_id: str, agent_type: AgentType, pid: int
    ) -> None:
        """Append an agent subprocess-spawned event."""
        await self._writer.append_event(
            make_message(
                "agent.subprocess_spawned",
                {
                    "agent_id": agent_id,
                    "agent_type": agent_type.value,
                    "pid": pid,
                },
            )
        )

    async def on_agent_subprocess_exited(
        self, agent_id: str, agent_type: AgentType, pid: int, exit_code: int | None
    ) -> None:
        """Append an agent subprocess-exited event."""
        await self._writer.append_event(
            make_message(
                "agent.subprocess_exited",
                {
                    "agent_id": agent_id,
                    "agent_type": agent_type.value,
                    "pid": pid,
                    "exit_code": exit_code,
                },
            )
        )

    async def on_feedback_requested(self, reason: str) -> None:
        """Append a feedback-requested escalation event."""
        await self._writer.append_event(
            make_message("feedback_requested", serialize_feedback_requested(reason))
        )

    async def on_session_paused(self, reason: str) -> None:
        """Append a session-paused event."""
        await self._writer.append_event(make_message("session_paused", {"reason": reason}))

    async def on_session_draining(self, reason: str) -> None:
        """Append a session-draining event: graceful shutdown has begun."""
        await self._writer.append_event(make_message("session_draining", {"reason": reason}))

    async def on_session_ended(self, reason: str) -> None:
        """Append a session-ended event (clean completion, not a crash)."""
        await self._writer.append_event(make_message("session_ended", {"reason": reason}))

    async def on_bootstrap_phase(self, phase: str, status: str, elapsed_ms: float) -> None:
        """Append a bootstrap-phase event so the dashboard can render a loading modal."""
        await self._writer.append_event(
            make_message(
                "bootstrap_phase",
                {"phase": phase, "status": status, "elapsed_ms": elapsed_ms},
            )
        )
