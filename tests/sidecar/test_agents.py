from __future__ import annotations

from pathlib import Path

import yaml

from agentshore.sidecar.agents import (
    agents_catalog,
    configure_agent,
    detect_available_agents,
    get_spawn_limits,
    list_agents,
    set_spawn_limits,
)
from agentshore.sidecar.server import INVALID_PARAMS, handle_request


def _write_config(path: Path, payload: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_list_agents_projects_canonical_shape(tmp_path: Path) -> None:
    _write_config(
        tmp_path / "agentshore.yaml",
        {
            "agents": {
                "claude_code": {
                    "enabled": True,
                    "identity": "ExAmple-User",
                    "model_tiers": {
                        "small": {"enabled": True, "model": "haiku"},
                        "medium": {"enabled": True, "model": "sonnet"},
                        "large": {"enabled": True, "model": "opus"},
                    },
                },
                "codex": {
                    "enabled": False,
                    "model_tiers": {
                        "small": {"enabled": True, "model": "gpt-5.4-mini"},
                    },
                },
            }
        },
    )

    rows = list_agents(tmp_path)

    assert [row["type"] for row in rows] == ["claude_code", "codex"]
    claude = rows[0]
    assert claude["enabled"] is True
    # GitHub logins are case-folded for safety comparisons.
    assert claude["identity"] == "example-user"
    assert set(claude["tier_models"].keys()) == {"small", "medium", "large"}
    assert claude["tier_models"]["large"]["model"] == "opus"

    codex = rows[1]
    assert codex["enabled"] is False
    assert codex["identity"] is None
    # Missing tiers are returned as empty dicts so the UI can render every row.
    assert codex["tier_models"]["medium"] == {}
    assert codex["tier_models"]["large"] == {}


def test_list_agents_returns_empty_when_block_missing(tmp_path: Path) -> None:
    _write_config(tmp_path / "agentshore.yaml", {"budget": {"enabled": True, "total": 20.0}})
    assert list_agents(tmp_path) == []


def test_list_agents_returns_empty_when_no_config(tmp_path: Path) -> None:
    assert list_agents(tmp_path) == []


def test_configure_agent_toggles_enabled(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_config(
        cfg,
        {"agents": {"claude_code": {"enabled": True, "identity": "example-user"}}},
    )

    configure_agent(tmp_path, "claude_code", {"enabled": False})

    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["agents"]["claude_code"]["enabled"] is False
    # Untouched fields persist.
    assert data["agents"]["claude_code"]["identity"] == "example-user"


def test_configure_agent_binds_and_clears_identity(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_config(cfg, {"agents": {"codex": {"enabled": True}}})

    configure_agent(tmp_path, "codex", {"identity": "UnseriousAI"})
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    # Canonicalised to case-folded login.
    assert data["agents"]["codex"]["identity"] == "unseriousai"

    configure_agent(tmp_path, "codex", {"identity": None})
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert "identity" not in data["agents"]["codex"]


def test_configure_agent_merges_tier_models(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_config(
        cfg,
        {
            "agents": {
                "claude_code": {
                    "enabled": True,
                    "model_tiers": {
                        "small": {"enabled": True, "model": "haiku"},
                        "medium": {"enabled": True, "model": "sonnet"},
                    },
                }
            }
        },
    )

    configure_agent(
        tmp_path,
        "claude_code",
        {
            "tier_models": {
                "medium": {"enabled": False},
                "large": {"enabled": True, "model": "opus"},
            }
        },
    )

    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    tiers = data["agents"]["claude_code"]["model_tiers"]
    # Existing tier merged, not replaced.
    assert tiers["small"] == {"enabled": True, "model": "haiku"}
    assert tiers["medium"]["enabled"] is False
    assert tiers["medium"]["model"] == "sonnet"
    assert tiers["large"] == {"enabled": True, "model": "opus"}


def test_configure_agent_creates_new_entry(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_config(cfg, {"budget": {"enabled": True, "total": 5.0}})

    configure_agent(tmp_path, "gemini", {"enabled": True, "identity": "example-user"})

    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["agents"]["gemini"] == {"enabled": True, "identity": "example-user"}
    # Pre-existing keys are preserved.
    assert data["budget"] == {"enabled": True, "total": 5.0}


def test_configure_agent_accepts_grok(tmp_path: Path) -> None:
    cfg = tmp_path / "agentshore.yaml"
    _write_config(cfg, {"agents": {}})

    configure_agent(tmp_path, "grok", {"enabled": True, "identity": "example-user"})

    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["agents"]["grok"] == {"enabled": True, "identity": "example-user"}


def test_agents_catalog_includes_grok_defaults() -> None:
    catalog = agents_catalog()

    models = catalog["models"]
    defaults = catalog["defaults"]
    assert isinstance(models, dict)
    assert isinstance(defaults, dict)
    assert "grok-build" in models["grok"]
    assert "grok-build-0.1" in models["grok"]
    assert "grok-4.3" in models["grok"]
    assert defaults["grok"]["small"] == {
        "model": "grok-build",
        "reasoning_effort": "low",
    }
    assert defaults["grok"]["medium"] == {
        "model": "grok-build",
        "reasoning_effort": "medium",
    }
    assert defaults["grok"]["large"] == {
        "model": "grok-build",
        "reasoning_effort": "high",
    }


def test_detect_available_agents_maps_grok_aliases(monkeypatch) -> None:
    monkeypatch.setattr(
        "agentshore.sidecar.agents.detect_agent_binaries",
        lambda: ("grok-build", "grok"),
    )

    assert detect_available_agents() == ["grok"]


def test_configure_agent_rejects_unknown_fields(tmp_path: Path) -> None:
    _write_config(tmp_path / "agentshore.yaml", {"agents": {"codex": {"enabled": True}}})

    try:
        configure_agent(tmp_path, "codex", {"binary": "/usr/bin/codex"})
    except ValueError as exc:
        assert "binary" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown patch field")


def test_configure_agent_rejects_invalid_tier(tmp_path: Path) -> None:
    _write_config(tmp_path / "agentshore.yaml", {"agents": {"codex": {"enabled": True}}})

    try:
        configure_agent(tmp_path, "codex", {"tier_models": {"giant": {"enabled": True}}})
    except ValueError as exc:
        assert "giant" in str(exc)
    else:
        raise AssertionError("expected ValueError for unsupported tier")


def test_rpc_agents_list_requires_no_params() -> None:
    response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "agents.list"})
    assert response is not None
    # Without a configured cwd this returns [] - the call itself must succeed.
    assert "error" not in response


def test_rpc_agents_configure_rejects_missing_type() -> None:
    response = handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "agents.configure", "params": {"enabled": True}}
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS


def test_rpc_agents_configure_rejects_non_object_params() -> None:
    response = handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "agents.configure", "params": []}
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS


def test_rpc_agents_configure_rejects_unknown_agent_type() -> None:
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "agents.configure",
            "params": {"type": "unknown_agent", "enabled": True},
        }
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS
    assert "unknown agent type" in response["error"]["message"]


