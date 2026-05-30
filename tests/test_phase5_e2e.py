"""Phase 5 end-to-end tests.

Exercises the full IPC path (server + provider + client), the TUI app with
mock orchestrator, cross-layer protocol compliance, and Phase 6 readiness
import smoke tests.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentshore.ipc.provider import IpcStateProvider
from agentshore.ipc.server import IpcServer
from agentshore.ipc.state_writer import NullStateWriter
from agentshore.plays.base import PlayParams
from agentshore.session_path import IpcEndpoint, find_free_tcp_port
from agentshore.state import (
    BudgetSnapshot,
    OrchestratorState,
    PlayOutcome,
    PlayType,
    SessionState,
    StateProvider,
)
from agentshore.ui.app import OrchestratorApp
from agentshore.ui.provider import TuiStateProvider

type Endpoint = Path | IpcEndpoint

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sock_path() -> Iterator[Endpoint]:
    """Return a platform-supported IPC endpoint."""
    if not hasattr(asyncio, "start_unix_server"):
        yield IpcEndpoint.tcp(port=find_free_tcp_port())
        return

    short_dir = tempfile.mkdtemp(prefix="fm_e2e_", dir="/tmp")
    try:
        yield Path(short_dir) / "test.sock"
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _connect(sock_path: Endpoint) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if isinstance(sock_path, IpcEndpoint):
        return await asyncio.open_connection(sock_path.host, sock_path.port)
    return await asyncio.open_unix_connection(str(sock_path))


def _make_state() -> OrchestratorState:
    return OrchestratorState(
        session_id="e2e-test",
        session_state=SessionState.RUNNING,
        total_plays=5,
        total_cost=1.50,
        budget=BudgetSnapshot(
            total_budget=10.0,
            spent=1.50,
            remaining=8.50,
            estimated_cost_per_play=0.05,
        ),
    )


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


# ===========================================================================
# 1. IPC Agent-Mode E2E
# ===========================================================================


async def test_e2e_state_update_written_to_file(tmp_path: Path) -> None:
    """Provider writes state_update to ``dashboard_state.json`` for the dashboard to tail."""
    from agentshore.ipc.state_writer import STATE_FILENAME, StateWriter

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    provider = IpcStateProvider(StateWriter(session_dir))

    await provider.on_state_update(_make_state())

    state_path = session_dir / STATE_FILENAME
    assert state_path.exists()
    msg = json.loads(state_path.read_text(encoding="utf-8"))
    assert msg["type"] == "state_update"
    assert "id" in msg
    assert "timestamp" in msg
    assert msg["payload"]["session_id"] == "e2e-test"
    assert msg["payload"]["session_state"] == "running"
    assert msg["payload"]["total_plays"] == 5
    assert msg["payload"]["total_cost"] == 1.50


async def test_e2e_play_lifecycle_appended_to_events(tmp_path: Path) -> None:
    """Play-started then play-completed both land in dashboard_events.ndjson."""
    from agentshore.ipc.state_writer import EVENTS_FILENAME, StateWriter

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    provider = IpcStateProvider(StateWriter(session_dir))
    params = PlayParams(agent_id="agent-1", issue_number=42)

    await provider.on_play_started(PlayType.ISSUE_PICKUP, params)
    await provider.on_play_completed(_make_outcome(success=True))

    events_path = session_dir / EVENTS_FILENAME
    lines = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[0]["type"] == "play_event"
    assert lines[0]["payload"]["status"] == "started"
    assert lines[0]["payload"]["play_type"] == "issue_pickup"
    assert lines[0]["payload"]["agent_id"] == "agent-1"
    assert lines[1]["type"] == "play_event"
    assert lines[1]["payload"]["status"] == "completed"
    assert lines[1]["payload"]["play_type"] == "issue_pickup"
    assert lines[1]["payload"]["success"] is True


async def test_e2e_ipc_command_roundtrip(sock_path: Endpoint) -> None:
    """Client sends a pause command; it appears on server.command_queue."""
    server = IpcServer(sock_path)
    await server.start()
    try:
        _reader, writer = await _connect(sock_path)
        await asyncio.sleep(0.05)

        cmd_line = json.dumps({"command": "pause"}) + "\n"
        writer.write(cmd_line.encode("utf-8"))
        await writer.drain()

        cmd = await asyncio.wait_for(server.command_queue.get(), timeout=2.0)
        assert cmd["command"] == "pause"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


async def test_e2e_feedback_requested_written_to_events(tmp_path: Path) -> None:
    """Provider feedback_requested lands in events with correct trigger mapping."""
    from agentshore.ipc.state_writer import EVENTS_FILENAME, StateWriter

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    provider = IpcStateProvider(StateWriter(session_dir))

    await provider.on_feedback_requested("budget_exhausted")

    line = (session_dir / EVENTS_FILENAME).read_text(encoding="utf-8").splitlines()[-1]
    msg = json.loads(line)
    assert msg["type"] == "feedback_requested"
    assert msg["payload"]["trigger"] == "budget_exhaustion"
    assert msg["payload"]["reason"] == "budget_exhausted"


async def test_e2e_ipc_lifecycle_clean_shutdown(sock_path: Endpoint) -> None:
    """Start server, connect a command client, stop server; socket file is removed."""
    server = IpcServer(sock_path)
    await server.start()
    if isinstance(sock_path, IpcEndpoint):
        assert server.endpoint.port == sock_path.port
    else:
        assert sock_path.exists()

    _reader, writer = await _connect(sock_path)
    await asyncio.sleep(0.05)

    # Send a command to confirm the connection is alive.
    writer.write((json.dumps({"command": "pause"}) + "\n").encode("utf-8"))
    await writer.drain()
    cmd = await asyncio.wait_for(server.command_queue.get(), timeout=2.0)
    assert cmd["command"] == "pause"

    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)

    await server.stop()
    if not isinstance(sock_path, IpcEndpoint):
        assert not sock_path.exists()


# ===========================================================================
# 2. Solo TUI E2E
# ===========================================================================


async def test_e2e_tui_state_update_tracked() -> None:
    """Post StateUpdated to OrchestratorApp; _latest_state is updated."""
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        state = _make_state()
        app.post_message(OrchestratorApp.StateUpdated(state))
        await pilot.pause()
        assert app._latest_state is state
        assert app._latest_state.session_id == "e2e-test"


async def test_e2e_tui_help_keybinding() -> None:
    """Press '?'; HelpOverlay appears in screen stack."""
    app = OrchestratorApp()
    async with app.run_test() as pilot:
        await pilot.press("question_mark")
        await pilot.pause()

        from agentshore.ui.screens.help import HelpOverlay

        assert any(isinstance(s, HelpOverlay) for s in app.screen_stack)


# The 'p' (pause) keybinding was removed in commit c4f276b
# (fix: TUI footer cleanup — remove Manual Revert, Quit, Pause, Learnings, Approvals);
# the corresponding pause-resume E2E test has been removed with it.


# ===========================================================================
# 3. Phase 6 Readiness Smoke
# ===========================================================================


def test_ipc_package_imports() -> None:
    """Verify top-level IPC package imports work."""
    from agentshore.ipc import IpcServer, IpcStateProvider, NullStateWriter, StateWriter

    assert IpcServer is not None
    assert IpcStateProvider is not None
    assert NullStateWriter is not None
    assert StateWriter is not None


def test_ui_package_imports() -> None:
    """Verify top-level UI package imports work."""
    from agentshore.ui import OrchestratorApp, TuiStateProvider

    assert OrchestratorApp is not None
    assert TuiStateProvider is not None


def test_screens_package_imports() -> None:
    """Verify all screen classes can be imported from the screens package."""
    from agentshore.ui.screens import (
        AgentDetailScreen,
        EscalationModal,
        GoalsScreen,
        HelpOverlay,
        MainDashboard,
        RevertConfirmModal,
        SessionEndScreen,
        SessionStartupScreen,
    )

    assert MainDashboard is not None
    assert HelpOverlay is not None
    assert GoalsScreen is not None
    assert AgentDetailScreen is not None
    assert SessionStartupScreen is not None
    assert SessionEndScreen is not None
    assert EscalationModal is not None
    assert RevertConfirmModal is not None


# ===========================================================================
# 4. Cross-layer Integration
# ===========================================================================


def test_state_provider_protocol_compliance_ipc() -> None:
    """IpcStateProvider satisfies the StateProvider protocol at runtime."""
    provider = IpcStateProvider(NullStateWriter())
    assert isinstance(provider, StateProvider)


def test_state_provider_protocol_compliance_tui() -> None:
    """TuiStateProvider satisfies the StateProvider protocol at runtime."""
    mock_app = MagicMock(spec=OrchestratorApp)
    provider = TuiStateProvider(mock_app)
    assert isinstance(provider, StateProvider)
