"""Tests for src/agentshore/dashboard/bridge.py — file-backed WS bridge.

The bridge tails ``dashboard_state.json`` and ``dashboard_events.ndjson``
in the session directory (written by
:class:`agentshore.ipc.state_writer.StateWriter`) and fans new content out
to connected browser WebSockets. Commands sent by the browser are
forwarded to the IPC command server.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentshore.dashboard.bridge import DashboardBridge
from agentshore.ipc.server import IpcServer
from agentshore.ipc.state_writer import EVENTS_FILENAME, STATE_FILENAME, StateWriter
from agentshore.session_path import IpcEndpoint, find_free_tcp_port

type Endpoint = Path | IpcEndpoint


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture()
def sock_path() -> Iterator[Endpoint]:
    """Return a platform-supported IPC endpoint."""
    if not hasattr(asyncio, "start_unix_server"):
        yield IpcEndpoint.tcp(port=find_free_tcp_port())
        return

    short_dir = tempfile.mkdtemp(prefix="fmd_", dir="/tmp")
    try:
        yield Path(short_dir) / "f.sock"
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)


@pytest.fixture()
def static_dir(tmp_path: Path) -> Path:
    """Create a minimal static directory with an index.html."""
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<html><body>test</body></html>")
    return d


def _make_bridge(
    sock_path: Endpoint,
    static_dir: Path,
    session_dir: Path,
) -> DashboardBridge:
    endpoint = sock_path if isinstance(sock_path, IpcEndpoint) else IpcEndpoint.unix(sock_path)
    return DashboardBridge(
        ipc_endpoint=endpoint,
        session_dir=session_dir,
        port=0,
        static_dir=static_dir,
        state_poll_interval=0.02,
        events_poll_interval=0.02,
    )


# ---------------------------------------------------------------------------
# File watcher behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_picks_up_state_snapshot(
    sock_path: Endpoint, static_dir: Path, tmp_path: Path
) -> None:
    """A state file change is read by the watcher and broadcast to clients."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    writer = StateWriter(session_dir)

    bridge = _make_bridge(sock_path, static_dir, session_dir)
    bridge._running = True

    fake_ws = FakeWS()
    bridge._ws_clients.append(fake_ws)  # type: ignore[arg-type]

    payload = json.dumps({"type": "state_update", "payload": {"tick": 7}})
    await writer.write_state(payload)

    # Poll up to ~1s for the watcher to pick the change up.
    for _ in range(50):
        if any(json.loads(m).get("payload", {}).get("tick") == 7 for m in fake_ws.sent):
            break
        await asyncio.to_thread(bridge._poll_state_file_sync)
        if bridge._latest_state is not None:
            await bridge._broadcast(bridge._latest_state)
        await asyncio.sleep(0.02)

    assert bridge._latest_state is not None
    assert "tick" in bridge._latest_state
    assert any("tick" in m for m in fake_ws.sent)


@pytest.mark.asyncio
async def test_bridge_caches_active_play_and_event_history(
    sock_path: Endpoint, static_dir: Path, tmp_path: Path
) -> None:
    """The bridge derives active_play and recent events from the events file."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    writer = StateWriter(session_dir)

    bridge = _make_bridge(sock_path, static_dir, session_dir)

    started = json.dumps(
        {
            "type": "play_event",
            "payload": {
                "play_type": "issue_pickup",
                "status": "started",
                "agent_id": "agent-1",
                "issue_number": 42,
                "play_id": 7,
            },
        }
    )
    await writer.append_event(started)

    # Drive the tail loop manually.
    lines = await asyncio.to_thread(bridge._read_new_events_sync)
    for line in lines:
        bridge._ingest_event_line(line, broadcast=False)

    assert bridge._active_play is not None
    assert bridge._active_play["play_type"] == "issue_pickup"
    assert bridge._active_play["agent_id"] == "agent-1"
    assert len(bridge._event_history) == 1
    assert bridge._event_history[0]["status"] == "started"


@pytest.mark.asyncio
async def test_bridge_clears_active_play_on_completion(
    sock_path: Endpoint, static_dir: Path, tmp_path: Path
) -> None:
    """A completed play_event clears the cached active play."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    writer = StateWriter(session_dir)

    bridge = _make_bridge(sock_path, static_dir, session_dir)
    await writer.append_event(
        json.dumps(
            {
                "type": "play_event",
                "payload": {
                    "play_type": "issue_pickup",
                    "status": "started",
                    "agent_id": "agent-1",
                    "play_id": 7,
                },
            }
        )
    )
    await writer.append_event(
        json.dumps(
            {
                "type": "play_event",
                "payload": {
                    "play_type": "issue_pickup",
                    "status": "completed",
                    "agent_id": "agent-1",
                    "play_id": 7,
                },
            }
        )
    )
    for line in await asyncio.to_thread(bridge._read_new_events_sync):
        bridge._ingest_event_line(line, broadcast=False)

    assert bridge._active_play is None


