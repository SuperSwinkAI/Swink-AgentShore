"""Line-framed JSON-RPC 2.0 server over stdin/stdout.

Implements the lifecycle surface defined in ``docs/design/desktop/DESIGN.md``
§5.1. Covers ``app.handshake``, the ``project.*`` family (``select``,
``inspect``, ``branches``, ``set_target_branch``, ``deselect``), and the
``recents.*`` methods (§4.2: ``list``, ``touch``, ``remove``).
Every other method returns ``-32601 MethodNotFound``.

Stdin/stdout carry JSON-RPC; logs go to stderr (§2.2). The loop exits on
EOF, matching Tauri sidecar lifecycle (§1.2: "Sidecar death == orchestrator
death").

A single stdio serve loop (:func:`_serve_async`) backs both the async path
and the synchronous :func:`serve` / :func:`run` entry points. It reads stdin
on a daemon thread so other asyncio tasks (notably the embedded
:class:`agentshore.sidecar.EmbeddedBridge` per §1.2 and §2.3, booted by
``session.start``) run concurrently in the same loop, carries the
request-cancellation machinery (``$/cancelRequest``), and fires the
``sidecar.health`` heartbeat (§5.1) so the shell can detect a stalled sidecar.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import sys
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, TypedDict

from agentshore.ipc.wire import frame
from agentshore.sidecar import archive_rpc
from agentshore.sidecar import project as project_rpc
from agentshore.sidecar.agents import (
    agents_catalog,
    configure_agent,
    detect_available_agents,
    get_spawn_limits,
    list_agents,
    set_spawn_limits,
)
from agentshore.sidecar.archive_rpc import ArchiveError
from agentshore.sidecar.config import read_config, write_config
from agentshore.sidecar.esr import build_esr_payload
from agentshore.sidecar.handshake import build_response, validate_params
from agentshore.sidecar.identities import (
    add_identity,
    keychain_status,
    list_identities,
    remove_identity,
    update_identity,
)
from agentshore.sidecar.recents import (
    list_recents,
    recents_path,
    remove_recent,
    touch_recent,
)
from agentshore.sidecar.session_lifecycle import (
    DEFAULT_DRAIN_TIMEOUT_SECONDS,
    SessionStartError,
    run_session_start,
)

if TYPE_CHECKING:
    from agentshore.data.store import DataStore
    from agentshore.sidecar.embedded_bridge import EmbeddedBridge

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
REQUEST_CANCELLED = -32800
ERR_SESSION_ACTIVE = -32010
ERR_NO_ACTIVE_PROJECT = -32011

MethodHandler = Callable[[dict[str, object]], object | Awaitable[object]]
# Empty by default — `app.handshake` is dispatched explicitly below. Tests
# inject ad-hoc methods (e.g. ``test.slow``) to exercise dispatch and cancel.
METHOD_HANDLERS: dict[str, MethodHandler] = {}


@dataclass
class SessionContext:
    """Per-session handles passed by the embedded bridge to the RPC server.

    Populated when ``session.start`` succeeds so ``session.stop`` can build a
    rich ESR payload. Left ``None`` outside an active session.
    """

    session_id: str
    store: DataStore
    archive_path: str
    report_path: str
    log_path: str | None


@dataclass
class ServerState:
    """In-memory lifecycle state shared across requests on one sidecar instance."""

    active_project_path: str | None = None
    session_active: bool = False
    session_id: str | None = None
    started_at: str | None = None
    ipc_endpoint: dict[str, object] | None = None
    session_context: SessionContext | None = None
    data_store: DataStore | None = None
    # Set by run_session_start when the dashboard bridge boots; session.stop
    # uses it to tear the bridge down before signalling completion.
    bridge: EmbeddedBridge | None = None
    # Populated when session.start boots a real Orchestrator (DESIGN §5.1).
    # ``orchestrator`` is the engine instance; ``orchestrator_task`` is the
    # supervised ``run_until_idle`` task. session.stop drives drain/hard
    # shutdown through these handles before tearing down the bridge.
    # Typed as ``object`` so the import stays lazy and the cold-start
    # torch-free invariant (test_cold_start_torch_free.py) is preserved.
    orchestrator: object | None = None
    orchestrator_task: asyncio.Task[None] | None = None
    esr_ready_report_path: str | None = None
    esr_ready_log_path: str | None = None
    # Optional timelapse capture (desktop feature). ``timelapse_run_id`` is the
    # CLI run-id of an active capture; ``timelapse_runs_cwd`` is the working dir
    # every timelapse call must share so the run-id resolves. session.stop stops
    # the capture and attaches the rendered MP4 path to the ESR payload.
    timelapse_run_id: str | None = None
    timelapse_runs_cwd: Path | None = None


class JsonRpcError(TypedDict):
    code: int
    message: str


class JsonRpcResponse(TypedDict, total=False):
    jsonrpc: str
    id: int | str | None
    result: object
    error: JsonRpcError


class JsonRpcNotification(TypedDict):
    jsonrpc: str
    method: str
    params: dict[str, object]


DispatchResult = JsonRpcResponse | Awaitable[JsonRpcResponse] | None


def _error(req_id: int | str | None, code: int, message: str) -> JsonRpcResponse:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _result(req_id: int | str | None, result: object) -> JsonRpcResponse:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def notification(method: str, params: dict[str, object]) -> JsonRpcNotification:
    """Build a JSON-RPC 2.0 notification (sidecar → shell).

    The single factory every notification builder routes through, so the
    ``{"jsonrpc": "2.0", "method": ..., "params": ...}`` envelope is authored
    in exactly one place (DESIGN §5.1).
    """
    return {"jsonrpc": "2.0", "method": method, "params": params}


class _ParamError(Exception):
    """Raised inside dispatch when params fail shape validation."""


def _as_dict(params: object) -> dict[str, object]:
    if params is None:
        return {}
    if isinstance(params, dict):
        return params
    raise _ParamError("params must be an object")


# Methods whose `project_rpc.ERR_PROJECT_NOT_ACTIVE` (-32004) is remapped to
# the public `ERR_NO_ACTIVE_PROJECT` (-32011) for the shell. `project.select`
# and `project.deselect` do not call `_require_active`; everything else does.
_PROJECT_NO_ACTIVE_REMAP = frozenset(
    {
        "project.inspect",
        "project.branches",
        "project.set_target_branch",
        "project.set_seed_paths",
        "project.set_budget",
        "project.set_timelapse",
        "project.install_timelapse",
    }
)


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
    if method == "project.set_timelapse":
        timelapse_param = obj_params.get("timelapse")
        if not isinstance(timelapse_param, dict):
            raise _ParamError("project.set_timelapse requires object 'timelapse'")
        return project_rpc.set_timelapse(timelapse_param)
    if method == "project.deselect":
        return project_rpc.deselect()
    raise KeyError(method)  # pragma: no cover — guarded by HANDLERS routing


async def _close_project_handles(state: ServerState) -> None:
    """Close DB handles before switching to a different active project (§1.3)."""
    store = state.data_store
    if store is None:
        return
    state.data_store = None
    with contextlib.suppress(Exception):
        await store.close()


def _finalize_project_select(
    resolved: str, state: ServerState, req_id: int | str | None
) -> JsonRpcResponse:
    state.active_project_path = resolved
    # When the desktop launches from Finder the sidecar's cwd is "/".
    # Subprocess spawns that inherit this cwd traverse ~/Music etc. on
    # relative-path operations, triggering macOS TCC prompts (see
    # desktop-2za5). Re-anchor cwd at project select; all downstream
    # subprocess spawns will inherit it unless they pass an explicit cwd.
    with contextlib.suppress(OSError):
        os.chdir(resolved)
    try:
        inspect_result = project_rpc.inspect()
    except project_rpc.ProjectError as exc:
        return _error(req_id, exc.code, str(exc))
    return _result(req_id, {"path": resolved, "inspect": inspect_result})


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
            return _finalize_project_select(resolved, state, req_id)

        return _switch_with_close()

    return _finalize_project_select(resolved, state, req_id)


def _active_project_path(state: ServerState) -> Path:
    """Filesystem root for project-relative operations (§1.3).

    Returns the active project path when one has been selected via
    ``project.select``; falls back to the sidecar's cwd otherwise so calls
    made before a project is selected (and existing tests) still resolve.
    """
    if state.active_project_path is not None:
        return Path(state.active_project_path)
    return Path.cwd()


def _progress_notification(
    token: object,
    *,
    step: str,
    percent: int,
    message: str,
) -> JsonRpcNotification:
    return notification(
        "$/progress",
        {
            "token": token,
            "step": step,
            "percent": percent,
            "message": message,
        },
    )


# DESIGN §10.2 — the six startup phases reported by ``session.start`` so the
# desktop Screen 8 checklist can advance step-by-step. Each phase emits a
# ``running`` (percent=0) notification followed by an ``ok`` (percent=100)
# notification on the same ``step`` id. Step ids must match
# ``STARTUP_STEP_IDS`` in ``desktop/src/startupSteps.ts``.
SESSION_START_PHASES: tuple[tuple[str, str], ...] = (
    ("config_merge", "Config merged"),
    ("install_skills", "Skills installed"),
    ("init_beads", "Beads ready"),
    ("bind_ipc", "IPC endpoint bound"),
    ("start_bridge", "Dashboard bridge starting"),
    ("first_snapshot", "First state snapshot"),
)


# DESIGN §5.1 / §5.2 — drain-mode ``session.stop`` reports phase progress as
# it walks through graceful shutdown. ``hard`` mode skips the drain wait and
# emits a single completion event. Phase ids are stable so the desktop shell
# can render a step list parallel to the startup checklist.
SESSION_STOP_DRAIN_PHASES: tuple[tuple[str, str], ...] = (
    ("cancel_pending", "Cancelling queued plays"),
    ("await_inflight", "Awaiting in-flight plays"),
    ("archive_session", "Archiving session"),
    ("generate_report", "Generating ESR report"),
)


# Valid values for ``session.stop`` ``mode`` param. Default is ``drain``.
SESSION_STOP_MODES: frozenset[str] = frozenset({"drain", "hard"})


def _parse_session_stop_mode(raw_params: object) -> str:
    """Extract and validate ``mode`` from session.stop params.

    Returns the validated string ("drain" or "hard"). Default is "drain"
    when ``mode`` is absent. Raises ``_ParamError`` for unknown values so
    the caller can translate it into ``-32602 INVALID_PARAMS``.
    """
    if not isinstance(raw_params, dict):
        return "drain"
    if "mode" not in raw_params:
        return "drain"
    mode = raw_params["mode"]
    if not isinstance(mode, str) or mode not in SESSION_STOP_MODES:
        raise _ParamError(f"mode must be one of {sorted(SESSION_STOP_MODES)}, got {mode!r}")
    return mode


def _emit_session_start_progress(
    notify: Callable[[JsonRpcNotification], None],
    token: object,
) -> None:
    """Emit per-phase ``$/progress`` notifications for ``session.start``.

    Two notifications per phase — ``percent=0`` (running) and ``percent=100``
    (ok) — so the desktop ``applyProgressEvent`` helper transitions each step
    through pending → running → ok individually (DESIGN §10.2).
    """
    for step, message in SESSION_START_PHASES:
        notify(_progress_notification(token, step=step, percent=0, message=f"{message}…"))
        notify(_progress_notification(token, step=step, percent=100, message=message))


def _emit_session_stop_drain_progress(
    notify: Callable[[JsonRpcNotification], None],
    token: object,
) -> None:
    """Emit per-phase ``$/progress`` notifications for ``session.stop(mode=drain)``.

    Mirrors the start-side pattern: one ``running`` notification then one
    ``ok`` notification per phase. Hard-mode stop emits a single
    ``lifecycle`` event instead (no drain wait to report).
    """
    for step, message in SESSION_STOP_DRAIN_PHASES:
        notify(_progress_notification(token, step=step, percent=0, message=f"{message}…"))
        notify(_progress_notification(token, step=step, percent=100, message=message))


def _lifecycle_result(method: str) -> dict[str, object]:
    if method == "session.start":
        return {"status": "started"}
    return {"status": "stopped"}


def _session_state_name(state: ServerState) -> str:
    return "running" if state.session_active else "idle"


def _session_status_result(state: ServerState) -> dict[str, object]:
    return {
        "state": _session_state_name(state),
        "session_id": state.session_id,
        "started_at": state.started_at,
    }


def build_session_completed_notification(payload: dict[str, object]) -> JsonRpcNotification:
    """Build the ``session.completed`` JSON-RPC notification (DESIGN §5.2).

    Callers (typically the embedded bridge on a self-driven orchestrator exit)
    pass the same payload returned by ``session.stop`` so Screen 10 receives
    identical data on both transports.
    """
    return notification("session.completed", payload)


def build_esr_ready_notification(
    *,
    session_id: str,
    archive_path: str,
    report_path: str,
    log_path: str | None,
) -> JsonRpcNotification:
    """Build the ``$/esr_ready`` JSON-RPC notification (issue #561).

    Fires from the engine's drain loop the moment the static ESR HTML file
    has been generated, replacing the legacy ``webbrowser.open`` handoff for
    embedded (desktop) sessions. Carries the core-provided locators the
    shell needs to navigate — the richer ``session.completed`` notification
    delivers the full ESR payload immediately after.
    """
    return notification(
        "$/esr_ready",
        {
            "session_id": session_id,
            "archive_path": archive_path,
            "report_path": report_path,
            "log_path": log_path,
        },
    )


def build_sidecar_health_notification() -> JsonRpcNotification:
    return notification(
        "sidecar.health",
        {"status": "ok", "timestamp": datetime.now(UTC).isoformat()},
    )


async def _build_session_stop_response(
    req_id: int | str | None,
    raw_params: object,
    state: ServerState,
    *,
    mode: str = "drain",
) -> JsonRpcResponse:
    context = state.session_context
    if context is None:
        return _error(req_id, ERR_SESSION_ACTIVE, "no active session")
    exit_reason = "user_stop"
    exit_code = 0
    if isinstance(raw_params, dict):
        param_reason = raw_params.get("exit_reason")
        if isinstance(param_reason, str):
            exit_reason = param_reason
        param_code = raw_params.get("exit_code")
        if isinstance(param_code, int):
            exit_code = param_code

    # Drive the orchestrator through drain or hard shutdown before building
    # the ESR payload so abandoned-play and final-alignment writes land in
    # the database the collector reads from. ``orchestrator_task`` is the
    # supervised run_until_idle task scheduled by ``_start_orchestrator``.
    orch = state.orchestrator
    orch_task = state.orchestrator_task
    if orch is not None:
        # Drive run-loop teardown FIRST (drain or hard) so abandoned-play
        # and final-alignment writes land in the DB the collector will
        # read. We delay ``orch.stop()`` (which closes the DataStore as
        # part of shutdown_step:store_close) until AFTER the ESR payload
        # is built — otherwise the collector raises "Session not found"
        # against a torn-down SQLite handle and the dashboard never sees
        # the result.
        if mode == "hard":
            if orch_task is not None and not orch_task.done():
                orch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await orch_task
        else:
            with contextlib.suppress(Exception):
                orch.request_drain("session_stop_drain")  # type: ignore[attr-defined]
            if orch_task is not None and not orch_task.done():
                drain_timeout = DEFAULT_DRAIN_TIMEOUT_SECONDS
                if isinstance(raw_params, dict):
                    param = raw_params.get("drain_timeout_seconds")
                    if isinstance(param, (int, float)) and param > 0:
                        drain_timeout = float(param)
                try:
                    await asyncio.wait_for(asyncio.shield(orch_task), timeout=drain_timeout)
                except TimeoutError:
                    # Drain timed out — fall back to hard-cancel semantics.
                    orch_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await orch_task
                except (asyncio.CancelledError, Exception):
                    pass

    payload = await build_esr_payload(
        context.store,
        context.session_id,
        archive_path=context.archive_path,
        report_path=context.report_path,
        log_path=context.log_path,
        exit_reason=exit_reason,
        exit_code=exit_code,
    )

    if orch is not None:
        with contextlib.suppress(Exception):
            await orch.stop()  # type: ignore[attr-defined]
        state.orchestrator = None
        state.orchestrator_task = None
        payload["report_path"] = context.report_path
        payload["log_path"] = context.log_path
    # Stop any timelapse capture (triggers render) before tearing the bridge
    # down, and attach the rendered MP4 path so the desktop can open it.
    from agentshore.sidecar.session_lifecycle import stop_timelapse_capture

    payload["timelapse_output_path"] = await stop_timelapse_capture(state)
    state.session_active = False
    state.session_id = None
    state.started_at = None
    state.session_context = None
    state.esr_ready_report_path = None
    state.esr_ready_log_path = None
    if state.bridge is not None:
        await state.bridge.stop()
        state.bridge = None
    # Drop the agentshore.pid + info.json that the desktop wrote at
    # session.start so a stale endpoint doesn't keep showing up to
    # `agentshore stop` after the session has cleanly ended (desktop-r3o6).
    if state.active_project_path:
        from pathlib import Path as _Path

        from agentshore.session_path import cleanup_session

        with contextlib.suppress(OSError):
            cleanup_session(_Path(state.active_project_path))
    return _result(req_id, payload)


def _dispatch_archive(
    method: str,
    raw_params: object,
    *,
    req_id: int | str | None,
    is_notification: bool,
    notify: Callable[[JsonRpcNotification], None] | None,
    state: ServerState,
) -> DispatchResult:
    return _archive_response(method, raw_params, state, req_id)


async def _archive_response(
    method: str,
    raw_params: object,
    state: ServerState,
    req_id: int | str | None,
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


def _extract_path_param(params: object) -> str | None:
    """Pull a ``path`` string out of JSON-RPC params (positional or named)."""
    if isinstance(params, dict):
        value = params.get("path")
        return value if isinstance(value, str) else None
    if isinstance(params, list) and params:
        value = params[0]
        return value if isinstance(value, str) else None
    return None


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


def _dispatch_session(
    method: str,
    raw_params: object,
    *,
    req_id: int | str | None,
    is_notification: bool,
    notify: Callable[[JsonRpcNotification], None] | None,
    state: ServerState,
) -> DispatchResult:
    if raw_params is not None and not isinstance(raw_params, dict):
        return _error(req_id, INVALID_REQUEST, "params must be an object")

    # session.stop's ``mode`` field must be validated before any side effects
    # so an unknown value can be rejected cleanly (DESIGN §5.1).
    stop_mode = "drain"
    if method == "session.stop":
        try:
            stop_mode = _parse_session_stop_mode(raw_params)
        except _ParamError as exc:
            return _error(req_id, INVALID_PARAMS, str(exc))

    progress_token: object | None = None
    if isinstance(raw_params, dict) and "progress_token" in raw_params:
        progress_token = raw_params["progress_token"]

    # #5: the desktop wizard's selected seed file, forwarded to the
    # orchestrator so a desktop-launched session can take the seed bootstrap
    # path instead of silently falling back to open-start. The shell sends it
    # as ``seed_input_path`` (see desktop sessionClient); ``seed_path`` is
    # accepted as an alias for non-shell callers.
    seed_path: str | None = None
    if isinstance(raw_params, dict):
        raw_seed = raw_params.get("seed_input_path") or raw_params.get("seed_path")
        if isinstance(raw_seed, str) and raw_seed:
            seed_path = raw_seed

    # Per-session timelapse override from the desktop Start toggle. ``None``
    # leaves the decision to ``cfg.timelapse.enabled``.
    timelapse_enabled: bool | None = None
    if isinstance(raw_params, dict):
        raw_timelapse = raw_params.get("timelapse")
        if isinstance(raw_timelapse, bool):
            timelapse_enabled = raw_timelapse

    if method == "session.stop" and notify is not None and progress_token is not None:
        if stop_mode == "drain" and state.session_active:
            _emit_session_stop_drain_progress(notify, progress_token)
        else:
            notify(
                _progress_notification(
                    progress_token,
                    step="lifecycle",
                    percent=100,
                    message="session.stop complete",
                )
            )

    if is_notification:
        return None
    if method == "session.status":
        return _result(req_id, _session_status_result(state))
    if method == "session.stop" and state.session_context is not None:
        return _build_session_stop_response(req_id, raw_params, state, mode=stop_mode)

    was_active = state.session_active
    if method == "session.start":
        # Real bringup goes through session_lifecycle.run_session_start so
        # each phase can do its own validation and signal failure via
        # $/progress + a JSON-RPC error response (DESIGN §10.2). The runner is
        # async (the start_bridge phase boots an EmbeddedBridge as a supervised
        # task), so handle_request returns an awaitable for the dispatcher to
        # resolve. ``start_orchestrator=True`` only takes effect when the state
        # has an active_project_path; otherwise the legacy stub-mode bringup
        # still applies (preserves
        # test_session_start_then_status_reports_running_state).
        async def _run_session_start_async() -> JsonRpcResponse:
            try:
                outcome = await run_session_start(
                    state,
                    progress_token=progress_token,
                    notify=notify,
                    start_orchestrator=True,
                    seed_path=seed_path,
                    timelapse_enabled=timelapse_enabled,
                )
            except SessionStartError as exc:
                return _error(req_id, exc.code, str(exc))
            state.session_active = True
            state.session_id = outcome.session_id
            state.started_at = outcome.started_at
            return _result(
                req_id,
                {
                    "session_id": outcome.session_id,
                    "ipc_endpoint": outcome.ipc_endpoint,
                },
            )

        return _run_session_start_async()

    state.session_active = False
    if method == "session.stop" and not was_active:
        return _error(req_id, ERR_SESSION_ACTIVE, "no active session")
    if method == "session.stop":
        state.session_id = None
        state.started_at = None
        # Tear down a bridge that started without a SessionContext (e.g.
        # session.start ran but the orchestrator never published).
        if state.bridge is not None:

            async def _stop_with_bridge() -> JsonRpcResponse:
                if state.bridge is not None:
                    await state.bridge.stop()
                    state.bridge = None
                return _result(req_id, _lifecycle_result(method))

            return _stop_with_bridge()
        return _result(req_id, _lifecycle_result(method))
    return _result(
        req_id,
        {
            "session_id": state.session_id,
            "ipc_endpoint": state.ipc_endpoint,
        },
    )


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


def _dispatch_recents_rpc(
    method: str,
    raw_params: object,
    *,
    req_id: int | str | None,
    is_notification: bool,
    notify: Callable[[JsonRpcNotification], None] | None,
    state: ServerState,
) -> DispatchResult:
    if method == "recents.list":
        return _result(req_id, list_recents(recents_path()))

    path = _extract_path_param(raw_params)
    if path is None:
        return _error(req_id, INVALID_PARAMS, "path (string) is required")
    if method == "recents.touch":
        touch_recent(path, recents_path())
    else:
        remove_recent(path, recents_path())
    return _result(req_id, None)


def _dispatch_config_rpc(
    method: str,
    raw_params: object,
    *,
    req_id: int | str | None,
    is_notification: bool,
    notify: Callable[[JsonRpcNotification], None] | None,
    state: ServerState,
) -> DispatchResult:
    if method == "config.read":
        return _result(req_id, read_config(_active_project_path(state)))

    if not isinstance(raw_params, dict):
        return _error(req_id, INVALID_PARAMS, "params must be an object")
    patch = raw_params.get("patch")
    if not isinstance(patch, dict):
        return _error(req_id, INVALID_PARAMS, "params.patch must be a mapping")
    try:
        write_config(_active_project_path(state), patch)
    except TypeError as exc:
        return _error(req_id, INVALID_PARAMS, str(exc))
    return _result(req_id, {})


def _dispatch_identities_rpc(
    method: str,
    raw_params: object,
    *,
    req_id: int | str | None,
    is_notification: bool,
    notify: Callable[[JsonRpcNotification], None] | None,
    state: ServerState,
) -> DispatchResult:
    if method == "identities.list":
        try:
            return _result(req_id, list_identities(_active_project_path(state)))
        except OSError as exc:
            return _error(req_id, INTERNAL_ERROR, f"identities.list: {exc}")

    if method == "identities.check_keychain":
        if not isinstance(raw_params, dict):
            return _error(
                req_id, INVALID_PARAMS, "identities.check_keychain requires object params"
            )
        login = raw_params.get("login")
        if not isinstance(login, str):
            return _error(req_id, INVALID_PARAMS, "identities.check_keychain requires login")
        try:
            return _result(req_id, keychain_status(login))
        except ValueError as exc:
            return _error(req_id, INVALID_PARAMS, str(exc))

    if method == "identities.add":
        if not isinstance(raw_params, dict):
            return _error(req_id, INVALID_PARAMS, "identities.add requires object params")
        login = raw_params.get("login")
        token_source = raw_params.get("token_source")
        if not isinstance(login, str) or not isinstance(token_source, str):
            return _error(req_id, INVALID_PARAMS, "identities.add requires login and token_source")
        pat = raw_params.get("pat")
        if pat is not None and not isinstance(pat, str):
            return _error(req_id, INVALID_PARAMS, "identities.add: 'pat' must be a string")
        try:
            add_identity(_active_project_path(state), login, token_source, pat=pat or None)
        except ValueError as exc:
            return _error(req_id, INVALID_PARAMS, str(exc))
        except OSError as exc:
            return _error(req_id, INTERNAL_ERROR, f"identities.add: {exc}")
        return _result(req_id, {})

    if method == "identities.update":
        if not isinstance(raw_params, dict):
            return _error(req_id, INVALID_PARAMS, "identities.update requires object params")
        login = raw_params.get("login")
        patch = raw_params.get("patch")
        if not isinstance(login, str) or not isinstance(patch, dict):
            return _error(req_id, INVALID_PARAMS, "identities.update requires login and patch")
        try:
            update_identity(_active_project_path(state), login, patch)
        except ValueError as exc:
            return _error(req_id, INVALID_PARAMS, str(exc))
        except OSError as exc:
            return _error(req_id, INTERNAL_ERROR, f"identities.update: {exc}")
        return _result(req_id, {})

    if method == "identities.remove":
        if not isinstance(raw_params, dict):
            return _error(req_id, INVALID_PARAMS, "identities.remove requires object params")
        login = raw_params.get("login")
        if not isinstance(login, str):
            return _error(req_id, INVALID_PARAMS, "identities.remove requires login")
        try:
            remove_identity(_active_project_path(state), login)
        except ValueError as exc:
            return _error(req_id, INVALID_PARAMS, str(exc))
        except OSError as exc:
            return _error(req_id, INTERNAL_ERROR, f"identities.remove: {exc}")
        return _result(req_id, {})

    return _error(req_id, METHOD_NOT_FOUND, f"unknown method: {method}")


def _dispatch_agents_rpc(
    method: str,
    raw_params: object,
    *,
    req_id: int | str | None,
    is_notification: bool,
    notify: Callable[[JsonRpcNotification], None] | None,
    state: ServerState,
) -> DispatchResult:
    if method == "agents.list":
        try:
            return _result(req_id, list_agents(_active_project_path(state)))
        except OSError as exc:
            return _error(req_id, INTERNAL_ERROR, f"agents.list: {exc}")
    if method == "agents.detect":
        return _result(req_id, detect_available_agents())
    if method == "agents.catalog":
        return _result(req_id, agents_catalog())

    if method == "agents.configure":
        if not isinstance(raw_params, dict):
            return _error(req_id, INVALID_PARAMS, "agents.configure requires object params")
        agent_type = raw_params.get("type")
        if not isinstance(agent_type, str) or not agent_type:
            return _error(req_id, INVALID_PARAMS, "agents.configure requires string 'type'")
        patch = {k: v for k, v in raw_params.items() if k != "type"}
        try:
            configure_agent(_active_project_path(state), agent_type, patch)
        except ValueError as exc:
            return _error(req_id, INVALID_PARAMS, str(exc))
        except OSError as exc:
            return _error(req_id, INTERNAL_ERROR, f"agents.configure: {exc}")
        return _result(req_id, {})

    if method == "agents.get_spawn_limits":
        try:
            return _result(req_id, get_spawn_limits(_active_project_path(state)))
        except OSError as exc:
            return _error(req_id, INTERNAL_ERROR, f"agents.get_spawn_limits: {exc}")

    if method == "agents.set_spawn_limits":
        if not isinstance(raw_params, dict):
            return _error(req_id, INVALID_PARAMS, "agents.set_spawn_limits requires object params")
        try:
            set_spawn_limits(_active_project_path(state), dict(raw_params))
        except ValueError as exc:
            return _error(req_id, INVALID_PARAMS, str(exc))
        except OSError as exc:
            return _error(req_id, INTERNAL_ERROR, f"agents.set_spawn_limits: {exc}")
        return _result(req_id, {})

    return _error(req_id, METHOD_NOT_FOUND, f"unknown method: {method}")


def _dispatch_custom_method(
    method: str,
    payload: dict[str, object],
    *,
    req_id: int | str | None,
    is_notification: bool,
) -> DispatchResult:
    if is_notification:
        return None
    try:
        result = METHOD_HANDLERS[method](payload)
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


# Uniform signature shared by every registered dispatcher. A handler receives
# the method name, the raw ``params`` value, and the request envelope context;
# it returns a response, an awaitable response, or ``None`` for "no reply".
RouteHandler = Callable[..., DispatchResult]


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


# Exact-match dispatch table. Prefix families (``identities.*``, ``agents.*``)
# fan out from one entry each via _ROUTE_GROUPS below.
HANDLERS: dict[str, Route] = {
    "app.handshake": Route(_dispatch_app_handshake),
    "session.start": Route(_dispatch_session, notify_ok=True),
    "session.status": Route(_dispatch_session, notify_ok=True),
    "session.stop": Route(_dispatch_session, notify_ok=True),
    "project.select": Route(_dispatch_project_rpc),
    "project.inspect": Route(_dispatch_project_rpc),
    "project.branches": Route(_dispatch_project_rpc),
    "project.set_target_branch": Route(_dispatch_project_rpc),
    "project.set_seed_paths": Route(_dispatch_project_rpc),
    "project.set_budget": Route(_dispatch_project_rpc),
    "project.set_timelapse": Route(_dispatch_project_rpc),
    "project.install_timelapse": Route(_dispatch_project_rpc),
    "project.deselect": Route(_dispatch_project_rpc),
    "archive.list": Route(_dispatch_archive),
    "archive.fetch_report": Route(_dispatch_archive),
    "archive.fetch_logs": Route(_dispatch_archive),
    "recents.list": Route(_dispatch_recents_rpc),
    "recents.touch": Route(_dispatch_recents_rpc),
    "recents.remove": Route(_dispatch_recents_rpc),
    "config.read": Route(_dispatch_config_rpc),
    "config.write": Route(_dispatch_config_rpc),
}

# Prefix-matched dispatch groups, tried after the exact table. Each family
# keeps its single fan-out function and resolves the concrete method itself.
_ROUTE_GROUPS: tuple[tuple[str, Route], ...] = (
    ("identities.", Route(_dispatch_identities_rpc)),
    ("agents.", Route(_dispatch_agents_rpc)),
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
) -> DispatchResult:
    """Dispatch a single parsed request payload.

    Returns ``None`` for notifications (no ``id``); per JSON-RPC 2.0 these
    receive no response. Methods that need async work (e.g. ``session.stop``
    or the ``archive.*`` family) return an awaitable that yields the final
    response — the calling stdio loops await before serialising.
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
    state = state or ServerState()

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
            state=state,
        )

    if method in METHOD_HANDLERS:
        return _dispatch_custom_method(
            method, payload, req_id=req_id, is_notification=is_notification
        )

    if is_notification:
        return None
    return _error(req_id, METHOD_NOT_FOUND, f"unknown method: {method}")


