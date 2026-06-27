"""Tests for sidecar config.read and config.write JSON-RPC methods."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
import yaml

from agentshore.sidecar.config import read_config, write_config
from agentshore.sidecar.server import INVALID_PARAMS, ServerState, handle_request


def test_read_returns_raw_and_parsed(tmp_path: Path) -> None:
    content = "budget:\n  total: 20.0\n"
    (tmp_path / "agentshore.yaml").write_text(content)
    result = read_config(tmp_path)
    assert result["raw"] == content
    assert result["parsed"] == {"budget": {"total": 20.0}}


def test_read_missing_file_returns_empty(tmp_path: Path) -> None:
    result = read_config(tmp_path)
    assert result == {"raw": "", "parsed": {}}


def test_read_empty_file_returns_empty_parsed(tmp_path: Path) -> None:
    (tmp_path / "agentshore.yaml").write_text("")
    result = read_config(tmp_path)
    assert result["raw"] == ""
    assert result["parsed"] == {}


def test_write_creates_new_file(tmp_path: Path) -> None:
    write_config(tmp_path, {"budget": {"total": 15.0}})
    yaml_path = tmp_path / "agentshore.yaml"
    assert yaml_path.exists()
    loaded = yaml.safe_load(yaml_path.read_text())
    assert loaded == {"budget": {"total": 15.0}}


def test_write_config_accepts_dict_str_object_patch(tmp_path: Path) -> None:
    patch: dict[str, object] = {"budget": {"total": 7.0}}

    write_config(tmp_path, patch)

    loaded = yaml.safe_load((tmp_path / "agentshore.yaml").read_text())
    assert loaded == {"budget": {"total": 7.0}}


def test_write_deep_merges_existing(tmp_path: Path) -> None:
    initial = {
        "budget": {"total": 20.0, "enabled": True},
        "agents": {"codex": {"enabled": True}},
    }
    (tmp_path / "agentshore.yaml").write_text(yaml.safe_dump(initial, sort_keys=False))
    write_config(
        tmp_path,
        {"budget": {"total": 50.0}, "agents": {"claude": {"enabled": True}}},
    )
    loaded = yaml.safe_load((tmp_path / "agentshore.yaml").read_text())
    assert loaded["budget"]["total"] == 50.0
    assert loaded["budget"]["enabled"] is True
    assert loaded["agents"]["codex"]["enabled"] is True
    assert loaded["agents"]["claude"]["enabled"] is True


def test_write_null_removes_key(tmp_path: Path) -> None:
    initial = {"agents": {"codex": {"enabled": True, "identity": "old"}}}
    (tmp_path / "agentshore.yaml").write_text(yaml.safe_dump(initial, sort_keys=False))
    write_config(tmp_path, {"agents": {"codex": {"identity": None}}})
    loaded = yaml.safe_load((tmp_path / "agentshore.yaml").read_text())
    assert "identity" not in loaded["agents"]["codex"]
    assert loaded["agents"]["codex"]["enabled"] is True


def test_write_replaces_list_value(tmp_path: Path) -> None:
    initial = {"plays": ["a", "b", "c"]}
    (tmp_path / "agentshore.yaml").write_text(yaml.safe_dump(initial, sort_keys=False))
    write_config(tmp_path, {"plays": ["x", "y"]})
    loaded = yaml.safe_load((tmp_path / "agentshore.yaml").read_text())
    assert loaded["plays"] == ["x", "y"]


def test_write_leaves_no_temp_file(tmp_path: Path) -> None:
    write_config(tmp_path, {"budget": {"total": 10.0}})
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].name == "agentshore.yaml"


def test_write_invalid_patch_raises_type_error(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        write_config(tmp_path, "not a dict")  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        write_config(tmp_path, ["list", "is", "also", "bad"])  # type: ignore[arg-type]


def test_rpc_config_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    content = "budget:\n  total: 30.0\n"
    (tmp_path / "agentshore.yaml").write_text(content)
    response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "config.read"})
    assert response is not None
    assert "result" in response
    result = cast("dict[str, object]", response["result"])
    assert result["raw"] == content
    assert result["parsed"] == {"budget": {"total": 30.0}}


def test_rpc_config_write_invalid_params(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "config.write",
            "params": {"patch": "not a dict"},
        }
    )
    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == INVALID_PARAMS


def test_rpc_config_write_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    write_resp = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "config.write",
            "params": {"patch": {"budget": {"total": 99.0}}},
        }
    )
    assert write_resp is not None
    assert "result" in write_resp
    assert write_resp["result"] == {}

    read_resp = handle_request({"jsonrpc": "2.0", "id": 4, "method": "config.read"})
    assert read_resp is not None
    result = cast("dict[str, object]", read_resp["result"])
    parsed = cast("dict[str, object]", result["parsed"])
    budget = cast("dict[str, object]", parsed["budget"])
    assert budget["total"] == 99.0


def test_rpc_config_write_missing_patch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    response = handle_request({"jsonrpc": "2.0", "id": 5, "method": "config.write", "params": {}})
    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == INVALID_PARAMS


def test_rpc_config_write_no_params(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    response = handle_request({"jsonrpc": "2.0", "id": 6, "method": "config.write"})
    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == INVALID_PARAMS


def test_rpc_config_read_targets_active_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config.read repoints at the active project per DESIGN §1.3, not cwd."""
    cwd_dir = tmp_path / "cwd"
    project_dir = tmp_path / "project"
    cwd_dir.mkdir()
    project_dir.mkdir()
    (cwd_dir / "agentshore.yaml").write_text("budget:\n  total: 1.0\n")
    (project_dir / "agentshore.yaml").write_text("budget:\n  total: 42.0\n")
    monkeypatch.chdir(cwd_dir)

    state = ServerState(active_project_path=str(project_dir))
    response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "config.read"}, state=state)
    assert response is not None
    result = cast("dict[str, object]", response["result"])
    parsed = cast("dict[str, object]", result["parsed"])
    budget = cast("dict[str, object]", parsed["budget"])
    assert budget["total"] == 42.0


def test_rpc_config_write_targets_active_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config.write also writes into the active project's agentshore.yaml."""
    cwd_dir = tmp_path / "cwd"
    project_dir = tmp_path / "project"
    cwd_dir.mkdir()
    project_dir.mkdir()
    monkeypatch.chdir(cwd_dir)

    state = ServerState(active_project_path=str(project_dir))
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "config.write",
            "params": {"patch": {"budget": {"total": 99.0}}},
        },
        state=state,
    )
    assert response is not None
    assert "result" in response
    assert not (cwd_dir / "agentshore.yaml").exists()
    written = yaml.safe_load((project_dir / "agentshore.yaml").read_text())
    assert written == {"budget": {"total": 99.0}}
