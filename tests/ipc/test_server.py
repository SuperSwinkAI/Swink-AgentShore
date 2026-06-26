"""Tests for src/agentshore/ipc/server.py — IPC command-channel server.

The IPC server is now inbound-only: it parses NDJSON commands sent by
the dashboard bridge (or other clients) and places them on a queue for
the orchestrator. State updates are no longer broadcast over the
socket; those go through the file-backed
:class:`agentshore.ipc.state_writer.StateWriter` and the bridge tails the
files. See :mod:`agentshore.dashboard.bridge` for the consumer side.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentshore.ipc.server import IpcServer
from agentshore.session_path import IpcEndpoint, find_free_tcp_port

type Endpoint = Path | IpcEndpoint


@pytest.fixture()
def sock_path() -> Iterator[Endpoint]:
    """Return a platform-supported IPC endpoint."""
    if not hasattr(asyncio, "start_unix_server"):
        yield IpcEndpoint.tcp(port=find_free_tcp_port())
        return

    short_dir = tempfile.mkdtemp(prefix="fm_", dir="/tmp")
    try:
        yield Path(short_dir) / "f.sock"
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)


async def _connect(
    socket_path: Endpoint,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if isinstance(socket_path, IpcEndpoint):
        return await asyncio.open_connection(socket_path.host, socket_path.port)
    return await asyncio.open_unix_connection(str(socket_path))


async def _read_line(reader: asyncio.StreamReader, timeout: float = 2.0) -> str:
    data = await asyncio.wait_for(reader.readline(), timeout=timeout)
    return data.decode("utf-8").strip()


def _create_stale_unix_socket(path: Path) -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
    finally:
        sock.close()


@pytest.mark.asyncio
async def test_server_creates_socket(sock_path: Endpoint) -> None:
    """Server exposes a usable platform endpoint after start()."""
    server = IpcServer(sock_path)
    await server.start()
    try:
        if isinstance(sock_path, IpcEndpoint):
            assert server.endpoint.kind == "tcp"
            assert server.endpoint.port > 0
        else:
            assert sock_path.exists()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_server_socket_is_owner_only(sock_path: Endpoint) -> None:
    """Unix socket is created with 0o600 permissions."""
    if isinstance(sock_path, IpcEndpoint):
        pytest.skip("TCP endpoint has no filesystem permissions to check")

    server = IpcServer(sock_path)
    await server.start()
    try:
        mode = sock_path.stat().st_mode & 0o777
        assert mode == 0o600
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_server_removes_socket_on_stop(sock_path: Endpoint) -> None:
    """Unix socket file is unlinked when the server stops."""
    if isinstance(sock_path, IpcEndpoint):
        pytest.skip("TCP endpoint has no filesystem entry to clean up")

    server = IpcServer(sock_path)
    await server.start()
    assert sock_path.exists()
    await server.stop()
    assert not sock_path.exists()


@pytest.mark.asyncio
async def test_stale_socket_cleanup(sock_path: Endpoint) -> None:
    """A leftover Unix socket at the bind path is unlinked before binding."""
    if isinstance(sock_path, IpcEndpoint):
        pytest.skip("Stale-socket cleanup only applies to Unix endpoints")

    _create_stale_unix_socket(sock_path)
    assert sock_path.exists()

    server = IpcServer(sock_path)
    await server.start()
    try:
        # Server should have replaced the stale socket and be listening.
        _reader, writer = await _connect(sock_path)
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_stale_socket_cleanup_refuses_regular_file(sock_path: Endpoint) -> None:
    """A non-socket file at the bind path raises rather than being deleted."""
    if isinstance(sock_path, IpcEndpoint):
        pytest.skip("Stale-socket cleanup only applies to Unix endpoints")

    sock_path.write_text("not a socket", encoding="utf-8")

    server = IpcServer(sock_path)
    with pytest.raises(RuntimeError, match="refusing to unlink non-socket"):
        await server.start()


@pytest.mark.asyncio
async def test_stale_socket_cleanup_replaces_dangling_symlink(
    sock_path: Endpoint,
    tmp_path: Path,
) -> None:
    """A dangling symlink at the bind path is removed before binding."""
    if isinstance(sock_path, IpcEndpoint):
        pytest.skip("Symlink handling only applies to Unix endpoints")

    target = tmp_path / "missing.sock"
    sock_path.symlink_to(target)

    server = IpcServer(sock_path)
    await server.start()
    try:
        assert not sock_path.is_symlink()
        _reader, writer = await _connect(sock_path)
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_client_sends_valid_command(sock_path: Endpoint) -> None:
    """A valid command from a client appears on command_queue."""
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


@pytest.mark.asyncio
async def test_client_sends_invalid_command(sock_path: Endpoint) -> None:
    """Malformed JSON gets an error response and does not land on command_queue."""
    server = IpcServer(sock_path)
    await server.start()
    try:
        reader, writer = await _connect(sock_path)
        await asyncio.sleep(0.05)

        writer.write(b"not valid json\n")
        await writer.drain()

        line = await _read_line(reader)
        parsed = json.loads(line)
        assert parsed["type"] == "error"
        assert "error" in parsed

        assert server.command_queue.empty()

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_abrupt_disconnect_no_crash(sock_path: Endpoint) -> None:
    """Client closes without sending; server keeps running and accepts new clients."""
    server = IpcServer(sock_path)
    await server.start()
    try:
        _reader, writer = await _connect(sock_path)
        await asyncio.sleep(0.05)
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.1)

        # Server still accepting connections.
        _reader2, writer2 = await _connect(sock_path)
        writer2.close()
        await writer2.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_command_queue_exposed(sock_path: Endpoint) -> None:
    """The shared command_queue is an asyncio.Queue accessible via the property."""
    server = IpcServer(sock_path)
    await server.start()
    try:
        assert isinstance(server.command_queue, asyncio.Queue)
        assert server.command_queue.empty()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_get_state_returns_error_when_no_cached_state(sock_path: Endpoint) -> None:
    """get_state before any snapshot has been cached yields a structured error."""
    server = IpcServer(sock_path)
    await server.start()
    try:
        reader, writer = await _connect(sock_path)
        await asyncio.sleep(0.05)

        writer.write((json.dumps({"command": "get_state"}) + "\n").encode("utf-8"))
        await writer.drain()

        line = await _read_line(reader)
        parsed = json.loads(line)
        assert parsed == {"type": "error", "error": "no cached state available"}
        assert server.command_queue.empty()

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_get_state_returns_cached_snapshot(sock_path: Endpoint) -> None:
    """get_state replies with the cached state_update envelope and skips the queue."""
    server = IpcServer(sock_path)
    await server.start()
    try:
        cached_envelope = json.dumps(
            {
                "type": "state_update",
                "id": "envelope-1",
                "timestamp": "2026-05-18T00:00:00Z",
                "seq": 1,
                "payload": {"session_id": "s"},
            }
        )
        server.set_cached_state(cached_envelope + "\n")

        reader, writer = await _connect(sock_path)
        await asyncio.sleep(0.05)

        writer.write((json.dumps({"command": "get_state"}) + "\n").encode("utf-8"))
        await writer.drain()

        line = await _read_line(reader)
        parsed = json.loads(line)
        assert parsed["type"] == "state_update"
        assert parsed["payload"]["session_id"] == "s"
        assert parsed["id"] == "envelope-1"
        assert parsed["seq"] == 1
        assert server.command_queue.empty()

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_get_state_after_provider_state_update_returns_snapshot(
    sock_path: Endpoint,
) -> None:
    """End-to-end: provider.on_state_update -> server cache -> get_state reply."""
    from agentshore.ipc.provider import IpcStateProvider
    from agentshore.state import OrchestratorState, SessionState

    class _NullWriter:
        async def write_state(self, message: str) -> None:  # noqa: ARG002
            return None

        async def append_event(self, message: str) -> None:  # noqa: ARG002
            return None

    server = IpcServer(sock_path)
    await server.start()
    try:
        provider = IpcStateProvider(_NullWriter(), server=server)
        state = OrchestratorState(
            session_id="end2end",
            session_state=SessionState.RUNNING,
            total_plays=3,
            total_cost=0.5,
        )
        await provider.on_state_update(state)

        reader, writer = await _connect(sock_path)
        await asyncio.sleep(0.05)

        writer.write((json.dumps({"command": "get_state"}) + "\n").encode("utf-8"))
        await writer.drain()

        line = await _read_line(reader)
        parsed = json.loads(line)
        assert parsed["type"] == "state_update"
        assert parsed["payload"]["session_id"] == "end2end"
        assert server.command_queue.empty()

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_client_count_tracks_connections(sock_path: Endpoint) -> None:
    """client_count increments on connect and decrements on disconnect."""
    server = IpcServer(sock_path)
    await server.start()
    try:
        assert server.client_count == 0
        _r1, w1 = await _connect(sock_path)
        await asyncio.sleep(0.05)
        assert server.client_count == 1

        _r2, w2 = await _connect(sock_path)
        await asyncio.sleep(0.05)
        assert server.client_count == 2

        w1.close()
        await w1.wait_closed()
        await asyncio.sleep(0.1)
        assert server.client_count == 1

        w2.close()
        await w2.wait_closed()
        await asyncio.sleep(0.1)
        assert server.client_count == 0
    finally:
        await server.stop()
