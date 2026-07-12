"""Shared helpers used by both the router and session handler.

Progress-notification builders and session phase constants live here
so they can be imported by both :mod:`.router` and :mod:`.handlers.session`
without creating a circular dependency.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agentshore.sidecar.rpc.protocol import (
    JsonRpcNotification,
    notification,
)

# ---------------------------------------------------------------------------
# Session phase tables (DESIGN §10.2 / §5.1 / §5.2)
# ---------------------------------------------------------------------------

# DESIGN §10.2 — the seven startup phases reported by ``session.start`` so the
# desktop Screen 8 checklist can advance step-by-step. Each phase emits a
# ``running`` (percent=0) notification followed by an ``ok`` (percent=100)
# notification on the same ``step`` id. Step ids must match
# ``STARTUP_STEP_IDS`` in ``desktop/src/startupSteps.ts``. The canonical
# ordering lives in ``session_lifecycle.SESSION_START_STEP_IDS``; this table
# mirrors it for the legacy stub emitter.
SESSION_START_PHASES: tuple[tuple[str, str], ...] = (
    ("config_merge", "Config merged"),
    ("check_agent_auth", "Agent auth checked"),
    ("install_skills", "Skills installed"),
    ("init_beads", "Beads ready"),
    ("bind_ipc", "IPC endpoint bound"),
    ("start_bridge", "Dashboard bridge starting"),
    ("first_snapshot", "First state snapshot"),
)


# DESIGN §5.1 / §5.2 — drain-mode ``session.stop`` reports phase progress as
# it walks through graceful shutdown. ``hard`` mode skips the drain wait and
# emits a single completion event. Phase ids are stable so the desktop shell
# can render a step list parallel to the startup checklist.
SESSION_STOP_DRAIN_PHASES: tuple[tuple[str, str], ...] = (
    ("cancel_pending", "Cancelling queued plays"),
    ("await_inflight", "Awaiting in-flight plays"),
    ("archive_session", "Archiving session"),
    ("generate_report", "Generating ESR report"),
)


# ---------------------------------------------------------------------------
# Progress notification builder
# ---------------------------------------------------------------------------


def _progress_notification(
    token: object,
    *,
    step: str,
    percent: int,
    message: str,
) -> JsonRpcNotification:
    return notification(
        "$/progress",
        {
            "token": token,
            "step": step,
            "percent": percent,
            "message": message,
        },
    )


# ---------------------------------------------------------------------------
# Named notification builders (re-exported from server.py public surface)
# ---------------------------------------------------------------------------


def build_session_completed_notification(payload: dict[str, object]) -> JsonRpcNotification:
    """Build the ``session.completed`` JSON-RPC notification (DESIGN §5.2).

    Callers (typically the embedded bridge on a self-driven orchestrator exit)
    pass the same payload returned by ``session.stop`` so Screen 10 receives
    identical data on both transports.
    """
    return notification("session.completed", payload)


def build_esr_ready_notification(
    *,
    session_id: str,
    archive_path: str,
    report_path: str,
    log_path: str | None,
) -> JsonRpcNotification:
    """Build the ``$/esr_ready`` JSON-RPC notification (issue #561).

    Fires from the engine's drain loop the moment the static ESR HTML file
    has been generated, replacing the legacy ``webbrowser.open`` handoff for
    embedded (desktop) sessions. Carries the core-provided locators the
    shell needs to navigate — the richer ``session.completed`` notification
    delivers the full ESR payload immediately after.
    """
    return notification(
        "$/esr_ready",
        {
            "session_id": session_id,
            "archive_path": archive_path,
            "report_path": report_path,
            "log_path": log_path,
        },
    )


def build_session_draining_notification(
    *,
    session_id: str,
    reason: str,
) -> JsonRpcNotification:
    """Build the ``session.draining`` JSON-RPC notification.

    Fires from ``begin_drain`` — the earliest point in graceful shutdown,
    well before ESR HTML generation starts. Lets the Tauri shell's heartbeat
    watchdog stand down as soon as drain begins rather than waiting for
    ``$/esr_ready`` (which only arrives after the unbounded, O(plays/agents)
    report-generation step completes).
    """
    return notification(
        "session.draining",
        {
            "session_id": session_id,
            "reason": reason,
        },
    )


def build_sidecar_health_notification() -> JsonRpcNotification:
    return notification(
        "sidecar.health",
        {"status": "ok", "timestamp": datetime.now(UTC).isoformat()},
    )
