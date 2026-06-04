"""Tests for the sidecar timelapse lifecycle helpers.

Covers ``_dashboard_url`` and ``stop_timelapse_capture`` (the session-end hook):
the capture is stopped + the render path awaited, failures are swallowed so
they never wedge shutdown, and the rendered MP4 path is returned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentshore import timelapse
from agentshore.sidecar import session_lifecycle as sl
from agentshore.sidecar.server import ServerState


def test_dashboard_url_from_tcp_endpoint() -> None:
    url = sl._dashboard_url({"kind": "tcp", "host": "127.0.0.1", "port": 9473})
    assert url == "http://127.0.0.1:9473/"


def test_dashboard_url_none_when_missing_fields() -> None:
    assert sl._dashboard_url(None) is None
    assert sl._dashboard_url({"kind": "tcp"}) is None
    assert sl._dashboard_url({"host": "h", "port": "nope"}) is None


async def test_stop_timelapse_capture_noop_without_run_id() -> None:
    state = ServerState()
    assert await sl.stop_timelapse_capture(state) is None


async def test_stop_timelapse_capture_stops_and_returns_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = ServerState()
    state.timelapse_run_id = "swift-otter-042"
    state.timelapse_runs_cwd = tmp_path
    stopped: dict[str, object] = {}

    async def fake_stop(run_id: str, cwd: Path) -> None:
        stopped["run_id"] = run_id

    async def fake_await(run_id: str, cwd: Path) -> str:
        return "/runs/x/output.mp4"

    monkeypatch.setattr(timelapse, "stop_capture", fake_stop)
    monkeypatch.setattr(timelapse, "await_output", fake_await)

    out = await sl.stop_timelapse_capture(state)

    assert out == "/runs/x/output.mp4"
    assert stopped["run_id"] == "swift-otter-042"
    # Run-id is cleared so a second stop is a no-op.
    assert state.timelapse_run_id is None


async def test_stop_timelapse_capture_swallows_stop_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = ServerState()
    state.timelapse_run_id = "swift-otter-042"
    state.timelapse_runs_cwd = tmp_path

    async def fake_stop(run_id: str, cwd: Path) -> None:
        raise timelapse.TimelapseError("stop failed")

    monkeypatch.setattr(timelapse, "stop_capture", fake_stop)

    # Must not raise — best-effort shutdown.
    assert await sl.stop_timelapse_capture(state) is None
    assert state.timelapse_run_id is None
