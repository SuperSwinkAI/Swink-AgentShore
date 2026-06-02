"""Adapter functions that translate AgentShore engine events into JSON-RPC
notifications on the sidecar stdio transport.

The Orchestrator's natural-exit hook fires the emitters built here so the
sidecar can fan the result out over its stdio JSON-RPC transport. Both
emitters in this module are live: :func:`build_session_completed_emitter`
and :func:`build_esr_ready_emitter` are wired into
``session_lifecycle._start_orchestrator``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.sidecar.server import (
    build_esr_ready_notification,
    build_session_completed_notification,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentshore.sidecar.server import JsonRpcNotification


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
