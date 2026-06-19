"""Handler for the ``preferences.*`` method family.

Unlike ``config.*`` these are project-independent (the file is user-global,
not in ``agentshore.yaml``), so no active project is required. ``set``
persists then, if a session is live, triggers a config reload so the change
(e.g. a disabled play) takes effect mid-session via the same atomic swap
SIGHUP uses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from agentshore.sidecar.preferences import get_preferences, set_preferences
from agentshore.sidecar.rpc.protocol import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    DispatchResult,
    JsonRpcNotification,
    JsonRpcResponse,
    ServerState,
    _error,
    _result,
)


def _dispatch_preferences_rpc(
    method: str,
    raw_params: object,
    *,
    req_id: int | str | None,
    is_notification: bool,
    notify: Callable[[JsonRpcNotification], None] | None,
    state: ServerState,
) -> DispatchResult:
    """Route ``preferences.{get,set}`` to the machine-global preferences file.

    Unlike ``config.*`` these are project-independent (the file is user-global,
    not in ``agentshore.yaml``), so no active project is required. ``set``
    persists then, if a session is live, triggers a config reload so the change
    (e.g. a disabled play) takes effect mid-session via the same atomic swap
    SIGHUP uses.
    """
    if method == "preferences.get":
        return _result(req_id, get_preferences())

    if method != "preferences.set":
        return _error(req_id, METHOD_NOT_FOUND, f"unknown method: {method}")

    if raw_params is not None and not isinstance(raw_params, dict):
        return _error(req_id, INVALID_PARAMS, "params must be an object")
    params = raw_params or {}
    from agentshore.preferences import PreferencesError

    try:
        view = set_preferences(params.get("disabled_plays"))
    except PreferencesError as exc:
        return _error(req_id, INVALID_PARAMS, str(exc))

    orch = state.orchestrator
    if orch is None:
        return _result(req_id, view)

    async def _run_reload() -> JsonRpcResponse:
        await orch.reload_config()
        return _result(req_id, view)

    return _run_reload()
