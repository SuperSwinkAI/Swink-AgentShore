"""Tests for the IPC StateProvider (file-backed writer)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from agentshore.ipc.provider import IpcStateProvider
from agentshore.plays.base import PlayParams
from agentshore.state import (
    AgentStatus,
    AgentType,
    OrchestratorState,
    PlayOutcome,
    PlayType,
    SessionState,
    StateProvider,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_writer() -> AsyncMock:
    """Return a mock StateWriter with async write_state + append_event."""
    writer = AsyncMock()
    writer.write_state = AsyncMock()
    writer.append_event = AsyncMock()
    return writer


def _last_state_msg(writer: AsyncMock) -> dict[str, object]:
    """Decode the most-recent write_state argument from its JSON envelope."""
    return json.loads(writer.write_state.await_args[0][0])


def _last_event_msg(writer: AsyncMock) -> dict[str, object]:
    """Decode the most-recent append_event argument from its JSON envelope."""
    return json.loads(writer.append_event.await_args[0][0])


def _make_state(**overrides: object) -> OrchestratorState:
    defaults: dict[str, object] = {
        "session_id": "test",
        "session_state": SessionState.RUNNING,
        "total_plays": 5,
        "total_cost": 1.0,
    }
    defaults.update(overrides)
    return OrchestratorState(**defaults)  # type: ignore[arg-type]


def _make_outcome(success: bool = True) -> PlayOutcome:
    return PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id="agent-1",
        success=success,
        partial=False,
        duration_seconds=10.0,
        token_cost=1000,
        dollar_cost=0.05,
        artifacts=[],
        alignment_delta=0.1,
        play_id=1,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_isinstance_state_provider() -> None:
    """IpcStateProvider must satisfy the StateProvider runtime_checkable protocol."""
    provider = IpcStateProvider(_mock_writer())
    assert isinstance(provider, StateProvider)


@pytest.mark.asyncio
async def test_on_state_update_writes_snapshot() -> None:
    writer = _mock_writer()
    provider = IpcStateProvider(writer)

    await provider.on_state_update(_make_state())

    writer.write_state.assert_awaited_once()
    writer.append_event.assert_not_awaited()
    msg = _last_state_msg(writer)
    assert msg["type"] == "state_update"
    assert "id" in msg
    assert "timestamp" in msg
    assert msg["payload"]["session_id"] == "test"
    assert msg["payload"]["session_state"] == "running"


@pytest.mark.asyncio
async def test_on_state_update_publishes_to_wired_server() -> None:
    """When a server is wired, on_state_update mirrors the envelope into its cache."""
    writer = _mock_writer()

    class _StubServer:
        def __init__(self) -> None:
            self.cached: str | None = None

        def set_cached_state(self, message: str) -> None:
            self.cached = message

    stub = _StubServer()
    provider = IpcStateProvider(writer, server=stub)

    await provider.on_state_update(_make_state())

    assert stub.cached is not None
    parsed = json.loads(stub.cached)
    assert parsed["type"] == "state_update"
    assert parsed["payload"]["session_id"] == "test"
    # The writer received the same envelope so on-disk and in-memory stay in sync.
    assert _last_state_msg(writer) == parsed


@pytest.mark.asyncio
async def test_on_play_started_appends_event() -> None:
    writer = _mock_writer()
    provider = IpcStateProvider(writer)
    params = PlayParams(
        agent_id="agent-1",
        issue_number=42,
        extras={"play_id": 99, "started_at": "2026-01-01T00:00:00Z"},
    )

    await provider.on_play_started(PlayType.ISSUE_PICKUP, params)

    writer.append_event.assert_awaited_once()
    writer.write_state.assert_not_awaited()
    msg = _last_event_msg(writer)
    assert msg["type"] == "play_event"
    assert msg["payload"]["status"] == "started"
    assert msg["payload"]["play_type"] == "issue_pickup"
    assert msg["payload"]["agent_id"] == "agent-1"
    assert msg["payload"]["issue_number"] == 42
    assert msg["payload"]["play_id"] == 99
    assert msg["payload"]["started_at"] == "2026-01-01T00:00:00Z"
    assert msg["payload"]["trigger_agent_id"] is None
    assert msg["payload"]["trigger_agent_type"] is None
    assert msg["payload"]["trigger_error_class"] is None


@pytest.mark.asyncio
async def test_on_play_started_appends_take_break_trigger() -> None:
    writer = _mock_writer()
    provider = IpcStateProvider(writer)
    params = PlayParams(
        extras={
            "trigger_agent_id": "grok-1",
            "trigger_agent_type": "grok",
            "trigger_error_class": "rate_limit",
        }
    )

    await provider.on_play_started(PlayType.TAKE_BREAK, params)

    msg = _last_event_msg(writer)
    assert msg["type"] == "play_event"
    assert msg["payload"]["status"] == "started"
    assert msg["payload"]["play_type"] == "take_break"
    assert msg["payload"]["agent_id"] is None
    assert msg["payload"]["trigger_agent_id"] == "grok-1"
    assert msg["payload"]["trigger_agent_type"] == "grok"
    assert msg["payload"]["trigger_error_class"] == "rate_limit"


@pytest.mark.asyncio
async def test_on_play_completed_success() -> None:
    writer = _mock_writer()
    provider = IpcStateProvider(writer)

    await provider.on_play_completed(_make_outcome(success=True))

    msg = _last_event_msg(writer)
    assert msg["type"] == "play_event"
    assert msg["payload"]["status"] == "completed"
    assert msg["payload"]["success"] is True
    assert msg["payload"]["play_type"] == "issue_pickup"


@pytest.mark.asyncio
async def test_on_play_completed_failure() -> None:
    writer = _mock_writer()
    provider = IpcStateProvider(writer)

    await provider.on_play_completed(_make_outcome(success=False))

    msg = _last_event_msg(writer)
    assert msg["type"] == "play_event"
    assert msg["payload"]["status"] == "failed"
    assert msg["payload"]["success"] is False


@pytest.mark.asyncio
async def test_on_agent_changed_appends() -> None:
    writer = _mock_writer()
    provider = IpcStateProvider(writer)

    await provider.on_agent_changed("agent-2", AgentStatus.BUSY)

    msg = _last_event_msg(writer)
    assert msg["type"] == "agent_changed"
    assert msg["payload"]["agent_id"] == "agent-2"
    assert msg["payload"]["status"] == "busy"


@pytest.mark.asyncio
async def test_on_agent_subprocess_spawned_appends() -> None:
    writer = _mock_writer()
    provider = IpcStateProvider(writer)

    await provider.on_agent_subprocess_spawned("agent-2", AgentType.CODEX, 12345)

    msg = _last_event_msg(writer)
    assert msg["type"] == "agent.subprocess_spawned"
    assert msg["payload"]["agent_id"] == "agent-2"
    assert msg["payload"]["agent_type"] == "codex"
    assert msg["payload"]["pid"] == 12345


@pytest.mark.asyncio
async def test_on_agent_subprocess_exited_appends() -> None:
    writer = _mock_writer()
    provider = IpcStateProvider(writer)

    await provider.on_agent_subprocess_exited("agent-2", AgentType.CODEX, 12345, 1)

    msg = _last_event_msg(writer)
    assert msg["type"] == "agent.subprocess_exited"
    assert msg["payload"]["agent_id"] == "agent-2"
    assert msg["payload"]["agent_type"] == "codex"
    assert msg["payload"]["pid"] == 12345
    assert msg["payload"]["exit_code"] == 1


@pytest.mark.asyncio
async def test_on_feedback_requested_appends() -> None:
    writer = _mock_writer()
    provider = IpcStateProvider(writer)

    await provider.on_feedback_requested("budget_exhausted")

    msg = _last_event_msg(writer)
    assert msg["type"] == "feedback_requested"
    assert msg["payload"]["reason"] == "budget_exhausted"
    assert msg["payload"]["trigger"] == "budget_exhaustion"


@pytest.mark.asyncio
async def test_on_session_paused_appends() -> None:
    writer = _mock_writer()
    provider = IpcStateProvider(writer)

    await provider.on_session_paused("user_requested")

    msg = _last_event_msg(writer)
    assert msg["type"] == "session_paused"
    assert msg["payload"]["reason"] == "user_requested"


# ---------------------------------------------------------------------------
# Session-id stamping (Tier 1 contract)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_carry_session_id_when_provider_has_one() -> None:
    """Every outbound event payload carries session_id so the bridge/browser
    can enforce one-bridge-one-session and reset cleanly on a new session."""
    writer = _mock_writer()
    provider = IpcStateProvider(writer, session_id="sess-xyz")

    await provider.on_play_started(
        PlayType.ISSUE_PICKUP, PlayParams(agent_id="agent-1", issue_number=1)
    )
    assert _last_event_msg(writer)["payload"]["session_id"] == "sess-xyz"

    await provider.on_agent_changed("agent-1", AgentStatus.BUSY)
    assert _last_event_msg(writer)["payload"]["session_id"] == "sess-xyz"

    await provider.on_bootstrap_phase("init_github", "started", 0.0)
    assert _last_event_msg(writer)["payload"]["session_id"] == "sess-xyz"

    await provider.on_session_ended("cli_request")
    assert _last_event_msg(writer)["payload"]["session_id"] == "sess-xyz"


@pytest.mark.asyncio
async def test_state_update_carries_session_id_when_provider_has_one() -> None:
    writer = _mock_writer()
    provider = IpcStateProvider(writer, session_id="sess-xyz")

    # The provider's id wins even if it differs from the state snapshot's
    # (in practice they match; this just asserts the stamp is applied).
    await provider.on_state_update(_make_state(session_id="test"))
    assert _last_state_msg(writer)["payload"]["session_id"] == "sess-xyz"


@pytest.mark.asyncio
async def test_events_omit_session_id_without_provider_id() -> None:
    """Back-compat: with no provider id, event payloads carry no session_id."""
    writer = _mock_writer()
    provider = IpcStateProvider(writer)

    await provider.on_session_ended("cli_request")
    assert "session_id" not in _last_event_msg(writer)["payload"]
