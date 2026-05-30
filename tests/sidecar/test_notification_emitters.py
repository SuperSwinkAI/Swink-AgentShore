"""Tests for the notification emitter adapters (DESIGN §5.1, desktop-8e1)."""

from __future__ import annotations

import asyncio
from typing import cast

from agentshore.sidecar.notification_emitters import (
    build_agent_subprocess_callbacks,
    build_session_completed_emitter,
)
from agentshore.sidecar.server import JsonRpcNotification
from agentshore.state import AgentType


def test_agent_subprocess_callbacks_emit_spawned_shape() -> None:
    notifications: list[JsonRpcNotification] = []
    on_spawned, _on_exited = build_agent_subprocess_callbacks(notifications.append)

    asyncio.run(on_spawned("agent-7", AgentType.CLAUDE_CODE, 42101))

    assert len(notifications) == 1
    note = notifications[0]
    assert note["method"] == "agent.subprocess_spawned"
    params = cast("dict[str, object]", note["params"])
    assert params == {
        "agent_id": "agent-7",
        "agent_type": AgentType.CLAUDE_CODE.value,
        "pid": 42101,
    }


def test_agent_subprocess_callbacks_emit_exited_shape() -> None:
    notifications: list[JsonRpcNotification] = []
    _on_spawned, on_exited = build_agent_subprocess_callbacks(notifications.append)

    asyncio.run(on_exited("agent-7", AgentType.CODEX, 42101, 0))

    assert len(notifications) == 1
    note = notifications[0]
    assert note["method"] == "agent.subprocess_exited"
    params = cast("dict[str, object]", note["params"])
    assert params == {
        "agent_id": "agent-7",
        "agent_type": AgentType.CODEX.value,
        "pid": 42101,
        "exit_code": 0,
    }


def test_agent_subprocess_exited_preserves_none_exit_code() -> None:
    """A killed-by-signal subprocess can land with ``exit_code=None``."""
    notifications: list[JsonRpcNotification] = []
    _on_spawned, on_exited = build_agent_subprocess_callbacks(notifications.append)

    asyncio.run(on_exited("agent-9", AgentType.GEMINI, 42103, None))

    params = cast("dict[str, object]", notifications[0]["params"])
    assert params["exit_code"] is None


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
