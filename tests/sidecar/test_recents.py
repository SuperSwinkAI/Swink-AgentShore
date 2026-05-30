"""Tests for the sidecar ``recents.*`` RPC methods (DESIGN §4.2 and §5.1).

The recents store persists AgentShore-domain project metadata at
``platformdirs.recents.json/recents.json`` so future frontends
can reuse it.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from agentshore.sidecar.handshake import capabilities
from agentshore.sidecar.recents import (
    list_recents,
    recents_path,
    remove_recent,
    touch_recent,
)
from agentshore.sidecar.server import (
    INVALID_PARAMS,
    handle_request,
    serve,
)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / "recents.json"


def test_recents_path_lives_under_user_data_dir() -> None:
    path = recents_path()
    # DESIGN §4.2 names the agentshore app explicitly.
    assert "agentshore" in path.parts
    assert path.name == "recents.json"


def test_list_recents_returns_empty_when_store_missing(store: Path) -> None:
    assert list_recents(store) == []


def test_touch_recent_creates_entry(store: Path, tmp_path: Path) -> None:
    project = tmp_path / "demo-project"
    project.mkdir()
    touch_recent(str(project), store)
    entries = list_recents(store)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["path"] == str(project)
    assert entry["label"] == "demo-project"
    assert entry["last_exit_reason"] is None
    assert isinstance(entry["last_started"], str)
    # Loose ISO-8601 sanity check.
    assert "T" in entry["last_started"]


def test_touch_recent_updates_existing_entry_and_does_not_duplicate(
    store: Path, tmp_path: Path
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    touch_recent(str(project), store)
    first = list_recents(store)[0]["last_started"]
    # Touch again — timestamp moves forward, label stays, only one entry.
    touch_recent(str(project), store)
    entries = list_recents(store)
    assert len(entries) == 1
    assert entries[0]["last_started"] >= first


def test_touch_recent_preserves_existing_last_exit_reason(store: Path, tmp_path: Path) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    # Hand-author a store with an existing exit reason; touch must not erase it.
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "path": str(project),
                        "label": "demo",
                        "last_started": "2026-05-01T00:00:00+00:00",
                        "last_exit_reason": "user_quit",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    touch_recent(str(project), store)
    entry = list_recents(store)[0]
    assert entry["last_exit_reason"] == "user_quit"


def test_list_recents_orders_by_last_started_desc(store: Path, tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    touch_recent(str(a), store)
    touch_recent(str(b), store)
    # Force a as older by rewriting its timestamp.
    raw = json.loads(store.read_text(encoding="utf-8"))
    for entry in raw["entries"]:
        if entry["path"] == str(a):
            entry["last_started"] = "2020-01-01T00:00:00+00:00"
    store.write_text(json.dumps(raw), encoding="utf-8")
    [first, second] = list_recents(store)
    assert first["path"] == str(b)
    assert second["path"] == str(a)


def test_remove_recent_drops_entry(store: Path, tmp_path: Path) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    touch_recent(str(project), store)
    remove_recent(str(project), store)
    assert list_recents(store) == []


def test_remove_recent_is_idempotent_for_unknown_path(store: Path, tmp_path: Path) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    touch_recent(str(project), store)
    remove_recent(str(tmp_path / "ghost"), store)
    # The known entry is untouched.
    entries = list_recents(store)
    assert len(entries) == 1
    assert entries[0]["path"] == str(project)


def test_list_recents_marks_valid_config(store: Path, tmp_path: Path) -> None:
    """Each entry carries ``has_valid_config`` based on the project's agentshore.yaml on disk."""
    valid = tmp_path / "valid"
    missing = tmp_path / "missing"
    malformed = tmp_path / "malformed"
    no_project = tmp_path / "no_project"
    for d in (valid, missing, malformed, no_project):
        d.mkdir()
    (valid / "agentshore.yaml").write_text("project:\n  name: demo\n", encoding="utf-8")
    (malformed / "agentshore.yaml").write_text(": not valid yaml :\n  - [", encoding="utf-8")
    (no_project / "agentshore.yaml").write_text("agents: []\n", encoding="utf-8")

    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "path": str(valid),
                        "label": "valid",
                        "last_started": "2026-05-04T00:00:00+00:00",
                        "last_exit_reason": None,
                    },
                    {
                        "path": str(missing),
                        "label": "missing",
                        "last_started": "2026-05-03T00:00:00+00:00",
                        "last_exit_reason": None,
                    },
                    {
                        "path": str(malformed),
                        "label": "malformed",
                        "last_started": "2026-05-02T00:00:00+00:00",
                        "last_exit_reason": None,
                    },
                    {
                        "path": str(no_project),
                        "label": "no_project",
                        "last_started": "2026-05-01T00:00:00+00:00",
                        "last_exit_reason": None,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    by_path = {entry["path"]: entry for entry in list_recents(store)}
    assert by_path[str(valid)]["has_valid_config"] is True
    assert by_path[str(missing)]["has_valid_config"] is False
    assert by_path[str(malformed)]["has_valid_config"] is False
    assert by_path[str(no_project)]["has_valid_config"] is False


def test_touch_recent_sets_has_valid_config(store: Path, tmp_path: Path) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    touch_recent(str(project), store)
    assert list_recents(store)[0]["has_valid_config"] is False
    (project / "agentshore.yaml").write_text("project:\n  name: demo\n", encoding="utf-8")
    # Recomputed on every read, no second touch needed.
    assert list_recents(store)[0]["has_valid_config"] is True


def test_list_recents_tolerates_corrupt_store(store: Path) -> None:
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("not json", encoding="utf-8")
    # A corrupt file must not crash the sidecar — treat as empty.
    assert list_recents(store) == []


def test_handshake_advertises_recents_methods() -> None:
    advertised = capabilities()
    assert "recents.list" in advertised
    assert "recents.touch" in advertised
    assert "recents.remove" in advertised


def test_rpc_recents_list_returns_array(monkeypatch: pytest.MonkeyPatch, store: Path) -> None:
    monkeypatch.setattr("agentshore.sidecar.server.recents_path", lambda: store)
    response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "recents.list"})
    assert response is not None
    assert response.get("result") == []


