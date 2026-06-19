"""Handler for the ``config.*`` method family."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

from agentshore.sidecar.config import read_config, write_config
from agentshore.sidecar.rpc.protocol import (
    INVALID_PARAMS,
    DispatchResult,
    JsonRpcNotification,
    ServerState,
    _error,
    _result,
)


def _dispatch_config_rpc(
    method: str,
    raw_params: object,
    *,
    req_id: int | str | None,
    is_notification: bool,
    notify: Callable[[JsonRpcNotification], None] | None,
    state: ServerState,
    active_project_path: Path,
) -> DispatchResult:
    if method == "config.read":
        return _result(req_id, read_config(active_project_path))

    if not isinstance(raw_params, dict):
        return _error(req_id, INVALID_PARAMS, "params must be an object")
    patch = raw_params.get("patch")
    if not isinstance(patch, dict):
        return _error(req_id, INVALID_PARAMS, "params.patch must be a mapping")
    try:
        write_config(active_project_path, patch)
    except TypeError as exc:
        return _error(req_id, INVALID_PARAMS, str(exc))
    return _result(req_id, {})
