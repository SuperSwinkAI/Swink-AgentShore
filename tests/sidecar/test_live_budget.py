"""Tests for the sidecar live-budget RPC (``session.{set_budget,get_budget}``, issue #41).

These routes drive the LIVE ``state.orchestrator`` (not ``agentshore.yaml``), so the
orchestrator is mocked with ``AsyncMock`` for ``set_budget`` / ``current_budget``.
"""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock

from agentshore.errors import OrchestratorError
from agentshore.sidecar.server import (
    ERR_SESSION_ACTIVE,
    INVALID_PARAMS,
    ServerState,
    handle_request,
)

_APPLIED = {
    "enabled": True,
    "total": 25.0,
    "spent": 3.0,
    "remaining": 22.0,
    "time_enabled": True,
    "time_total_minutes": 120,
    "time_elapsed_minutes": 10,
    "time_remaining_minutes": 110,
    "resumed": False,
}


def _drive(payload: dict[str, object], *, state: ServerState | None = None) -> dict[str, object]:
    """Run ``handle_request`` and await any coroutine response."""
    response = handle_request(payload, state=state)
    if asyncio.iscoroutine(response):
        response = asyncio.run(response)
    assert response is not None
    return cast("dict[str, object]", response)


def _live_state() -> tuple[ServerState, AsyncMock]:
    orch = AsyncMock()
    orch.set_budget = AsyncMock(return_value=dict(_APPLIED))
    orch.current_budget = AsyncMock(return_value=dict(_APPLIED))
    state = ServerState()
    state.orchestrator = orch
    return state, orch


def _request(method: str, params: object | None = None, *, req_id: int = 1) -> dict[str, object]:
    payload: dict[str, object] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def test_set_budget_applies_and_echoes_caps() -> None:
    state, orch = _live_state()
    budget = {
        "enabled": True,
        "total": 25.0,
        "time_enabled": True,
        "time_total_minutes": 120,
    }
    response = _drive(_request("session.set_budget", {"budget": budget}), state=state)

    assert "error" not in response
    assert response["result"] == {"budget": _APPLIED}
    orch.set_budget.assert_awaited_once_with(
        dollars_enabled=True,
        dollars=25.0,
        time_enabled=True,
        time_minutes=120,
        persist=True,
    )


def test_set_budget_disabled_passes_through_optional_fields() -> None:
    state, orch = _live_state()
    response = _drive(_request("session.set_budget", {"budget": {"enabled": False}}), state=state)

    assert "error" not in response
    orch.set_budget.assert_awaited_once_with(
        dollars_enabled=False,
        dollars=None,
        time_enabled=False,
        time_minutes=None,
        persist=True,
    )


def test_get_budget_echoes_current() -> None:
    state, orch = _live_state()
    response = _drive(_request("session.get_budget"), state=state)

    assert "error" not in response
    assert response["result"] == {"budget": _APPLIED}
    orch.current_budget.assert_awaited_once_with()


def test_set_budget_without_orchestrator_errors() -> None:
    state = ServerState()  # no live orchestrator
    response = _drive(_request("session.set_budget", {"budget": {"enabled": False}}), state=state)

    error = cast("dict[str, object]", response["error"])
    assert error["code"] == ERR_SESSION_ACTIVE
    assert "no active session" in cast("str", error["message"])


def test_get_budget_without_orchestrator_errors() -> None:
    state = ServerState()
    response = _drive(_request("session.get_budget"), state=state)

    error = cast("dict[str, object]", response["error"])
    assert error["code"] == ERR_SESSION_ACTIVE


def test_set_budget_bounds_rejection_is_invalid_params() -> None:
    state, orch = _live_state()
    orch.set_budget = AsyncMock(side_effect=OrchestratorError("dollar cap must be at least $5.00"))
    response = _drive(
        _request("session.set_budget", {"budget": {"enabled": True, "total": 0.5}}),
        state=state,
    )

    error = cast("dict[str, object]", response["error"])
    assert error["code"] == INVALID_PARAMS
    assert "at least" in cast("str", error["message"])


def test_set_budget_missing_budget_object_is_invalid_params() -> None:
    state, _orch = _live_state()
    response = _drive(_request("session.set_budget", {}), state=state)

    error = cast("dict[str, object]", response["error"])
    assert error["code"] == INVALID_PARAMS


def test_set_budget_non_bool_enabled_is_invalid_params() -> None:
    state, _orch = _live_state()
    response = _drive(_request("session.set_budget", {"budget": {"enabled": "yes"}}), state=state)

    error = cast("dict[str, object]", response["error"])
    assert error["code"] == INVALID_PARAMS
