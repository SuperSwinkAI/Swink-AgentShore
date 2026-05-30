"""Integration test for the real EmbeddedBridge start during session.start.

Covers the ``start_bridge`` and ``first_snapshot`` phases of
desktop-0vc.11.2 (gh-307): a valid project setup ends with a running
WebSocket bridge that the desktop WebView can connect to.

Heavy by design (uvicorn binds a port, waits for ready); kept in its
own module so test_handshake.py stays fast.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from agentshore.sidecar.server import ServerState
from agentshore.sidecar.session_lifecycle import run_session_start


@pytest.mark.asyncio
async def test_real_bridge_starts_for_valid_project(tmp_path: Path) -> None:
    """With agentshore.yaml + .beads/ + a writable session_dir, session.start
    boots an EmbeddedBridge that binds its WebSocket port."""
    project_path = tmp_path / "valid"
    project_path.mkdir()
    (project_path / "agentshore.yaml").write_text("project: {}\n", encoding="utf-8")
    (project_path / ".beads").mkdir()
    state = ServerState(active_project_path=str(project_path))

    outcome = await run_session_start(state, start_bridge=True)
    try:
        assert state.bridge is not None
        assert state.bridge.is_running
        # WebSocket bridge is listening on its declared port.
        ws_endpoint = state.bridge.endpoint()
        port = ws_endpoint["port"]
        assert isinstance(port, int) and port > 0
        # The bridge IS the WebSocket server — connect to its port to
        # prove it bound the loopback. We don't expect to round-trip a
        # protocol upgrade here; the TCP connect is sufficient evidence.
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            pass
        # outcome.ipc_endpoint is the orchestrator-side IPC channel
        # (separate from the WebSocket port the bridge serves).
        assert outcome.ipc_endpoint["kind"] == "tcp"
    finally:
        if state.bridge is not None:
            await state.bridge.stop()
            state.bridge = None
