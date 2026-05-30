"""Tests for the in-sidecar dashboard bridge (DESIGN §1.2 / §2.3)."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from agentshore.session_path import IpcEndpoint
from agentshore.sidecar.embedded_bridge import EmbeddedBridge


@pytest.fixture()
def static_dir(tmp_path: Path) -> Path:
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<html><body>test</body></html>", encoding="utf-8")
    return d


def test_auto_selects_loopback_port(static_dir: Path) -> None:
    bridge = EmbeddedBridge(
        IpcEndpoint.tcp("127.0.0.1", 0),
        session_dir=static_dir.parent,
        static_dir=static_dir,
    )
    assert bridge.host == "127.0.0.1"
    assert bridge.port > 0
    assert bridge.is_running is False


def test_endpoint_payload_advertises_ws_url(static_dir: Path) -> None:
    bridge = EmbeddedBridge(
        IpcEndpoint.tcp("127.0.0.1", 0),
        session_dir=static_dir.parent,
        static_dir=static_dir,
    )
    payload = bridge.endpoint()
    assert payload["kind"] == "ws"
    assert payload["host"] == "127.0.0.1"
    assert payload["port"] == bridge.port
    assert payload["url"] == f"ws://127.0.0.1:{bridge.port}/ws"


def test_explicit_port_is_honoured(static_dir: Path) -> None:
    bridge = EmbeddedBridge(
        IpcEndpoint.tcp("127.0.0.1", 0),
        session_dir=static_dir.parent,
        port=12345,
        static_dir=static_dir,
    )
    assert bridge.port == 12345
    assert bridge.endpoint()["url"] == "ws://127.0.0.1:12345/ws"


@pytest.mark.asyncio
async def test_start_propagates_underlying_failure(tmp_path: Path) -> None:
    """A missing static_dir surfaces as a FileNotFoundError on start()."""
    missing = tmp_path / "absent"
    bridge = EmbeddedBridge(
        IpcEndpoint.tcp("127.0.0.1", 0),
        session_dir=tmp_path,
        static_dir=missing,
    )
    with pytest.raises(FileNotFoundError):
        await bridge.start()
    assert bridge.is_running is False


@pytest.mark.asyncio
async def test_start_signals_ready_then_stop_cancels(static_dir: Path) -> None:
    """``start`` returns once uvicorn is listening; ``stop`` tears it down."""
    bridge = EmbeddedBridge(
        IpcEndpoint.tcp("127.0.0.1", 0),
        session_dir=static_dir.parent,
        static_dir=static_dir,
    )
    try:
        await asyncio.wait_for(bridge.start(), timeout=10.0)
        assert bridge.is_running is True

        reader, writer = await asyncio.open_connection(bridge.host, bridge.port)
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()
        del reader
    finally:
        await bridge.stop()

    assert bridge.is_running is False