# ---------------------------------------------------------------------------
# Spawn limits (desktop-ty04)
# ---------------------------------------------------------------------------


def test_get_spawn_limits_returns_default_when_unset(tmp_path: Path) -> None:
    """No agentshore.yaml -> defaults to max_per_config=2 (the desktop-ty04 default)."""
    result = get_spawn_limits(tmp_path)
    assert result == {"max_per_config": 2}


def test_get_spawn_limits_reads_persisted_value(tmp_path: Path) -> None:
    _write_config(tmp_path / "agentshore.yaml", {"agent_spawn": {"max_per_config": 4}})
    result = get_spawn_limits(tmp_path)
    assert result == {"max_per_config": 4}


def test_set_spawn_limits_writes_to_yaml(tmp_path: Path) -> None:
    """set_spawn_limits round-trips through get_spawn_limits."""
    _write_config(tmp_path / "agentshore.yaml", {"agents": {}})
    set_spawn_limits(tmp_path, {"max_per_config": 5})
    assert get_spawn_limits(tmp_path) == {"max_per_config": 5}


def test_set_spawn_limits_rejects_unsupported_field(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError, match="unsupported agent_spawn fields"):
        set_spawn_limits(tmp_path, {"max_total": 10})


def test_set_spawn_limits_rejects_non_integer(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError, match="must be an integer"):
        set_spawn_limits(tmp_path, {"max_per_config": "two"})  # type: ignore[dict-item]


def test_set_spawn_limits_rejects_out_of_range(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError, match="between 1 and 32"):
        set_spawn_limits(tmp_path, {"max_per_config": 99})
    with pytest.raises(ValueError, match="between 1 and 32"):
        set_spawn_limits(tmp_path, {"max_per_config": 0})


def test_rpc_agents_get_spawn_limits_returns_default() -> None:
    response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "agents.get_spawn_limits"})
    assert response is not None
    # Without a configured project, defaults - the call itself must succeed.
    assert "error" not in response
    assert response["result"]["max_per_config"] == 2


def test_rpc_agents_set_spawn_limits_rejects_non_object() -> None:
    response = handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "agents.set_spawn_limits", "params": []}
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS


def test_rpc_agents_set_spawn_limits_rejects_invalid_value() -> None:
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "agents.set_spawn_limits",
            "params": {"max_per_config": 99},
        }
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS
    assert "between 1 and 32" in response["error"]["message"]
