"""``agents.check_auth`` RPC handler — desktop CLI-agent backend-auth probing.

The desktop setup screen calls this to render a per-agent badge proving each
configured CLI agent's *backend* auth (e.g. the Codex CLI's cached
``chatgpt.com`` session token) is currently valid — the same check the
``session.start`` launch gate runs (``session_lifecycle._check_agent_auth``),
so a green badge here provably means the gate will pass.

It is intentionally separate from ``identities.check_access`` (which validates
the *GitHub* identity token an agent commits/merges with) — backend auth and
GitHub auth are independent failure modes.

The handler never raises on a probe failure: every outcome is represented as a
row the frontend can render. The blocking ``subprocess.run`` probe is wrapped
in ``asyncio.to_thread`` so the serve loop keeps pumping.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from agentshore.agents.auth_probe import AUTH_ERROR
from agentshore.state import AgentType

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.agents.auth_probe import AuthProbeResult
    from agentshore.config import RuntimeConfig


def _row(result: AuthProbeResult) -> dict[str, object]:
    """Project one probe result onto the frontend row shape."""
    return {
        "agent_type": result.agent_type.value,
        "status": result.status,
        "detail": result.detail,
    }


def _load_cfg(project_path: Path) -> RuntimeConfig:
    """Load the active project's merged RuntimeConfig from ``agentshore.yaml``."""
    from agentshore.config import load_config

    return load_config(project_path / "agentshore.yaml")


def _probe_all(cfg: RuntimeConfig) -> list[dict[str, object]]:
    """Probe every configured CLI agent's backend auth (blocking)."""
    # Import at call time so launch-gate tests' patch("...auth_probe.*") applies here.
    from agentshore.agents import auth_probe

    return [_row(result) for result in auth_probe.probe_configured_cli_auth(cfg)]


def _probe_one(cfg: RuntimeConfig, agent_type: AgentType) -> list[dict[str, object]]:
    """Probe a single configured CLI agent's backend auth (blocking).

    Reuses the configured agent's ``binary`` override and identity env overlay
    so the probe matches how the Agent Manager spawns it, mirroring
    ``probe_configured_cli_auth``. Returns a single-row list.
    """
    from agentshore.agents import auth_probe
    from agentshore.agents.identity import resolve_identity_env

    for configured_type, agent_cfg in auth_probe.configured_cli_agent_types(cfg):
        if configured_type is agent_type:
            try:
                env = resolve_identity_env(cfg, agent_cfg)
            except Exception:
                env = {}
            result = auth_probe.probe_cli_auth(agent_type, env, binary=agent_cfg.binary)
            return [_row(result)]
    # Not configured: probe with defaults so the setup screen gets a row, not [].
    return [_row(auth_probe.probe_cli_auth(agent_type))]


async def check_auth(project_path: Path, params: dict[str, object]) -> dict[str, object]:
    """Probe configured CLI agents' backend auth and return frontend rows.

    With no ``agent_type`` param, probes every enabled CLI agent. With
    ``{"agent_type": "codex"}``, probes only that type. Never raises on a probe
    failure — config-load and per-probe failures are represented as
    error-status rows so the setup screen always renders.

    Returns ``{"agents": [{"agent_type", "status", "detail"}, ...]}``.
    """
    requested = params.get("agent_type")
    target: AgentType | None = None
    if isinstance(requested, str) and requested:
        try:
            target = AgentType(requested)
        except ValueError:
            return {
                "agents": [
                    {
                        "agent_type": requested,
                        "status": AUTH_ERROR,
                        "detail": f"unknown agent type: {requested!r}",
                    }
                ]
            }

    try:
        cfg = await asyncio.to_thread(_load_cfg, project_path)
    except Exception as exc:  # noqa: BLE001 — surface config-load failure as a row
        agent_label = target.value if target is not None else "unknown"
        return {
            "agents": [
                {
                    "agent_type": agent_label,
                    "status": AUTH_ERROR,
                    "detail": f"could not load agentshore.yaml: {str(exc)[:200]}",
                }
            ]
        }

    if target is not None:
        rows = await asyncio.to_thread(_probe_one, cfg, target)
    else:
        rows = await asyncio.to_thread(_probe_all, cfg)
    return {"agents": rows}
