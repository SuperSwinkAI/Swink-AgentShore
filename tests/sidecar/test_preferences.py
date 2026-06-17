"""Tests for the ``preferences.{get,set}`` sidecar RPCs."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentshore import preferences as gp
from agentshore.sidecar.server import ServerState, handle_request


@pytest.fixture
def global_prefs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "preferences.yaml"
    monkeypatch.setattr(gp, "GLOBAL_PREFERENCES_PATH", path)
    return path


def _drive(method: str, params: object | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    response = handle_request(payload, state=ServerState())
    if asyncio.iscoroutine(response):
        response = asyncio.run(response)
    assert isinstance(response, dict)
    return response


def test_get_returns_empty_with_menu(global_prefs: Path) -> None:
    result = _drive("preferences.get")["result"]
    assert result == {
        "disabled_plays": [],
        "disableable_plays": ["cleanup", "design_audit", "groom_backlog", "prune", "run_qa"],
    }


def test_set_persists_and_echoes(global_prefs: Path) -> None:
    result = _drive("preferences.set", {"disabled_plays": ["run_qa", "cleanup"]})["result"]
    assert result["disabled_plays"] == ["cleanup", "run_qa"]
    # Persisted: a fresh get sees it.
    assert _drive("preferences.get")["result"]["disabled_plays"] == ["cleanup", "run_qa"]


def test_set_rejects_non_allowlisted_play(global_prefs: Path) -> None:
    response = _drive("preferences.set", {"disabled_plays": ["issue_pickup"]})
    assert response["error"]["code"] == -32602
    assert "issue_pickup" in response["error"]["message"]


def test_set_rejects_non_array(global_prefs: Path) -> None:
    response = _drive("preferences.set", {"disabled_plays": "run_qa"})
    assert response["error"]["code"] == -32602


def test_set_with_live_session_triggers_reload(
    global_prefs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reloaded = asyncio.Event()

    class _Orch:
        async def reload_config(self) -> None:
            reloaded.set()

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "preferences.set",
        "params": {"disabled_plays": ["prune"]},
    }
    state = ServerState()
    state.orchestrator = _Orch()  # type: ignore[assignment]

    async def _run() -> dict[str, object]:
        result = handle_request(payload, state=state)
        assert asyncio.iscoroutine(result)
        return await result  # type: ignore[no-any-return]

    response = asyncio.run(_run())
    assert response["result"]["disabled_plays"] == ["prune"]
    assert reloaded.is_set()
