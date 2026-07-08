"""Handler for the ``agents.*`` method family."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

from agentshore.sidecar.agents import (
    agents_catalog,
    configure_agent,
    detect_available_agents,
    list_agents,
)
from agentshore.sidecar.rpc.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    DispatchResult,
    JsonRpcNotification,
    JsonRpcResponse,
    ServerState,
    _error,
    _result,
)


def _dispatch_agents_rpc(
    method: str,
    raw_params: object,
    *,
    req_id: int | str | None,
    is_notification: bool,
    notify: Callable[[JsonRpcNotification], None] | None,
    state: ServerState,
    active_project_path: Path,
) -> DispatchResult:
    if method == "agents.list":
        try:
            return _result(req_id, list_agents(active_project_path))
        except OSError as exc:
            return _error(req_id, INTERNAL_ERROR, f"agents.list: {exc}")
    if method == "agents.detect":
        return _result(req_id, detect_available_agents())
    if method == "agents.catalog":
        return _result(req_id, agents_catalog())

    if method == "agents.check_auth":
        # Run off the serve loop so concurrent setup-screen RPCs don't serialize
        # behind the per-agent auth shell-out; probe failures return error rows, never raise.
        obj_params = raw_params if isinstance(raw_params, dict) else {}
        _project_path = active_project_path

        async def _run_check_auth() -> JsonRpcResponse:
            from agentshore.sidecar.agent_auth import check_auth

            return _result(req_id, await check_auth(_project_path, obj_params))

        return _run_check_auth()

    if method == "agents.refresh_models":
        # Run off the serve loop: the free-harness probes alone take up to a
        # few seconds each, and the opt-in Claude Code path can take minutes
        # (see model_discovery_llm.py) — must never block other RPCs.
        obj_params = raw_params if isinstance(raw_params, dict) else {}
        include_claude_code = bool(obj_params.get("include_claude_code", False))
        tier = obj_params.get("tier")
        max_budget_usd = obj_params.get("max_budget_usd")
        dry_run = bool(obj_params.get("dry_run", False))
        if tier is not None and not isinstance(tier, str):
            return _error(req_id, INVALID_PARAMS, "agents.refresh_models: 'tier' must be a string")
        if max_budget_usd is not None and not isinstance(max_budget_usd, (int, float)):
            return _error(
                req_id, INVALID_PARAMS, "agents.refresh_models: 'max_budget_usd' must be a number"
            )

        async def _run_refresh_models() -> JsonRpcResponse:
            import asyncio

            from agentshore.agents.model_discovery_llm import (
                DEFAULT_MAX_BUDGET_USD,
                DEFAULT_MODEL_TIER,
            )
            from agentshore.agents.model_refresh import refresh_model_catalog

            summary = await asyncio.to_thread(
                refresh_model_catalog,
                include_claude_code=include_claude_code,
                claude_code_tier=tier or DEFAULT_MODEL_TIER,
                claude_code_max_budget_usd=(
                    DEFAULT_MAX_BUDGET_USD if max_budget_usd is None else float(max_budget_usd)
                ),
                dry_run=dry_run,
            )
            return _result(req_id, summary.to_jsonable())

        return _run_refresh_models()

    if method == "agents.configure":
        if not isinstance(raw_params, dict):
            return _error(req_id, INVALID_PARAMS, "agents.configure requires object params")
        agent_type = raw_params.get("type")
        if not isinstance(agent_type, str) or not agent_type:
            return _error(req_id, INVALID_PARAMS, "agents.configure requires string 'type'")
        patch = {k: v for k, v in raw_params.items() if k != "type"}
        try:
            configure_agent(active_project_path, agent_type, patch)
        except ValueError as exc:
            return _error(req_id, INVALID_PARAMS, str(exc))
        except OSError as exc:
            return _error(req_id, INTERNAL_ERROR, f"agents.configure: {exc}")
        return _result(req_id, {})

    return _error(req_id, METHOD_NOT_FOUND, f"unknown method: {method}")
