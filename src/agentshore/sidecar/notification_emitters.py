"""Adapter functions that translate AgentShore engine events into JSON-RPC
notifications on the sidecar stdio transport.

Three notification builders live in ``server.py``:

* :func:`build_session_completed_notification`
* :func:`build_agent_subprocess_spawned_notification`
* :func:`build_agent_subprocess_exited_notification`

The Orchestrator and ``AgentManager`` produce events with a different
shape (e.g. ``AgentManager`` exposes ``on_subprocess_spawned`` as an
async callback taking ``agent_id``, ``AgentType``, ``pid``). This module
sits between them, turning each engine-side event into the JSON-RPC
payload shape so the Rust supervisor can react.

Currently the connectors are not invoked anywhere — the orchestrator
boot path that constructs an ``AgentManager`` inside the sidecar process
is still deferred (DESIGN §5.1 / desktop-0vc.11.2). They live here as
self-contained adapters with tests so that wiring is straightforward
once the orchestrator boot lands.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.sidecar.server import (
    build_agent_subprocess_exited_notification,
    build_agent_subprocess_spawned_notification,
    build_esr_ready_notification,
    build_session_completed_notification,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agentshore.sidecar.server import JsonRpcNotification
    from agentshore.state import AgentType


def build_agent_subprocess_callbacks(
    notify: Callable[[JsonRpcNotification], None],
) -> tuple[
    Callable[[str, AgentType, int], Awaitable[None]],
    Callable[[str, AgentType, int, int | None], Awaitable[None]],
]:
    """Return ``(on_spawned, on_exited)`` callbacks for ``AgentManager``.

    Each callback receives the AgentManager-side event signature and
    forwards it through ``notify`` as the JSON-RPC notification shape
    documented in DESIGN §5.1. ``agent_type.value`` is the wire format
    (a stable string like ``"claude_code"``, ``"codex"``, etc.).
    """

    async def on_spawned(agent_id: str, agent_type: AgentType, pid: int) -> None:
        notify(
            build_agent_subprocess_spawned_notification(
                agent_id=agent_id, agent_type=agent_type.value, pid=pid
            )
        )

    async def on_exited(
        agent_id: str, agent_type: AgentType, pid: int, exit_code: int | None
    ) -> None:
        notify(
            build_agent_subprocess_exited_notification(
                agent_id=agent_id,
                agent_type=agent_type.value,
                pid=pid,
                exit_code=exit_code,
            )
        )

    return on_spawned, on_exited


def build_session_completed_emitter(
    notify: Callable[[JsonRpcNotification], None],
) -> Callable[[dict[str, object]], None]:
    """Return a callback the Orchestrator can fire on natural exit.

    The Orchestrator's natural-exit hook calls this with the same payload
    shape ``session.stop`` returns — ``{session_id, exit_reason, exit_code,
    archive_path, report_path, log_path, esr_summary}`` — so Screen 10 receives
    identical data over either transport (DESIGN §5.2).
    """

    def emit(payload: dict[str, object]) -> None:
        notify(build_session_completed_notification(payload))

    return emit


def build_esr_ready_emitter(
    notify: Callable[[JsonRpcNotification], None],
) -> Callable[[str, str, str, str | None], None]:
    """Return a sync callback the orchestrator drains through on ESR ready.

    Issue #561: in embedded mode the engine no longer opens the OS browser
    on the static HTML report — instead it calls this emitter with
    ``(session_id, archive_path, report_path, log_path)`` and the sidecar
    fans a ``$/esr_ready`` JSON-RPC notification out to the Tauri shell,
    which navigates to ``/session/esr``. The notification is intentionally
    lightweight; the full ESR payload still arrives on ``session.completed``.
    """

    def emit(
        session_id: str,
        archive_path: str,
        report_path: str,
        log_path: str | None,
    ) -> None:
        notify(
            build_esr_ready_notification(
                session_id=session_id,
                archive_path=archive_path,
                report_path=report_path,
                log_path=log_path,
            )
        )

    return emit
