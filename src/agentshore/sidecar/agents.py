"""Agents RPC helpers for the desktop sidecar (DESIGN §5.1, §10.1).

Implements ``agents.list`` and ``agents.configure`` against ``agentshore.yaml``.
Screen 5 enables/disables agent runners and binds each runner to an identity
from the configured identities pool; Screen 6 (tier configuration) writes
through the same ``agents.configure`` entry point.
"""

from __future__ import annotations

import os
import tempfile
from typing import TYPE_CHECKING, TypedDict

import yaml

from agentshore.agents.model_catalog import KNOWN_MODELS
from agentshore.agents.model_tiers import DEFAULT_MODEL_TIERS
from agentshore.environment import detect_agent_binaries
from agentshore.identity_names import canonical_identity_name
from agentshore.state import AgentType

if TYPE_CHECKING:
    from pathlib import Path


class TierModel(TypedDict, total=False):
    enabled: bool
    model: str
    reasoning_effort: str


class AgentRow(TypedDict):
    type: str
    enabled: bool
    identity: str | None
    tier_models: dict[str, TierModel]


_TIER_KEYS = ("small", "medium", "large")


def agents_catalog() -> dict[str, object]:
    """Return the canonical catalog the desktop Agent Config screen needs.

    Pure data: a per-agent-key list of known model IDs (matching the CLI
    wizard's source — agentshore.agents.model_catalog.KNOWN_MODELS) plus the
    per-tier recommended defaults (agentshore.agents.model_tiers.DEFAULT_MODEL_TIERS).
    No I/O — the desktop calls this once on mount and renders dropdowns from
    the result. Keeping the catalog in one place means the CLI wizard and
    the desktop screen always offer the same models with the same defaults.
    """
    models: dict[str, list[str]] = {key: list(items) for key, items in KNOWN_MODELS.items()}
    defaults: dict[str, dict[str, dict[str, str | None]]] = {}
    for agent_type in AgentType:
        tier_map = DEFAULT_MODEL_TIERS.get(agent_type, {})
        defaults[agent_type.value] = {
            tier: {
                "model": cfg.model,
                "reasoning_effort": cfg.reasoning_effort,
            }
            for tier, cfg in tier_map.items()
        }
    return {"models": models, "defaults": defaults}


_BINARY_TO_AGENT_TYPE: dict[str, str] = {
    "claude": "claude_code",
    "codex": "codex",
    "gemini": "gemini",
    "grok": "grok",
    "grok-build": "grok",
}


def detect_available_agents() -> list[str]:
    """Return agent type keys for CLI binaries found on PATH."""
    detected = set(detect_agent_binaries())
    available: list[str] = []
    for name in _BINARY_TO_AGENT_TYPE:
        if name not in detected:
            continue
        agent_type = _BINARY_TO_AGENT_TYPE.get(name)
        if agent_type is not None and agent_type not in available:
            available.append(agent_type)
    return available


_PATCHABLE_FIELDS = frozenset({"enabled", "identity", "tier_models"})
_ALLOWED_AGENT_TYPES = frozenset(agent.value for agent in AgentType)


def _config_path(project_path: Path) -> Path:
    return project_path / "agentshore.yaml"


