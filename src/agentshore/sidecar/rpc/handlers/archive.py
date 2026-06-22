"""Handler for the ``archive.*`` method family."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from agentshore.sidecar import archive_rpc
from agentshore.sidecar.archive_rpc import ArchiveError
from agentshore.sidecar.rpc.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    JsonRpcNotification,
    JsonRpcResponse,
    ServerState,
    _as_dict,
    _error,
    _ParamError,
    _result,
)


async def _dispatch_archive(
    method: str,
    raw_params: object,
    *,
    req_id: int | str | None,
    is_notification: bool,
    notify: Callable[[JsonRpcNotification], None] | None,
    state: ServerState,
) -> JsonRpcResponse:
    store = state.data_store
    if store is None and state.session_context is not None:
        store = state.session_context.store
    if store is None:
        return _error(req_id, INTERNAL_ERROR, "data store not available")
    try:
        if method == "archive.list":
            return _result(req_id, await archive_rpc.list_archives(store))
        if method == "archive.fetch_report":
            params = _as_dict(raw_params)
            archive_id = params.get("archive_id")
            if not isinstance(archive_id, str):
                return _error(req_id, INVALID_PARAMS, "archive_id (string) required")
            return _result(req_id, await archive_rpc.fetch_report(store, archive_id))
        if method == "archive.fetch_logs":
            params = _as_dict(raw_params)
            archive_id = params.get("archive_id")
            if not isinstance(archive_id, str):
                return _error(req_id, INVALID_PARAMS, "archive_id (string) required")
            range_value = params.get("range")
            if range_value is not None and not isinstance(range_value, dict):
                return _error(req_id, INVALID_PARAMS, "range must be an object")
            logs = await archive_rpc.fetch_logs(store, archive_id, range_=range_value)
            return _result(req_id, logs)
    except ArchiveError as exc:
        return _error(req_id, exc.code, str(exc))
    except _ParamError as exc:
        return _error(req_id, INVALID_PARAMS, str(exc))
    return _error(req_id, METHOD_NOT_FOUND, f"unknown method: {method}")
