"""Tests for the ``agentshore add-budget`` CLI subcommand."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from agentshore.cli import main
from agentshore.session_path import budget_from_state_line

_APPLIED: dict[str, object] = {
    "enabled": True,
    "total": 75.0,
    "spent": 10.0,
    "remaining": 65.0,
    "time_enabled": True,
    "time_total_minutes": 180.0,
    "time_elapsed_minutes": 30.0,
    "time_remaining_minutes": 150.0,
    "resumed": False,
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


# --------------------------------------------------------------------------- #
# budget_from_state_line — get_state envelope parsing (the readback that fed the
# CLI's resulting-caps echo; budget lives under payload, not top-level).
# --------------------------------------------------------------------------- #


def test_budget_from_state_line_reads_payload_nesting() -> None:
    import json

    env = json.dumps(
        {"type": "state_update", "payload": {"budget": {"enabled": True, "total": 45.0}}}
    ).encode()
    budget = budget_from_state_line(env)
    assert budget == {"enabled": True, "total": 45.0}


def test_budget_from_state_line_tolerates_flat_shape() -> None:
    import json

    env = json.dumps({"type": "state_update", "budget": {"enabled": False}}).encode()
    assert budget_from_state_line(env) == {"enabled": False}


@pytest.mark.parametrize(
    "line",
    [
        None,
        b"",
        b"   ",
        b"not json",
        b'{"type": "error", "error": "boom"}',
        b'{"type": "state_update", "payload": {}}',
    ],
)
def test_budget_from_state_line_returns_none_on_no_budget(line: bytes | None) -> None:
    assert budget_from_state_line(line) is None
