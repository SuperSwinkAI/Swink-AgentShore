"""Handler for the ``session.{get,set}_budget`` method sub-family.

Unlike ``project.set_budget`` (which only rewrites ``agentshore.yaml``), these
operate on ``state.orchestrator`` — the running engine instance — so cap
changes take effect mid-session. ``set_budget`` is absolute-set and persists;
``get_budget`` is a read-only echo. Both require a live session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from agentshore.sidecar.rpc.protocol import (
    ERR_SESSION_ACTIVE,
    INVALID_PARAMS,
    INVALID_REQUEST,
    DispatchResult,
    JsonRpcNotification,
    JsonRpcResponse,
    ServerState,
    _as_dict,
    _error,
    _ParamError,
    _result,
)


def _dispatch_session_budget(
    method: str,
    raw_params: object,
    *,
    req_id: int | str | None,
    is_notification: bool,
    notify: Callable[[JsonRpcNotification], None] | None,
    state: ServerState,
) -> DispatchResult:
    """Route ``session.{set_budget,get_budget}`` to the LIVE orchestrator (issue #41).

    Unlike ``project.set_budget`` (which only rewrites ``agentshore.yaml``), these
    operate on ``state.orchestrator`` — the running engine instance — so cap
    changes take effect mid-session. ``set_budget`` is absolute-set and persists;
    ``get_budget`` is a read-only echo. Both require a live session.
    """
    if raw_params is not None and not isinstance(raw_params, dict):
        return _error(req_id, INVALID_REQUEST, "params must be an object")

    # ``state.orchestrator`` is typed via the TYPE_CHECKING-only
    # ``OrchestratorHandle`` Protocol — the concrete engine import stays lazy
    # (cold-start torch-free invariant) while these live methods type-check.
    orch = state.orchestrator
    if orch is None:
        return _error(req_id, ERR_SESSION_ACTIVE, "no active session")

    if method == "session.get_budget":

        async def _run_get() -> JsonRpcResponse:
            current = orch.current_budget
            return _result(req_id, {"budget": await current()})

        return _run_get()

    if method == "session.set_budget":
        try:
            obj_params = _as_dict(raw_params)
        except _ParamError as exc:
            return _error(req_id, INVALID_PARAMS, str(exc))
        budget_raw = obj_params.get("budget")
        if not isinstance(budget_raw, dict):
            return _error(req_id, INVALID_PARAMS, "session.set_budget requires object 'budget'")
        from agentshore.budget import validate_budget_payload
        from agentshore.errors import ConfigError, OrchestratorError

        try:
            validated = validate_budget_payload(budget_raw)
        except ConfigError as exc:
            return _error(req_id, INVALID_PARAMS, str(exc))

        async def _run_set() -> JsonRpcResponse:
            set_budget = orch.set_budget
            try:
                applied = await set_budget(
                    dollars_enabled=validated.enabled,
                    dollars=validated.total if validated.enabled else None,
                    time_enabled=validated.time_enabled,
                    time_minutes=validated.time_total_minutes if validated.time_enabled else None,
                    persist=True,
                )
            except OrchestratorError as exc:
                return _error(req_id, INVALID_PARAMS, str(exc))
            return _result(req_id, {"budget": applied})

        return _run_set()

    raise KeyError(method)  # pragma: no cover — guarded by HANDLERS routing
