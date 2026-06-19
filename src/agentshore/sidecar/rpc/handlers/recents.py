"""Handler for the ``recents.*`` method family.

NOTE: ``recents_path`` is intentionally NOT imported at module level here.
The production caller (``server.py``) imports this handler and holds
``recents_path`` in *its own* namespace.  Tests monkeypatch
``agentshore.sidecar.server.recents_path``; for the patch to be visible,
the call to ``recents_path()`` must happen through the ``server`` module's
attribute.  Therefore ``_dispatch_recents_rpc`` accepts ``recents_path_fn``
as an explicit parameter so the caller can pass the right (potentially
patched) lookup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

from agentshore.sidecar.recents import list_recents, remove_recent, touch_recent
from agentshore.sidecar.rpc.protocol import (
    INVALID_PARAMS,
    DispatchResult,
    JsonRpcNotification,
    ServerState,
    _error,
    _result,
)


def _extract_path_param(params: object) -> str | None:
    """Pull a ``path`` string out of JSON-RPC params (positional or named)."""
    if isinstance(params, dict):
        value = params.get("path")
        return value if isinstance(value, str) else None
    if isinstance(params, list) and params:
        value = params[0]
        return value if isinstance(value, str) else None
    return None


def _dispatch_recents_rpc(
    method: str,
    raw_params: object,
    *,
    req_id: int | str | None,
    is_notification: bool,
    notify: Callable[[JsonRpcNotification], None] | None,
    state: ServerState,
    recents_path_fn: Callable[[], Path],
) -> DispatchResult:
    if method == "recents.list":
        return _result(req_id, list_recents(recents_path_fn()))

    path = _extract_path_param(raw_params)
    if path is None:
        return _error(req_id, INVALID_PARAMS, "path (string) is required")
    if method == "recents.touch":
        touch_recent(path, recents_path_fn())
    else:
        remove_recent(path, recents_path_fn())
    return _result(req_id, None)