def test_rpc_recents_touch_then_list_round_trips(
    monkeypatch: pytest.MonkeyPatch, store: Path, tmp_path: Path
) -> None:
    monkeypatch.setattr("agentshore.sidecar.server.recents_path", lambda: store)
    project = tmp_path / "demo"
    project.mkdir()
    touch_response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": "t",
            "method": "recents.touch",
            "params": {"path": str(project)},
        }
    )
    assert touch_response is not None
    assert "error" not in touch_response

    list_response = handle_request({"jsonrpc": "2.0", "id": 2, "method": "recents.list"})
    assert list_response is not None
    result = list_response["result"]
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["path"] == str(project)
    assert result[0]["label"] == "demo"


def test_rpc_recents_touch_accepts_positional_params(
    monkeypatch: pytest.MonkeyPatch, store: Path, tmp_path: Path
) -> None:
    monkeypatch.setattr("agentshore.sidecar.server.recents_path", lambda: store)
    project = tmp_path / "demo"
    project.mkdir()
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "recents.touch",
            "params": [str(project)],
        }
    )
    assert response is not None
    assert "error" not in response
    assert list_recents(store)[0]["path"] == str(project)


def test_rpc_recents_touch_missing_path_returns_invalid_params(
    monkeypatch: pytest.MonkeyPatch, store: Path
) -> None:
    monkeypatch.setattr("agentshore.sidecar.server.recents_path", lambda: store)
    response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "recents.touch", "params": {}})
    assert response is not None
    error = response.get("error")
    assert error is not None
    assert error["code"] == INVALID_PARAMS


def test_rpc_recents_remove_drops_entry(
    monkeypatch: pytest.MonkeyPatch, store: Path, tmp_path: Path
) -> None:
    monkeypatch.setattr("agentshore.sidecar.server.recents_path", lambda: store)
    project = tmp_path / "demo"
    project.mkdir()
    touch_recent(str(project), store)
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "recents.remove",
            "params": {"path": str(project)},
        }
    )
    assert response is not None
    assert "error" not in response
    assert list_recents(store) == []


def test_rpc_recents_methods_serve_over_stdio(
    monkeypatch: pytest.MonkeyPatch, store: Path, tmp_path: Path
) -> None:
    monkeypatch.setattr("agentshore.sidecar.server.recents_path", lambda: store)
    project = tmp_path / "demo"
    project.mkdir()
    payloads = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "recents.touch",
            "params": {"path": str(project)},
        },
        {"jsonrpc": "2.0", "id": 2, "method": "recents.list"},
    ]
    stdin = io.StringIO("\n".join(json.dumps(p) for p in payloads) + "\n")
    stdout = io.StringIO()
    serve(stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[0].get("error") is None
    listed = replies[1]["result"]
    assert listed[0]["path"] == str(project)