def _load_yaml(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError("agentshore.yaml must be a mapping")
    return loaded


def _write_yaml_atomic(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".agentshore_", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(data, handle, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def _tier_models_from_raw(raw: object) -> dict[str, TierModel]:
    """Project the ``model_tiers`` block onto Screen 5's flattened shape.

    Returns a mapping with exactly the three canonical tier keys so the UI can
    render every tier even when the saved config omits one.
    """
    tiers: dict[str, TierModel] = {key: {} for key in _TIER_KEYS}
    if not isinstance(raw, dict):
        return tiers
    for tier_key in _TIER_KEYS:
        body = raw.get(tier_key)
        if not isinstance(body, dict):
            continue
        row: TierModel = {}
        if isinstance(body.get("enabled"), bool):
            row["enabled"] = bool(body["enabled"])
        model = body.get("model")
        if isinstance(model, str):
            row["model"] = model
        effort = body.get("reasoning_effort")
        if isinstance(effort, str):
            row["reasoning_effort"] = effort
        tiers[tier_key] = row
    return tiers


def list_agents(project_path: Path) -> list[AgentRow]:
    """Return the agent rows Screen 5 renders.

    Each row reports the agent type key, the enable flag, the bound identity
    login (or ``None`` if the agent has no ``identity:`` entry), and a
    flattened ``tier_models`` view derived from ``model_tiers``.
    """
    cfg_path = _config_path(project_path)
    if not cfg_path.exists():
        return []
    data = _load_yaml(cfg_path)
    agents_raw = data.get("agents")
    if not isinstance(agents_raw, dict):
        return []
    rows: list[AgentRow] = []
    for type_raw, body_raw in sorted(agents_raw.items()):
        if not isinstance(body_raw, dict):
            continue
        enabled = body_raw.get("enabled")
        identity_raw = body_raw.get("identity")
        identity = canonical_identity_name(identity_raw) if isinstance(identity_raw, str) else None
        rows.append(
            {
                "type": str(type_raw),
                "enabled": bool(enabled) if isinstance(enabled, bool) else False,
                "identity": identity,
                "tier_models": _tier_models_from_raw(body_raw.get("model_tiers")),
            }
        )
    return rows


def _validate_tier_models(value: object) -> dict[str, dict[str, object]]:
    if not isinstance(value, dict):
        raise ValueError("tier_models must be a mapping")
    out: dict[str, dict[str, object]] = {}
    for tier_key, body in value.items():
        if tier_key not in _TIER_KEYS:
            raise ValueError(f"unsupported tier: {tier_key}")
        if not isinstance(body, dict):
            raise ValueError(f"tier_models.{tier_key} must be a mapping")
        clean: dict[str, object] = {}
        for field in ("enabled", "model", "reasoning_effort"):
            if field not in body:
                continue
            field_value = body[field]
            if field == "enabled" and not isinstance(field_value, bool):
                raise ValueError(f"tier_models.{tier_key}.enabled must be a boolean")
            if field in ("model", "reasoning_effort") and not isinstance(field_value, str):
                raise ValueError(f"tier_models.{tier_key}.{field} must be a string")
            clean[field] = field_value
        out[tier_key] = clean
    return out


def configure_agent(project_path: Path, agent_type: str, patch: dict[str, object]) -> None:
    """Write the Screen 5/6 patch through to ``agentshore.yaml``.

    Recognised patch fields:

    * ``enabled`` — Screen 5 enable/disable toggle.
    * ``identity`` — identity-pool binding (string login or ``None`` to clear).
    * ``tier_models`` — Screen 6 tier configuration, merged onto ``model_tiers``.

    Unknown fields raise ``ValueError`` so the UI surfaces typos instead of
    silently ignoring them.
    """
    unknown = [field for field in patch if field not in _PATCHABLE_FIELDS]
    if unknown:
        raise ValueError(f"unsupported agent patch fields: {sorted(unknown)}")
    if agent_type not in _ALLOWED_AGENT_TYPES:
        raise ValueError(f"unknown agent type: {agent_type}")

    cfg_path = _config_path(project_path)
    data = _load_yaml(cfg_path)
    agents = data.get("agents")
    if agents is None:
        agents = {}
        data["agents"] = agents
    if not isinstance(agents, dict):
        raise ValueError("agents block must be a mapping")

    body_raw = agents.get(agent_type)
    if body_raw is None:
        body: dict[str, object] = {}
        agents[agent_type] = body
    elif isinstance(body_raw, dict):
        body = body_raw
    else:
        raise ValueError(f"agent entry must be a mapping: {agent_type}")

    if "enabled" in patch:
        enabled_value = patch["enabled"]
        if not isinstance(enabled_value, bool):
            raise ValueError("agents.configure 'enabled' must be a boolean")
        body["enabled"] = enabled_value

    if "identity" in patch:
        identity_value = patch["identity"]
        if identity_value is None:
            body.pop("identity", None)
        elif isinstance(identity_value, str):
            body["identity"] = canonical_identity_name(identity_value)
        else:
            raise ValueError("agents.configure 'identity' must be a string or null")

    if "tier_models" in patch:
        cleaned = _validate_tier_models(patch["tier_models"])
        existing_tiers = body.get("model_tiers")
        merged_tiers: dict[str, object] = (
            dict(existing_tiers) if isinstance(existing_tiers, dict) else {}
        )
        for tier_key, tier_patch in cleaned.items():
            current = merged_tiers.get(tier_key)
            current_dict: dict[str, object] = dict(current) if isinstance(current, dict) else {}
            current_dict.update(tier_patch)
            merged_tiers[tier_key] = current_dict
        body["model_tiers"] = merged_tiers

    _write_yaml_atomic(cfg_path, data)


_DEFAULT_MAX_PER_CONFIG = 2
_MAX_PER_CONFIG_LIMIT = 32


def get_spawn_limits(project_path: Path) -> dict[str, int]:
    """Return ``agent_spawn`` limits as a flat dict.

    Currently surfaces ``max_per_config`` (desktop-ty04): the per-(agent_type,
    model_tier) cap. ``max_total`` was removed when per-cell gating became
    the sole ceiling. Adds default if not yet persisted.
    """
    cfg_path = _config_path(project_path)
    if not cfg_path.exists():
        return {"max_per_config": _DEFAULT_MAX_PER_CONFIG}
    data = _load_yaml(cfg_path)
    agent_spawn = data.get("agent_spawn")
    if not isinstance(agent_spawn, dict):
        return {"max_per_config": _DEFAULT_MAX_PER_CONFIG}
    value = agent_spawn.get("max_per_config")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return {"max_per_config": value}
    return {"max_per_config": _DEFAULT_MAX_PER_CONFIG}


def set_spawn_limits(project_path: Path, patch: dict[str, object]) -> None:
    """Update ``agent_spawn`` fields in agentshore.yaml.

    Currently accepts ``max_per_config`` only. The patch shape matches the
    sidecar RPC ``agents.set_spawn_limits`` so the Desktop UI can call it
    directly with the rendered field name (desktop-ty04).
    """
    unknown = [field for field in patch if field != "max_per_config"]
    if unknown:
        raise ValueError(f"unsupported agent_spawn fields: {sorted(unknown)}")
    if "max_per_config" not in patch:
        # Empty patch — no-op rather than rewriting the file.
        return
    value = patch["max_per_config"]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("agent_spawn.max_per_config must be an integer")
    if value < 1 or value > _MAX_PER_CONFIG_LIMIT:
        raise ValueError(
            f"agent_spawn.max_per_config must be between 1 and {_MAX_PER_CONFIG_LIMIT}"
        )

    cfg_path = _config_path(project_path)
    data = _load_yaml(cfg_path)
    agent_spawn = data.get("agent_spawn")
    if not isinstance(agent_spawn, dict):
        agent_spawn = {}
        data["agent_spawn"] = agent_spawn
    agent_spawn["max_per_config"] = value
    _write_yaml_atomic(cfg_path, data)
