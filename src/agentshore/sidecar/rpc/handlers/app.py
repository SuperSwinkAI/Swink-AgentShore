"""Handler for the ``app.*`` method family (currently just ``app.handshake``)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from agentshore.sidecar.handshake import build_response, validate_params
from agentshore.sidecar.rpc.protocol import (
    INVALID_REQUEST,
    DispatchResult,
    JsonRpcNotification,
    ServerState,
    _error,
    _result,
)


def _dispatch_app_handshake(
    method: str,
    raw_params: object,
    *,
    req_id: int | str | None,
    is_notification: bool,
    notify: Callable[[JsonRpcNotification], None] | None,
    state: ServerState,
) -> DispatchResult:
    try:
        handshake_params = validate_params(raw_params)
    except ValueError as exc:
        return _error(req_id, INVALID_REQUEST, str(exc))
    response = build_response()
    if handshake_params["client_build_id"] != response["sidecar_build_id"]:
        return _error(req_id, INVALID_REQUEST, "build_id mismatch")
    return _result(req_id, response)
