"""Handler for the ``project.*`` method family."""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from agentshore.sidecar import project as project_rpc
from agentshore.sidecar.rpc.protocol import (
    _PROJECT_NO_ACTIVE_REMAP,
    ERR_NO_ACTIVE_PROJECT,
    ERR_SESSION_ACTIVE,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    DispatchResult,
    JsonRpcNotification,
    JsonRpcResponse,
    ServerState,
    _as_dict,
    _error,
    _ParamError,
    _result,
)


def _active_project_path_str(state: ServerState) -> str | None:
    """Return the raw active project path string (or None)."""
    return state.active_project_path


async def _close_project_handles(state: ServerState) -> None:
    """Close DB handles before switching to a different active project (§1.3)."""
    store = state.data_store
    if store is None:
        return
    state.data_store = None
    with contextlib.suppress(Exception):
        await store.close()


async def _finalize_project_select(
    resolved: str,
    state: ServerState,
    req_id: int | str | None,
    *,
    include_inspect: bool = True,
) -> JsonRpcResponse:
    state.active_project_path = resolved
    # Finder-launched sidecar has cwd "/"; child spawns then traverse ~/Music
    # etc. on relative ops, triggering macOS TCC prompts (desktop-2za5).
    # Re-anchor cwd here so downstream spawns inherit the project dir.
    with contextlib.suppress(OSError):
        os.chdir(resolved)
    if not include_inspect:
        return _result(req_id, {"path": resolved})
    try:
        inspect_result = await project_rpc.inspect()
    except project_rpc.ProjectError as exc:
        return _error(req_id, exc.code, str(exc))
    return _result(req_id, {"path": resolved, "inspect": inspect_result})


def _dispatch_project(method: str, params: object, state: ServerState) -> object:
    """Route a non-``select`` ``project.*`` call to the matching implementation."""
    obj_params = _as_dict(params)
    if method == "project.inspect":
        return project_rpc.inspect()
    if method == "project.branches":
        refresh = obj_params.get("refresh", False)
        if not isinstance(refresh, bool):
            raise _ParamError("project.branches 'refresh' must be a boolean")
        return project_rpc.branches(refresh=refresh)
    if method == "project.set_target_branch":
        name = obj_params.get("name")
        if not isinstance(name, str):
            raise _ParamError("project.set_target_branch requires string 'name'")
        return project_rpc.set_target_branch(name)
    if method == "project.set_seed_paths":
        seed_param = obj_params.get("seed_paths")
        if not isinstance(seed_param, str | list):
            raise _ParamError("project.set_seed_paths requires string or list 'seed_paths'")
        return project_rpc.set_seed_paths(seed_param)
    if method == "project.set_budget":
        budget_param = obj_params.get("budget")
        if not isinstance(budget_param, dict):
            raise _ParamError("project.set_budget requires object 'budget'")
        return project_rpc.set_budget(budget_param)
    if method == "project.set_trusted_issue_enforcement":
        enabled = obj_params.get("enabled")
        if not isinstance(enabled, bool):
            raise _ParamError("project.set_trusted_issue_enforcement requires boolean 'enabled'")
        return project_rpc.set_trusted_issue_enforcement(enabled)
    if method == "project.set_timelapse":
        timelapse_param = obj_params.get("timelapse")
        if not isinstance(timelapse_param, dict):
            raise _ParamError("project.set_timelapse requires object 'timelapse'")
        return project_rpc.set_timelapse(timelapse_param)
    if method == "project.deselect":
        return project_rpc.deselect()
    raise KeyError(method)  # pragma: no cover — guarded by HANDLERS routing