def _cancel_request_id(payload: object) -> int | str | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("method") != "$/cancelRequest":
        return None
    params = payload.get("params")
    if not isinstance(params, dict):
        return None
    req_id = params.get("id")
    if isinstance(req_id, (int, str)):
        return req_id
    return None


def _reader_loop(
    stdin: IO[str], loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[str | None]
) -> None:
    try:
        for line in stdin:
            loop.call_soon_threadsafe(queue.put_nowait, line)
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, None)


DEFAULT_HEALTH_INTERVAL_SECONDS: float = 30.0
"""Default cadence for ``sidecar.health`` liveness pings (DESIGN §5.1)."""


async def _serve_async(
    stdin: IO[str],
    stdout: IO[str],
    *,
    health_interval_seconds: float = DEFAULT_HEALTH_INTERVAL_SECONDS,
) -> None:
    """Read line-framed JSON-RPC from ``stdin``, write responses to ``stdout``.

    The single stdio serve loop (DESIGN §1.2). Carries the request-cancellation
    machinery (``$/cancelRequest``, in-flight tracking, background drain of
    cancelled handlers) and fires ``sidecar.health`` JSON-RPC notifications on a
    fixed interval (DESIGN §5.1) so the Tauri shell can detect a stalled
    sidecar. Pass ``health_interval_seconds <= 0`` to disable the heartbeat
    (used by tests that drive a quick request and then close stdin). Returns
    when ``stdin`` reaches EOF and any in-flight async requests have drained.
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _handle_loop_exception(
        _loop: asyncio.AbstractEventLoop, context: dict[str, object]
    ) -> None:
        # Global backstop for orphaned-task exceptions the per-task callbacks
        # don't cover. structlog is already routed to stderr by
        # _configure_sidecar_logging (stdout is reserved for JSON-RPC), and
        # dict_tracebacks renders the stack when an exception is present.
        import structlog

        exc = context.get("exception")
        structlog.get_logger(__name__).error(
            "sidecar_unhandled_loop_exception",
            message=context.get("message"),
            exc_info=exc if isinstance(exc, BaseException) else None,
        )

    loop.set_exception_handler(_handle_loop_exception)
    reader = threading.Thread(target=_reader_loop, args=(stdin, loop, queue), daemon=True)
    reader.start()
    in_flight: dict[int | str, asyncio.Task[None]] = {}
    cancelled_ids: set[int | str] = set()
    # Background tasks that drain cancelled handlers so the serve loop never
    # blocks on a slow `finally` and so a non-CancelledError from handler
    # cleanup cannot escape and kill the loop. Held to prevent premature GC.
    drain_tasks: set[asyncio.Task[None]] = set()
    state = ServerState()

    def _schedule_drain(task: asyncio.Task[None], req_id: int | str) -> None:
        async def _drain() -> None:
            try:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            finally:
                # Keep `cancelled_ids` sticky until the handler finishes so
                # `run_request` cannot double-write the cancellation reply.
                cancelled_ids.discard(req_id)

        drain = asyncio.create_task(_drain())
        drain_tasks.add(drain)
        drain.add_done_callback(drain_tasks.discard)

    def _emit(obj: object) -> None:
        # One framing site for every stdout write (H2/H3): json_safe +
        # allow_nan=False so a non-finite float in a response can never emit
        # invalid JSON, then flush so the shell sees the line immediately.
        stdout.write(frame(obj))
        stdout.flush()

    def _write_notification(notification: JsonRpcNotification) -> None:
        _emit(notification)

    async def _health_emitter() -> None:
        while True:
            await asyncio.sleep(health_interval_seconds)
            _emit(build_sidecar_health_notification())

    async def run_request(req_id: int | str, response: Awaitable[JsonRpcResponse]) -> None:
        emitted = False
        try:
            rpc_response = await response
            # If a cancel arrived while this request was in flight, the
            # cancel handler already wrote the cancellation reply — never
            # double-write, even if the handler's `finally` masked the
            # CancelledError with a RuntimeError that the inner
            # `_await_handler` then turned into a normal error response.
            if req_id not in cancelled_ids:
                _emit(rpc_response)
            emitted = True
        except asyncio.CancelledError:
            if req_id not in cancelled_ids:
                _emit(_error(req_id, REQUEST_CANCELLED, "request cancelled"))
                emitted = True
            raise
        finally:
            if not emitted and req_id not in cancelled_ids:
                _emit(_error(req_id, REQUEST_CANCELLED, "request cancelled"))
            in_flight.pop(req_id, None)

    health_task: asyncio.Task[None] | None = None
    if health_interval_seconds > 0:
        health_task = asyncio.create_task(_health_emitter())

    while True:
        line = await queue.get()
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        try:
            payload_obj: object = json.loads(line)
        except json.JSONDecodeError:
            _emit(_error(None, PARSE_ERROR, "invalid JSON"))
            continue

        cancel_id = _cancel_request_id(payload_obj)
        if cancel_id is not None:
            task = in_flight.get(cancel_id)
            if task is not None and not task.done():
                cancelled_ids.add(cancel_id)
                task.cancel()
                # Write the cancellation reply immediately — never block the
                # serve loop on the cancelled handler's cleanup (desktop-y4g).
                _emit(_error(cancel_id, REQUEST_CANCELLED, "request cancelled"))
                # Drain the cancelled task in the background so a non-
                # CancelledError raised by its `finally` cleanup is swallowed
                # rather than killing the loop (desktop-6hd). The drain
                # discards `cancel_id` from `cancelled_ids` only after the
                # task finishes — keeps `run_request` from double-writing.
                _schedule_drain(task, cancel_id)
            continue

        # Defensive: an unhandled exception in handle_request used to escape
        # all the way out of _serve_async, killing the dispatch loop and
        # leaving the desktop shell's pending JSON-RPC promise to hang
        # forever (the "freeze" symptom on Identities Add when add_identity
        # raised PermissionError writing to Path.cwd()). Trap any unexpected
        # error and convert it to an INTERNAL_ERROR response so the loop
        # survives and the caller sees a real failure.
        try:
            response = handle_request(payload_obj, notify=_write_notification, state=state)
        except Exception as exc:  # noqa: BLE001 — last-resort guard
            req_id_for_err: int | str | None = None
            if isinstance(payload_obj, dict):
                pid = payload_obj.get("id")
                if isinstance(pid, (int, str)):
                    req_id_for_err = pid
            response = _error(req_id_for_err, INTERNAL_ERROR, f"unhandled handler error: {exc}")
        if inspect.isawaitable(response):
            req_id_obj: int | str | None = None
            if isinstance(payload_obj, dict):
                payload_id = payload_obj.get("id")
                if "id" in payload_obj and isinstance(payload_id, (int, str)):
                    req_id_obj = payload_id
            if req_id_obj is None:
                response = await response
            else:
                task = asyncio.create_task(run_request(req_id_obj, response))
                in_flight[req_id_obj] = task
                continue
        if response is None:
            continue
        _emit(response)

    if health_task is not None:
        health_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await health_task
    if in_flight:
        await asyncio.gather(*in_flight.values(), return_exceptions=True)
    if drain_tasks:
        await asyncio.gather(*drain_tasks, return_exceptions=True)


def serve(
    stdin: IO[str],
    stdout: IO[str],
    *,
    health_interval_seconds: float = DEFAULT_HEALTH_INTERVAL_SECONDS,
) -> None:
    """Read line-framed JSON-RPC from ``stdin``, write responses to ``stdout``.

    Synchronous wrapper around :func:`_serve_async`. Returns when ``stdin``
    reaches EOF and any in-flight async requests have drained.
    """
    asyncio.run(_serve_async(stdin, stdout, health_interval_seconds=health_interval_seconds))


def _configure_sidecar_logging() -> None:
    """Route structlog output to stderr.

    The sidecar reserves stdout for line-framed JSON-RPC; any structlog or
    stdlib log line that lands on stdout corrupts the protocol and trips
    every JSON-RPC client with ``json.JSONDecodeError: Extra data``. Default
    structlog writes ConsoleRenderer to stdout, so the entry point has to
    configure logging before any module fires its first log line.
    """
    from agentshore.logging import setup_logging

    setup_logging("info")


def run() -> None:
    """Sync entry point. Wraps :func:`serve` against the real stdio streams."""
    _configure_sidecar_logging()
    serve(sys.stdin, sys.stdout)
