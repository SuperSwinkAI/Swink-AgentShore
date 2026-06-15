from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
import yaml

from agentshore.agents.auth_probe import AUTH_EXPIRED, AUTH_OK, AuthProbeResult
from agentshore.sidecar.agents import (
    agents_catalog,
    configure_agent,
    detect_available_agents,
    list_agents,
)
from agentshore.sidecar.server import INVALID_PARAMS, ServerState, handle_request
from agentshore.state import AgentType


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

    configure_agent(tmp_path, "codex", {"identity": "Bot-User"})
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    # Canonicalised to case-folded login.
    assert data["agents"]["codex"]["identity"] == "bot-user"

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
# Per-tier max
# ---------------------------------------------------------------------------


def test_tier_models_from_raw_reads_max(tmp_path: Path) -> None:
    """_tier_models_from_raw surfaces the per-tier max field."""
    _write_config(
        tmp_path / "agentshore.yaml",
        {
            "agents": {
                "claude_code": {
                    "model_tiers": {
                        "medium": {"enabled": True, "model": "sonnet", "max": 5},
                    }
                }
            }
        },
    )
    rows = list_agents(tmp_path)
    assert len(rows) == 1
    tier = rows[0]["tier_models"].get("medium", {})
    assert tier.get("max") == 5


def test_tier_models_from_raw_clamps_max(tmp_path: Path) -> None:
    """max values outside 1–20 are silently clamped."""
    _write_config(
        tmp_path / "agentshore.yaml",
        {
            "agents": {
                "claude_code": {
                    "model_tiers": {
                        "small": {"enabled": True, "model": "haiku", "max": 99},
                        "large": {"enabled": True, "model": "opus", "max": 0},
                    }
                }
            }
        },
    )
    rows = list_agents(tmp_path)
    tiers = rows[0]["tier_models"]
    assert tiers.get("small", {}).get("max") == 20  # clamped from 99
    assert tiers.get("large", {}).get("max") == 1  # clamped from 0


def test_configure_agent_writes_max(tmp_path: Path) -> None:
    """configure_agent persists max for each tier."""
    _write_config(tmp_path / "agentshore.yaml", {"agents": {"codex": {"enabled": True}}})
    configure_agent(
        tmp_path,
        "codex",
        {"tier_models": {"medium": {"enabled": True, "model": "gpt-5.4", "max": 10}}},
    )
    rows = list_agents(tmp_path)
    tier = rows[0]["tier_models"].get("medium", {})
    assert tier.get("max") == 10


def test_validate_tier_models_rejects_bool_max(tmp_path: Path) -> None:
    """max must be an integer, not a boolean."""
    _write_config(tmp_path / "agentshore.yaml", {"agents": {"codex": {"enabled": True}}})
    with pytest.raises(ValueError, match="max must be an integer"):
        configure_agent(
            tmp_path,
            "codex",
            {"tier_models": {"medium": {"max": True}}},
        )


# ---------------------------------------------------------------------------
# agents.check_auth RPC (backend CLI-agent auth probe)
# ---------------------------------------------------------------------------


def _resolve(result: object) -> dict[str, object]:
    if inspect.isawaitable(result):
        return cast("dict[str, object]", asyncio.run(result))
    return cast("dict[str, object]", result)


def test_rpc_agents_check_auth_returns_rows(tmp_path: Path) -> None:
    """``agents.check_auth`` (no params) returns one row per configured CLI
    agent in the frontend shape, sourced from the shared probe."""
    _write_config(
        tmp_path / "agentshore.yaml", {"project": {}, "agents": {"codex": {"enabled": True}}}
    )
    state = ServerState(active_project_path=str(tmp_path))

    rows = [AuthProbeResult(AgentType.CODEX, AUTH_OK, "authenticated")]
    with patch(
        "agentshore.agents.auth_probe.probe_configured_cli_auth",
        return_value=rows,
    ):
        response = _resolve(
            handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "agents.check_auth"},
                state=state,
            )
        )

    assert "error" not in response
    result = cast("dict[str, object]", response["result"])
    agents = cast("list[dict[str, object]]", result["agents"])
    assert agents == [{"agent_type": "codex", "status": "ok", "detail": "authenticated"}]


def test_rpc_agents_check_auth_expired_row(tmp_path: Path) -> None:
    """An expired backend session surfaces as an ``expired`` row (the RPC never
    raises — it represents the failure as data the setup screen renders)."""
    _write_config(
        tmp_path / "agentshore.yaml", {"project": {}, "agents": {"codex": {"enabled": True}}}
    )
    state = ServerState(active_project_path=str(tmp_path))

    rows = [AuthProbeResult(AgentType.CODEX, AUTH_EXPIRED, "run `codex login`")]
    with patch(
        "agentshore.agents.auth_probe.probe_configured_cli_auth",
        return_value=rows,
    ):
        response = _resolve(
            handle_request(
                {"jsonrpc": "2.0", "id": 2, "method": "agents.check_auth"},
                state=state,
            )
        )

    result = cast("dict[str, object]", response["result"])
    agents = cast("list[dict[str, object]]", result["agents"])
    assert agents == [{"agent_type": "codex", "status": "expired", "detail": "run `codex login`"}]


def test_rpc_agents_check_auth_probe_one(tmp_path: Path) -> None:
    """With ``{"agent_type": "codex"}`` the RPC probes only that type."""
    _write_config(
        tmp_path / "agentshore.yaml", {"project": {}, "agents": {"codex": {"enabled": True}}}
    )
    state = ServerState(active_project_path=str(tmp_path))

    one = AuthProbeResult(AgentType.CODEX, AUTH_OK, "authenticated")
    with patch(
        "agentshore.agents.auth_probe.probe_cli_auth",
        return_value=one,
    ):
        response = _resolve(
            handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "agents.check_auth",
                    "params": {"agent_type": "codex"},
                },
                state=state,
            )
        )

    result = cast("dict[str, object]", response["result"])
    agents = cast("list[dict[str, object]]", result["agents"])
    assert len(agents) == 1
    assert agents[0]["agent_type"] == "codex"
    assert agents[0]["status"] == "ok"


def test_rpc_agents_check_auth_unknown_agent_type(tmp_path: Path) -> None:
    """An unknown ``agent_type`` yields an error-status row, not an exception."""
    _write_config(tmp_path / "agentshore.yaml", {"project": {}, "agents": {}})
    state = ServerState(active_project_path=str(tmp_path))

    response = _resolve(
        handle_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "agents.check_auth",
                "params": {"agent_type": "not_an_agent"},
            },
            state=state,
        )
    )
    result = cast("dict[str, object]", response["result"])
    agents = cast("list[dict[str, object]]", result["agents"])
    assert agents[0]["agent_type"] == "not_an_agent"
    assert agents[0]["status"] == "error"


def test_rpc_agents_check_auth_bad_config_returns_error_row(tmp_path: Path) -> None:
    """A config that cannot load surfaces as an error row, not a raised RPC."""
    (tmp_path / "agentshore.yaml").write_text("project: [not, a, mapping\n", encoding="utf-8")
    state = ServerState(active_project_path=str(tmp_path))

    response = _resolve(
        handle_request(
            {"jsonrpc": "2.0", "id": 5, "method": "agents.check_auth"},
            state=state,
        )
    )
    result = cast("dict[str, object]", response["result"])
    agents = cast("list[dict[str, object]]", result["agents"])
    assert agents[0]["status"] == "error"
