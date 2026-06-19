"""JSON-RPC request routing — dispatch table and ``handle_request``.

``HANDLERS`` is the exact-match dispatch table.  ``_ROUTE_GROUPS`` is the
prefix-match fallback for families (``identities.*``, ``agents.*``,
``preferences.*``).  ``handle_request`` is the single entry point called
by the serve loop for every parsed request.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from agentshore.sidecar.rpc.handlers.agents import _dispatch_agents_rpc
from agentshore.sidecar.rpc.handlers.app import _dispatch_app_handshake
from agentshore.sidecar.rpc.handlers.archive import _dispatch_archive
from agentshore.sidecar.rpc.handlers.config import _dispatch_config_rpc
from agentshore.sidecar.rpc.handlers.custom import _dispatch_custom_method
from agentshore.sidecar.rpc.handlers.identities import _dispatch_identities_rpc
from agentshore.sidecar.rpc.handlers.preferences import _dispatch_preferences_rpc
from agentshore.sidecar.rpc.handlers.project import _dispatch_project_rpc
from agentshore.sidecar.rpc.handlers.session import _dispatch_session
from agentshore.sidecar.rpc.handlers.session_budget import _dispatch_session_budget
from agentshore.sidecar.rpc.protocol import (
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    DispatchResult,
    JsonRpcNotification,
    MethodHandler,
    RouteHandler,
    ServerState,
    _error,
)


def _active_project_path(state: ServerState) -> Path:
    """Filesystem root for project-relative operations (§1.3).

    Returns the active project path when one has been selected via
    ``project.select``; falls back to the sidecar's cwd otherwise so calls
    made before a project is selected (and existing tests) still resolve.
    """
    if state.active_project_path is not None:
        return Path(state.active_project_path)
    return Path.cwd()


# ---------------------------------------------------------------------------
# Route dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    """One entry in the :data:`HANDLERS` dispatch table.

    ``fn`` is the dispatcher; ``notify_ok`` declares whether the method runs
    when the request arrives as a JSON-RPC notification (no ``id``). The
    default ``False`` makes ``handle_request`` short-circuit to ``None`` for
    notifications — replacing the per-handler ``if is_notification: return
    None`` boilerplate with one declarative rule (H4). Only ``session.*`` opts
    in (``notify_ok=True``) because it emits ``$/progress`` side effects even
    for fire-and-forget stop notifications.
    """

    fn: RouteHandler
    notify_ok: bool = False


# ---------------------------------------------------------------------------
# Adapter wrappers — inject computed extra args for handlers that need them
# ---------------------------------------------------------------------------
# Handlers for identities/agents/config need ``active_project_path``; the
# recents handler needs ``recents_path_fn``.  We wrap them here in thin
# callables that have the *same* uniform signature as every other RouteHandler
# so ``handle_request`` can call every route with a single pattern.


def _wrap_identities(
    method: str,
    raw_params: object,
    *,
    req_id: int | str | None,
    is_notification: bool,
    notify: Callable[[JsonRpcNotification], None] | None,
    state: ServerState,
) -> DispatchResult:
    return _dispatch_identities_rpc(
        method,
        raw_params,
        req_id=req_id,
        is_notification=is_notification,
        notify=notify,
        state=state,
        active_project_path=_active_project_path(state),
    )


def _wrap_agents(
    method: str,
    raw_params: object,
    *,
    req_id: int | str | None,
    is_notification: bool,
    notify: Callable[[JsonRpcNotification], None] | None,
    state: ServerState,
) -> DispatchResult:
    return _dispatch_agents_rpc(
        method,
        raw_params,
        req_id=req_id,
        is_notification=is_notification,
        notify=notify,
        state=state,
        active_project_path=_active_project_path(state),
    )


def _wrap_config(
    method: str,
    raw_params: object,
    *,
    req_id: int | str | None,
    is_notification: bool,
    notify: Callable[[JsonRpcNotification], None] | None,
    state: ServerState,
) -> DispatchResult:
    return _dispatch_config_rpc(
        method,
        raw_params,
        req_id=req_id,
        is_notification=is_notification,
        notify=notify,
        state=state,
        active_project_path=_active_project_path(state),
    )


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

# Exact-match dispatch table. Prefix families (``identities.*``, ``agents.*``)
# fan out from one entry each via _ROUTE_GROUPS below.
HANDLERS: dict[str, Route] = {
    "app.handshake": Route(_dispatch_app_handshake),
    "session.start": Route(_dispatch_session, notify_ok=True),
    "session.status": Route(_dispatch_session, notify_ok=True),
    "session.stop": Route(_dispatch_session, notify_ok=True),
    "session.set_budget": Route(_dispatch_session_budget),
    "session.get_budget": Route(_dispatch_session_budget),
    "project.select": Route(_dispatch_project_rpc),
    "project.inspect": Route(_dispatch_project_rpc),
    "project.branches": Route(_dispatch_project_rpc),
    "project.set_target_branch": Route(_dispatch_project_rpc),
    "project.set_seed_paths": Route(_dispatch_project_rpc),
    "project.set_budget": Route(_dispatch_project_rpc),
    "project.set_trusted_issue_enforcement": Route(_dispatch_project_rpc),
    "project.set_timelapse": Route(_dispatch_project_rpc),
    "project.install_timelapse": Route(_dispatch_project_rpc),
    "project.deselect": Route(_dispatch_project_rpc),
    "archive.list": Route(_dispatch_archive),
    "archive.fetch_report": Route(_dispatch_archive),
    "archive.fetch_logs": Route(_dispatch_archive),
    "config.read": Route(_wrap_config),
    "config.write": Route(_wrap_config),
}

# Prefix-matched dispatch groups, tried after the exact table. Each family
# keeps its single fan-out function and resolves the concrete method itself.
_ROUTE_GROUPS: tuple[tuple[str, Route], ...] = (
    ("identities.", Route(_wrap_identities)),
    ("agents.", Route(_wrap_agents)),
    ("preferences.", Route(_dispatch_preferences_rpc)),
)


def _resolve_route(method: str) -> Route | None:
    route = HANDLERS.get(method)
    if route is not None:
        return route
    for prefix, group in _ROUTE_GROUPS:
        if method.startswith(prefix):
            return group
    return None


def handle_request(
    payload: object,
    notify: Callable[[JsonRpcNotification], None] | None = None,
    *,
    state: ServerState | None = None,
    method_handlers: dict[str, MethodHandler] | None = None,
) -> DispatchResult:
    """Dispatch a single parsed request payload.

    Returns ``None`` for notifications (no ``id``); per JSON-RPC 2.0 these
    receive no response. Methods that need async work (e.g. ``session.stop``
    or the ``archive.*`` family) return an awaitable that yields the final
    response — the calling stdio loops await before serialising.

    ``method_handlers`` is the custom-method lookup dict; callers pass the
    server-module-level ``METHOD_HANDLERS`` so monkeypatches are visible.
    """
    if not isinstance(payload, dict):
        return _error(None, INVALID_REQUEST, "request must be a JSON object")
    if payload.get("jsonrpc") != "2.0":
        return _error(payload.get("id"), INVALID_REQUEST, "jsonrpc must be '2.0'")
    method = payload.get("method")
    if not isinstance(method, str):
        return _error(payload.get("id"), INVALID_REQUEST, "method must be a string")

    req_id = payload.get("id")
    is_notification = "id" not in payload
    _state = state or ServerState()

    route = _resolve_route(method)
    if route is not None:
        if is_notification and not route.notify_ok:
            return None
        return route.fn(
            method,
            payload.get("params"),
            req_id=req_id,
            is_notification=is_notification,
            notify=notify,
            state=_state,
        )

    _method_handlers = method_handlers if method_handlers is not None else {}
    if method in _method_handlers:
        return _dispatch_custom_method(
            method,
            payload,
            req_id=req_id,
            is_notification=is_notification,
            method_handlers=_method_handlers,
        )

    if is_notification:
        return None
    return _error(req_id, METHOD_NOT_FOUND, f"unknown method: {method}")
