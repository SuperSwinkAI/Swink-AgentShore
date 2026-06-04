"""Tests for the notification emitter adapters (DESIGN §5.1, desktop-8e1)."""

from __future__ import annotations

from agentshore.sidecar.notification_emitters import build_session_completed_emitter
from agentshore.sidecar.server import JsonRpcNotification


def test_session_completed_emitter_passes_payload_through() -> None:
    notifications: list[JsonRpcNotification] = []
    emit = build_session_completed_emitter(notifications.append)
    payload: dict[str, object] = {
        "session_id": "sess-1",
        "exit_reason": "natural_exit",
        "exit_code": 0,
        "archive_path": "/var/lib/agentshore/archives/sess-1",
        "report_path": "/var/lib/agentshore/archives/sess-1/report.html",
        "log_path": "/var/lib/agentshore/logs/agentshore-sess-1.log",
        "esr_summary": {"plays": 12, "merges": 3},
    }

    emit(payload)

    assert len(notifications) == 1
    note = notifications[0]
    assert note["method"] == "session.completed"
    assert note["params"] == payload