def _dispatch_project_select(
    raw_params: object,
    state: ServerState,
    req_id: int | str | None,
) -> JsonRpcResponse | Awaitable[JsonRpcResponse]:
    """``project.select`` with DESIGN §1.3 side-effects.

    Switching to a different project closes existing DB handles before
    repointing the active-project slot, then re-runs ``project.inspect`` so
    the response carries the fresh inspect envelope. Idempotent calls (same
    resolved path) skip the close and the slot move. Switching while a
    session is active is rejected with ``ERR_SESSION_ACTIVE``.
    """
    try:
        obj_params = _as_dict(raw_params)
    except _ParamError as exc:
        return _error(req_id, INVALID_PARAMS, str(exc))
    path = obj_params.get("path")
    if not isinstance(path, str):
        return _error(req_id, INVALID_PARAMS, "project.select requires string 'path'")
    include_inspect = obj_params.get("include_inspect", True)
    if not isinstance(include_inspect, bool):
        return _error(req_id, INVALID_PARAMS, "project.select 'include_inspect' must be a boolean")

    prior_path = state.active_project_path
    if state.session_active and prior_path is not None and prior_path != path:
        return _error(req_id, ERR_SESSION_ACTIVE, "ERR_SESSION_ACTIVE")

    try:
        select_result = project_rpc.select(path)
    except project_rpc.ProjectError as exc:
        return _error(req_id, exc.code, str(exc))
    resolved = select_result.get("path")
    if not isinstance(resolved, str):
        return _error(req_id, INTERNAL_ERROR, "project.select returned no resolved path")

    is_switch = prior_path is not None and prior_path != resolved
    if is_switch and state.data_store is not None:

        async def _switch_with_close() -> JsonRpcResponse:
            await _close_project_handles(state)
            return await _finalize_project_select(
                resolved,
                state,
                req_id,
                include_inspect=include_inspect,
            )

        return _switch_with_close()

    return _finalize_project_select(resolved, state, req_id, include_inspect=include_inspect)


def _dispatch_install_timelapse(req_id: int | str | None) -> Awaitable[JsonRpcResponse]:
    """``project.install_timelapse`` — long-running auto-install, returns a coroutine."""

    async def _run() -> JsonRpcResponse:
        try:
            result = await project_rpc.install_timelapse()
        except project_rpc.ProjectError as exc:
            if exc.code == project_rpc.ERR_PROJECT_NOT_ACTIVE:
                return _error(req_id, ERR_NO_ACTIVE_PROJECT, str(exc))
            return _error(req_id, exc.code, str(exc))
        except Exception as exc:  # pragma: no cover — defensive guard
            return _error(req_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        return _result(req_id, result)

    return _run()


def _dispatch_project_rpc(
    method: str,
    raw_params: object,
    *,
    req_id: int | str | None,
    is_notification: bool,
    notify: Callable[[JsonRpcNotification], None] | None,
    state: ServerState,
) -> DispatchResult:
    if method == "project.select":
        return _dispatch_project_select(raw_params, state, req_id)
    if method == "project.install_timelapse":
        return _dispatch_install_timelapse(req_id)

    # inspect/branches are coroutines — wrap so ProjectError/_ParamError raised
    # during await is handled consistently.
    if method in ("project.inspect", "project.branches"):

        async def _run_async_project() -> JsonRpcResponse:
            try:
                coro = _dispatch_project(method, raw_params, state)
                result = await coro  # type: ignore[misc]
            except _ParamError as exc:
                return _error(req_id, INVALID_PARAMS, str(exc))
            except project_rpc.ProjectError as exc:
                if (
                    method in _PROJECT_NO_ACTIVE_REMAP
                    and exc.code == project_rpc.ERR_PROJECT_NOT_ACTIVE
                ):
                    return _error(req_id, ERR_NO_ACTIVE_PROJECT, str(exc))
                return _error(req_id, exc.code, str(exc))
            return _result(req_id, result)

        return _run_async_project()

    try:
        result = _dispatch_project(method, raw_params, state)
        if method == "project.deselect":
            state.active_project_path = None
    except _ParamError as exc:
        return _error(req_id, INVALID_PARAMS, str(exc))
    except project_rpc.ProjectError as exc:
        if method in _PROJECT_NO_ACTIVE_REMAP and exc.code == project_rpc.ERR_PROJECT_NOT_ACTIVE:
            return _error(req_id, ERR_NO_ACTIVE_PROJECT, str(exc))
        return _error(req_id, exc.code, str(exc))
    except Exception as exc:  # pragma: no cover — defensive guard
        return _error(req_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
    return _result(req_id, result)
