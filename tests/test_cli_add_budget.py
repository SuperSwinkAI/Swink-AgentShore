"""Tests for the ``agentshore add-budget`` CLI subcommand."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from agentshore.cli import main

# Wire encoding from the IPC reply; keys mirror DrainController._applied_from_state
# and _serialize_budget (both use ``total`` for the dollar cap).
_APPLIED: dict[str, object] = {
    "enabled": True,
    "total": 75.0,
    "spent": 10.0,
    "remaining": 65.0,
    "time_enabled": True,
    "time_total_minutes": 180.0,
    "time_elapsed_minutes": 30.0,
    "time_remaining_minutes": 150.0,
}


@pytest.fixture()
def project_dir(tmp_path: Any) -> str:
    # The CLI uses click.Path(exists=True, file_okay=False); tmp_path is a dir.
    return str(tmp_path)


def test_add_budget_dollars_happy_path(monkeypatch: pytest.MonkeyPatch, project_dir: str) -> None:
    captured: dict[str, object] = {}

    def fake_request(project_path: Any, *, delta_usd: Any, delta_minutes: Any) -> dict[str, object]:
        captured["delta_usd"] = delta_usd
        captured["delta_minutes"] = delta_minutes
        return _APPLIED

    monkeypatch.setattr("agentshore.session_path.is_session_running", lambda p: True)
    monkeypatch.setattr("agentshore.session_path.request_add_budget", fake_request)

    result = CliRunner().invoke(main, ["add-budget", "--project", project_dir, "--budget", "25"])

    assert result.exit_code == 0, result.output
    assert captured["delta_usd"] == 25.0
    assert captured["delta_minutes"] is None
    assert "+$25.00" in result.output
    assert "Dollar cap: $75.00" in result.output


def test_add_budget_time_happy_path(monkeypatch: pytest.MonkeyPatch, project_dir: str) -> None:
    captured: dict[str, object] = {}

    def fake_request(project_path: Any, *, delta_usd: Any, delta_minutes: Any) -> dict[str, object]:
        captured["delta_usd"] = delta_usd
        captured["delta_minutes"] = delta_minutes
        return _APPLIED

    monkeypatch.setattr("agentshore.session_path.is_session_running", lambda p: True)
    monkeypatch.setattr("agentshore.session_path.request_add_budget", fake_request)

    result = CliRunner().invoke(main, ["add-budget", "--project", project_dir, "--time", "30m"])

    assert result.exit_code == 0, result.output
    assert captured["delta_usd"] is None
    assert captured["delta_minutes"] == 30
    assert "+30m" in result.output
    assert "Time cap: 180 min" in result.output


def test_add_budget_both(monkeypatch: pytest.MonkeyPatch, project_dir: str) -> None:
    captured: dict[str, object] = {}

    def fake_request(project_path: Any, *, delta_usd: Any, delta_minutes: Any) -> dict[str, object]:
        captured["delta_usd"] = delta_usd
        captured["delta_minutes"] = delta_minutes
        return _APPLIED

    monkeypatch.setattr("agentshore.session_path.is_session_running", lambda p: True)
    monkeypatch.setattr("agentshore.session_path.request_add_budget", fake_request)

    result = CliRunner().invoke(
        main,
        ["add-budget", "--project", project_dir, "--budget", "10", "--time", "2h"],
    )

    assert result.exit_code == 0, result.output
    assert captured["delta_usd"] == 10.0
    assert captured["delta_minutes"] == 120


def test_add_budget_no_args_errors(project_dir: str) -> None:
    result = CliRunner().invoke(main, ["add-budget", "--project", project_dir])
    assert result.exit_code != 0
    assert "at least one of --budget or --time" in result.output


def test_add_budget_no_running_session(monkeypatch: pytest.MonkeyPatch, project_dir: str) -> None:
    monkeypatch.setattr("agentshore.session_path.is_session_running", lambda p: False)

    result = CliRunner().invoke(main, ["add-budget", "--project", project_dir, "--budget", "25"])

    assert result.exit_code == 0
    assert "No running AgentShore session" in result.output


def test_add_budget_no_session_sentinel(monkeypatch: pytest.MonkeyPatch, project_dir: str) -> None:
    monkeypatch.setattr("agentshore.session_path.is_session_running", lambda p: True)
    monkeypatch.setattr(
        "agentshore.session_path.request_add_budget",
        lambda p, **k: "no_session",
    )

    result = CliRunner().invoke(main, ["add-budget", "--project", project_dir, "--budget", "25"])

    assert result.exit_code == 0
    assert "No running AgentShore session" in result.output


def test_add_budget_ipc_error_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, project_dir: str
) -> None:
    monkeypatch.setattr("agentshore.session_path.is_session_running", lambda p: True)
    monkeypatch.setattr(
        "agentshore.session_path.request_add_budget",
        lambda p, **k: "error",
    )

    result = CliRunner().invoke(main, ["add-budget", "--project", project_dir, "--budget", "25"])

    assert result.exit_code == 1
    assert "Failed to add budget" in result.output


def test_add_budget_rejection_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, project_dir: str
) -> None:
    """Orchestrator rejection (rejected:<msg>) exits non-zero and surfaces the message."""
    monkeypatch.setattr("agentshore.session_path.is_session_running", lambda p: True)
    monkeypatch.setattr(
        "agentshore.session_path.request_add_budget",
        lambda p, **k: "rejected:resulting dollar cap $0.01 is below the $1.00 minimum",
    )

    result = CliRunner().invoke(main, ["add-budget", "--project", project_dir, "--budget", "25"])

    assert result.exit_code == 1
    assert "rejected" in result.output.lower()


def test_add_budget_rejects_non_positive_dollars(project_dir: str) -> None:
    result = CliRunner().invoke(main, ["add-budget", "--project", project_dir, "--budget", "0"])
    assert result.exit_code != 0
    assert "--budget" in result.output


def test_add_budget_rejects_bad_time(project_dir: str) -> None:
    result = CliRunner().invoke(main, ["add-budget", "--project", project_dir, "--time", "soon"])
    assert result.exit_code != 0
    assert "--time" in result.output


def test_add_budget_command_name_is_hyphenated() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "add-budget" in result.output


# request_add_budget — reads an ``add_budget_ok`` reply directly, no polling.


def test_request_add_budget_returns_no_session_when_no_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Returns ``"no_session"`` when IPC endpoint is not discoverable."""
    from agentshore.session_path import request_add_budget

    monkeypatch.setattr("agentshore.session_path.discover_ipc_endpoint", lambda p: None)
    result = request_add_budget(tmp_path, delta_usd=10.0, delta_minutes=None)
    assert result == "no_session"