@pytest.mark.asyncio
async def test_bridge_replay_to_new_client(
    sock_path: Endpoint, static_dir: Path, tmp_path: Path
) -> None:
    """Cached state + events + active play are replayed to a new WS client."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    bridge = _make_bridge(sock_path, static_dir, session_dir)
    bridge._latest_state = json.dumps({"type": "state_update", "payload": {"tick": 1}})
    bridge._active_play = {"play_type": "issue_pickup"}
    bridge._event_history = [{"type": "play_event", "status": "started"}]

    ws = FakeWS()
    await bridge._replay_to_ws(ws)  # type: ignore[arg-type]

    types = [json.loads(m).get("type") for m in ws.sent]
    assert types == ["state_update", "active_play_replay", "event_history_replay"]


@pytest.mark.asyncio
async def test_bridge_prime_from_disk_loads_existing_files(
    sock_path: Endpoint, static_dir: Path, tmp_path: Path
) -> None:
    """On startup the bridge reads any state already on disk."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / STATE_FILENAME).write_text(
        json.dumps({"type": "state_update", "payload": {"tick": 99}}),
        encoding="utf-8",
    )
    (session_dir / EVENTS_FILENAME).write_text(
        json.dumps(
            {
                "type": "play_event",
                "payload": {
                    "play_type": "issue_pickup",
                    "status": "started",
                    "agent_id": "agent-1",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    bridge = _make_bridge(sock_path, static_dir, session_dir)
    await bridge._prime_from_disk()

    assert bridge._latest_state is not None
    assert "tick" in bridge._latest_state
    assert bridge._active_play is not None
    assert bridge._active_play["play_type"] == "issue_pickup"


@pytest.mark.asyncio
async def test_prime_from_disk_ignores_stale_session_ended(
    sock_path: Endpoint, static_dir: Path, tmp_path: Path
) -> None:
    """A `session_ended` line read during prime must not set should_exit.

    Regression for the v0.13.x file-backed IPC: when the events file
    contained a `session_ended` from a prior session, the bridge's prime
    flagged the new session as ended and uvicorn exited within ms of
    binding. Live tails still honour the marker — only prime-from-disk
    is filtered.
    """
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / EVENTS_FILENAME).write_text(
        json.dumps({"type": "session_ended", "payload": {"reason": "cli_request"}})
        + "\n"
        + json.dumps({"type": "session_draining", "payload": {"reason": "cli_request"}})
        + "\n",
        encoding="utf-8",
    )

    bridge = _make_bridge(sock_path, static_dir, session_dir)
    await bridge._prime_from_disk()

    assert bridge._session_ended is False
    assert bridge._session_ended_raw is None
    assert bridge._session_draining_raw is None

    # A live tail (broadcast=True) still flips the flag.
    bridge._ingest_event_line(
        json.dumps({"type": "session_ended", "payload": {"reason": "cli_request"}}),
        broadcast=True,
    )
    assert bridge._session_ended is True


@pytest.mark.asyncio
async def test_events_tail_resumes_after_rotation(
    sock_path: Endpoint, static_dir: Path, tmp_path: Path
) -> None:
    """If the events file shrinks (rotation), the tail restarts from byte 0."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    writer = StateWriter(session_dir)

    bridge = _make_bridge(sock_path, static_dir, session_dir)
    bridge._running = True

    big_payload = "x" * 4096
    await writer.append_event(
        json.dumps(
            {
                "type": "play_event",
                "payload": {
                    "play_type": "x",
                    "status": "started",
                    "play_id": 1,
                    "filler": big_payload,
                },
            }
        )
    )
    lines = await asyncio.to_thread(bridge._read_new_events_sync)
    assert len(lines) == 1

    # Simulate rotation by truncating the file to a shorter payload.
    rotated = json.dumps(
        {"type": "play_event", "payload": {"play_type": "y", "status": "started", "play_id": 2}}
    )
    (session_dir / EVENTS_FILENAME).write_text(rotated + "\n", encoding="utf-8")

    lines = await asyncio.to_thread(bridge._read_new_events_sync)
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["payload"]["play_id"] == 2


# ---------------------------------------------------------------------------
# Command forwarding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_forwards_command_to_ipc(
    sock_path: Endpoint, static_dir: Path, tmp_path: Path
) -> None:
    """Valid commands from WebSocket are forwarded to the IPC socket."""
    server = IpcServer(sock_path)
    await server.start()

    bridge = _make_bridge(sock_path, static_dir, tmp_path / "session")
    bridge._running = True

    try:
        assert await bridge._ensure_ipc_writer()

        fake_ws = FakeWS()
        bridge._controlling_ws = fake_ws  # type: ignore[assignment]
        cmd = json.dumps({"command": "pause", "token": bridge._auth_token})
        await bridge._handle_ws_command(cmd, fake_ws)  # type: ignore[arg-type]

        received = await asyncio.wait_for(server.command_queue.get(), timeout=2.0)
        assert received["command"] == "pause"
        assert "token" not in received
    finally:
        bridge._running = False
        await bridge._disconnect_ipc()
        await server.stop()


@pytest.mark.asyncio
async def test_bridge_rejects_invalid_command(
    sock_path: Endpoint, static_dir: Path, tmp_path: Path
) -> None:
    """Invalid commands get an error response sent back."""
    bridge = _make_bridge(sock_path, static_dir, tmp_path / "session")
    bridge._running = True

    fake_ws = FakeWS()

    await bridge._handle_ws_command("not json", fake_ws)  # type: ignore[arg-type]
    assert len(fake_ws.sent) == 1
    error = json.loads(fake_ws.sent[0])
    assert error["type"] == "error"
    assert "Invalid JSON" in error["error"]


@pytest.mark.asyncio
async def test_bridge_error_on_unknown_command(
    sock_path: Endpoint, static_dir: Path, tmp_path: Path
) -> None:
    """Unknown commands get an error response."""
    bridge = _make_bridge(sock_path, static_dir, tmp_path / "session")
    bridge._running = True

    fake_ws = FakeWS()
    bridge._controlling_ws = fake_ws  # type: ignore[assignment]

    cmd = json.dumps({"command": "explode_everything", "token": bridge._auth_token})
    await bridge._handle_ws_command(cmd, fake_ws)  # type: ignore[arg-type]
    assert len(fake_ws.sent) == 1
    error = json.loads(fake_ws.sent[0])
    assert error["type"] == "error"
    assert "Unknown command" in error["error"]


@pytest.mark.asyncio
async def test_bridge_error_when_ipc_unreachable(
    sock_path: Endpoint, static_dir: Path, tmp_path: Path
) -> None:
    """Commands fail gracefully when the IPC endpoint isn't accepting."""
    bridge = _make_bridge(sock_path, static_dir, tmp_path / "session")
    bridge._running = True

    fake_ws = FakeWS()
    bridge._controlling_ws = fake_ws  # type: ignore[assignment]

    cmd = json.dumps({"command": "pause", "token": bridge._auth_token})
    await bridge._handle_ws_command(cmd, fake_ws)  # type: ignore[arg-type]
    assert len(fake_ws.sent) == 1
    error = json.loads(fake_ws.sent[0])
    assert error["type"] == "error"
    assert "Not connected" in error["error"]


@pytest.mark.asyncio
async def test_bridge_rejects_read_only_client(
    sock_path: Endpoint, static_dir: Path, tmp_path: Path
) -> None:
    """Commands from non-controlling clients are rejected."""
    bridge = _make_bridge(sock_path, static_dir, tmp_path / "session")
    bridge._running = True

    controller = FakeWS()
    reader = FakeWS()
    bridge._controlling_ws = controller  # type: ignore[assignment]

    cmd = json.dumps({"command": "pause", "token": bridge._auth_token})
    await bridge._handle_ws_command(cmd, reader)  # type: ignore[arg-type]
    error = json.loads(reader.sent[0])
    assert error["type"] == "error"
    assert "Read-only" in error["error"]


@pytest.mark.asyncio
async def test_bridge_allows_read_only_feedback_response(
    sock_path: Endpoint, static_dir: Path, tmp_path: Path
) -> None:
    """Any visible dashboard tab can answer a blocking feedback prompt."""
    server = IpcServer(sock_path)
    await server.start()

    bridge = _make_bridge(sock_path, static_dir, tmp_path / "session")
    bridge._running = True

    try:
        assert await bridge._ensure_ipc_writer()

        controller = FakeWS()
        reader = FakeWS()
        bridge._controlling_ws = controller  # type: ignore[assignment]

        cmd = json.dumps({"command": "feedback_response", "action": "continue"})
        await bridge._handle_ws_command(cmd, reader)  # type: ignore[arg-type]

        received = await asyncio.wait_for(server.command_queue.get(), timeout=2.0)
        assert received == {"command": "feedback_response", "action": "continue"}
        assert reader.sent == []
    finally:
        bridge._running = False
        await bridge._disconnect_ipc()
        await server.stop()


@pytest.mark.asyncio
async def test_bridge_rejects_wrong_token(
    sock_path: Endpoint, static_dir: Path, tmp_path: Path
) -> None:
    """Commands from the controller still need the bridge token."""
    bridge = _make_bridge(sock_path, static_dir, tmp_path / "session")
    bridge._running = True

    fake_ws = FakeWS()
    bridge._controlling_ws = fake_ws  # type: ignore[assignment]

    cmd = json.dumps({"command": "pause", "token": "wrong"})
    await bridge._handle_ws_command(cmd, fake_ws)  # type: ignore[arg-type]
    error = json.loads(fake_ws.sent[0])
    assert error["type"] == "error"
    assert "Read-only" in error["error"]


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_server_exits_after_session_ended(
    sock_path: Endpoint, static_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HTTP server should stop once a session_ended event is seen."""
    import uvicorn

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    bridge = _make_bridge(sock_path, static_dir, session_dir)

    class FakeServer:
        def __init__(self, _config: object) -> None:
            self.started = True
            self._should_exit = False
            self.exit_requested = asyncio.Event()

        @property
        def should_exit(self) -> bool:
            return self._should_exit

        @should_exit.setter
        def should_exit(self, value: bool) -> None:
            self._should_exit = value
            if value:
                self.exit_requested.set()

        async def serve(self) -> None:
            await self.exit_requested.wait()

    fake_servers: list[FakeServer] = []

    def fake_server_factory(config: object) -> FakeServer:
        srv = FakeServer(config)
        fake_servers.append(srv)
        return srv

    monkeypatch.setattr(uvicorn, "Config", lambda *args, **kwargs: object())
    monkeypatch.setattr(uvicorn, "Server", fake_server_factory)

    async def fire_session_ended() -> None:
        # Wait until the bridge has finished priming + started its server.
        for _ in range(200):
            if fake_servers:
                break
            await asyncio.sleep(0.01)
        bridge._session_ended = True

    fire_task = asyncio.create_task(fire_session_ended())
    try:
        await asyncio.wait_for(bridge.start(), timeout=2.0)
    finally:
        fire_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await fire_task

    assert len(fake_servers) == 1
    assert fake_servers[0].should_exit is True


# ---------------------------------------------------------------------------
# Packaging smoke
# ---------------------------------------------------------------------------


def test_default_static_assets_exist() -> None:
    """Source checkout includes production dashboard assets."""
    static_dir = Path(__file__).parents[2] / "src" / "agentshore" / "dashboard" / "static"
    assert (static_dir / "index.html").exists()


def test_websocket_backend_is_a_base_dependency() -> None:
    """Dashboard server stack (uvicorn + websockets) ships in base deps."""
    pyproject = Path(__file__).parents[2] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    base_deps = data["project"]["dependencies"]
    assert any(dep.startswith("websockets") for dep in base_deps)
    assert any(dep.startswith("uvicorn") for dep in base_deps)
    assert any(dep.startswith("starlette") for dep in base_deps)
