"""Handler for custom (test-injected) methods via the ``METHOD_HANDLERS`` table.

``METHOD_HANDLERS`` is a module-level dict in ``agentshore.sidecar.server``.
Tests monkeypatch it there (``monkeypatch.setitem(METHOD_HANDLERS, ...)``) and
``handle_request`` checks it by name in the server module.  This module only
holds the dispatch implementation; the dict itself stays in ``server.py`` so
patches are visible to the caller.
"""

from __future__ import annotations

import inspect

from agentshore.sidecar.rpc.protocol import (
    INTERNAL_ERROR,
    DispatchResult,
    JsonRpcResponse,
    MethodHandler,
    _error,
    _result,
)


def _dispatch_custom_method(
    method: str,
    payload: dict[str, object],
    *,
    req_id: int | str | None,
    is_notification: bool,
    method_handlers: dict[str, MethodHandler],
) -> DispatchResult:
    if is_notification:
        return None
    try:
        result = method_handlers[method](payload)
    except Exception as exc:  # pragma: no cover - defensive guard
        return _error(req_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
    if inspect.isawaitable(result):

        async def _await_handler() -> JsonRpcResponse:
            try:
                resolved = await result
            except Exception as exc:  # pragma: no cover - defensive guard
                return _error(req_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
            return _result(req_id, resolved)

        return _await_handler()
    return _result(req_id, result)