def test_request_add_budget_parses_ok_reply(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Returns the applied-caps dict from an ``add_budget_ok`` reply."""
    import json
    import socket
    import threading

    from agentshore.session_path import IpcEndpoint, request_add_budget

    reply = json.dumps({"type": "add_budget_ok", "enabled": True, "total": 60.0}) + "\n"
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    host, port = server_sock.getsockname()[:2]

    def _serve() -> None:
        conn, _ = server_sock.accept()
        conn.recv(4096)  # consume the command
        conn.sendall(reply.encode())
        conn.close()
        server_sock.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    endpoint = IpcEndpoint.tcp(host, port)
    monkeypatch.setattr("agentshore.session_path.discover_ipc_endpoint", lambda p: endpoint)
    result = request_add_budget(tmp_path, delta_usd=10.0, delta_minutes=None)
    t.join(timeout=5)

    assert isinstance(result, dict)
    assert result.get("total") == 60.0
    assert result.get("enabled") is True


def test_request_add_budget_returns_rejected_on_error_reply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Returns ``"rejected:<msg>"`` for an error reply from the server."""
    import json
    import socket
    import threading

    from agentshore.session_path import IpcEndpoint, request_add_budget

    reply = json.dumps({"type": "error", "error": "cap too low"}) + "\n"
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    host, port = server_sock.getsockname()[:2]

    def _serve() -> None:
        conn, _ = server_sock.accept()
        conn.recv(4096)
        conn.sendall(reply.encode())
        conn.close()
        server_sock.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    endpoint = IpcEndpoint.tcp(host, port)
    monkeypatch.setattr("agentshore.session_path.discover_ipc_endpoint", lambda p: endpoint)
    result = request_add_budget(tmp_path, delta_usd=10.0, delta_minutes=None)
    t.join(timeout=5)

    assert isinstance(result, str)
    assert result.startswith("rejected:")
    assert "cap too low" in result
