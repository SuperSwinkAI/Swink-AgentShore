from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentshore.cli import runtime
from agentshore.config.models import BudgetConfig, PolicyMode
from agentshore.session_path import IpcEndpoint


class _Conn:
    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _FakeProc:
    def __init__(self, pid: int, *, exit_immediately: bool = False) -> None:
        self.pid = pid
        self._exit_immediately = exit_immediately

    def poll(self) -> int | None:
        return 1 if self._exit_immediately else None


def _launch_kwargs(tmp_path: Path) -> dict[str, Any]:
    return {
        "project_path": tmp_path,
        "ipc_endpoint": IpcEndpoint.tcp("127.0.0.1", 56789),
        "session_id": "session-1",
        "seed": None,
        "budget_cfg": BudgetConfig(enabled=True, total=20.0),
        "policy_mode": PolicyMode.LEARNING,
        "policy": None,
        "strict": None,
        "config_path": None,
        "timelapse_enabled": False,
    }


def test_launch_dashboard_waits_for_tcp_ipc_before_dashboard_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    procs = [_FakeProc(101), _FakeProc(202)]

    def fake_popen(cmd: list[str], **_kwargs: object) -> _FakeProc:
        proc = procs.pop(0)
        events.append(f"popen:{cmd[3]}:{proc.pid}")
        return proc

    attempts = 0

    def fake_create_connection(addr: tuple[str, int], timeout: float) -> _Conn:
        nonlocal attempts
        attempts += 1
        events.append(f"connect:{attempts}")
        assert addr == ("127.0.0.1", 56789)
        assert timeout == runtime._SOCKET_POLL_INTERVAL_S
        assert "dashboard-pid" not in events
        if attempts == 1:
            raise OSError("not ready")
        return _Conn()

    import socket
    import subprocess
    import time
    import webbrowser

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(time, "sleep", lambda _seconds: events.append("sleep"))
    monkeypatch.setattr(webbrowser, "open", lambda url: events.append(f"open:{url}"))
    monkeypatch.setattr("agentshore.session_path.session_dir", lambda _project: tmp_path / "s")
    monkeypatch.setattr("agentshore.session_path.find_dashboard_port", lambda: 9400)
    monkeypatch.setattr("agentshore.session_path.stop_dashboard_process", lambda _project: False)
    monkeypatch.setattr(
        "agentshore.session_path.write_dashboard_pid",
        lambda _project, pid: events.append(f"dashboard-pid:{pid}"),
    )

    runtime._launch_dashboard_background(**_launch_kwargs(tmp_path))

    assert events.index("connect:2") < events.index("popen:dashboard:202")
    # Supervisor must not write dashboard.pid: bridge records its own pid to dodge
    # the Windows uv-trampoline self-kill; browser open is the post-launch marker.
    assert "dashboard-pid:202" not in events
    assert events.index("popen:dashboard:202") < events.index("open:http://localhost:9400")
    assert "open:http://localhost:9400" in events


def test_launch_dashboard_aborts_if_orchestrator_exits_before_ipc_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    def fake_popen(cmd: list[str], **_kwargs: object) -> _FakeProc:
        events.append(f"popen:{cmd[3]}")
        return _FakeProc(101, exit_immediately=True)

    import socket
    import subprocess
    import webbrowser

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: events.append("connect"))
    monkeypatch.setattr(webbrowser, "open", lambda url: events.append(f"open:{url}"))
    monkeypatch.setattr("agentshore.session_path.session_dir", lambda _project: tmp_path / "s")
    monkeypatch.setattr("agentshore.session_path.stop_dashboard_process", lambda _project: False)
    monkeypatch.setattr(
        "agentshore.session_path.write_dashboard_pid",
        lambda _project, pid: events.append(f"dashboard-pid:{pid}"),
    )

    runtime._launch_dashboard_background(**_launch_kwargs(tmp_path))

    assert events == ["popen:start"]
