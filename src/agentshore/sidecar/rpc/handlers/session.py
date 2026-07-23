"""Handler for the ``session.*`` method family.

Covers ``session.start``, ``session.stop``, ``session.status``.
The ``session.{get,set}_budget`` sub-family lives in :mod:`.session_budget`
because it operates against the live orchestrator instance rather than
lifecycle state.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from agentshore.sidecar.esr import build_esr_payload
from agentshore.sidecar.rpc.protocol import (
    ERR_SESSION_ACTIVE,
    INVALID_PARAMS,
    INVALID_REQUEST,
    DispatchResult,
    JsonRpcNotification,
    JsonRpcResponse,
    ServerState,
    _error,
    _ParamError,
    _result,
)
from agentshore.sidecar.rpc.router_helpers import (
    SESSION_START_PHASES,
    SESSION_STOP_DRAIN_PHASES,
    _progress_notification,
)
from agentshore.sidecar.session_lifecycle import (
    SessionStartError,
    run_session_start,
    stop_orchestrator_tracked,
)

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

    # orchestrator_task is the supervised run_until_idle task from _start_orchestrator.
    orch = state.orchestrator
    orch_task = state.orchestrator_task
    if orch is not None:
        # Run-loop teardown FIRST so abandoned-play and final-alignment writes
        # land in the DB. orch.stop() (which closes the DataStore) is deferred
        # until after the ESR payload is built — else the collector hits
        # "Session not found" on a torn-down SQLite handle.
        if mode == "hard":
            if orch_task is not None and not orch_task.done():
                orch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await orch_task
        else:
            with contextlib.suppress(Exception):
                orch.request_drain("session_stop_drain")
            if orch_task is not None and not orch_task.done():
                # No deadline by default: in-flight plays finish on their own.
                # Bounded wait via drain_timeout_seconds; immediate kill is mode="hard".
                drain_timeout: float | None = None
                if isinstance(raw_params, dict):
                    param = raw_params.get("drain_timeout_seconds")
                    if isinstance(param, (int, float)) and param > 0:
                        drain_timeout = float(param)
                try:
                    if drain_timeout is None:
                        await asyncio.shield(orch_task)
                    else:
                        await asyncio.wait_for(asyncio.shield(orch_task), timeout=drain_timeout)
                except TimeoutError:
                    # Drain timed out — fall back to hard-cancel.
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
        # Track store teardown (#283) so a racing session.start waits for
        # agentshore.db's close (snapshot + os.replace under the writer lock).
        # Null orchestrator/orchestrator_task here, ahead of the shared
        # _clear_session_state call below, so a concurrent RPC arriving
        # during the (possibly slow) timelapse-render wait never dials an
        # orchestrator whose store is already closed.
        with contextlib.suppress(Exception):
            await stop_orchestrator_tracked(state, orch)
        state.orchestrator = None
        state.orchestrator_task = None
        payload["report_path"] = context.report_path
        payload["log_path"] = context.log_path
    # Stop timelapse capture (triggers render) before bridge teardown; attach
    # the rendered MP4 path for the desktop to open.
    from agentshore.sidecar.session_lifecycle import _clear_session_state, stop_timelapse_capture

    payload["timelapse_output_path"] = await stop_timelapse_capture(state)
    _clear_session_state(state)
    if state.bridge is not None:
        await state.bridge.stop()
        state.bridge = None
    # Drop the agentshore.pid + info.json the desktop wrote at session.start so a
    # stale endpoint doesn't linger for `agentshore stop` (desktop-r3o6).
    if state.active_project_path:
        from pathlib import Path as _Path

        from agentshore.session_path import cleanup_session

        with contextlib.suppress(OSError):
            cleanup_session(_Path(state.active_project_path))
    return _result(req_id, payload)


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

    # Validate session.stop's ``mode`` before any side effects (DESIGN §5.1).
    stop_mode = "drain"
    if method == "session.stop":
        try:
            stop_mode = _parse_session_stop_mode(raw_params)
        except _ParamError as exc:
            return _error(req_id, INVALID_PARAMS, str(exc))

    progress_token: object | None = None
    if isinstance(raw_params, dict) and "progress_token" in raw_params:
        progress_token = raw_params["progress_token"]

    # #5: desktop wizard's seed file → orchestrator seed-bootstrap path (else
    # silent open-start). Shell sends seed_input_path; seed_path is an alias.
    seed_path: str | None = None
    if isinstance(raw_params, dict):
        raw_seed = raw_params.get("seed_input_path") or raw_params.get("seed_path")
        if isinstance(raw_seed, str) and raw_seed:
            seed_path = raw_seed

    # Per-session timelapse override from the desktop Start toggle; None defers
    # to cfg.timelapse.enabled.
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
        # Real bringup via run_session_start so each phase validates and signals
        # failure via $/progress + JSON-RPC error (DESIGN §10.2). Async (start_bridge
        # boots an EmbeddedBridge task), so we return an awaitable. start_orchestrator
        # only takes effect with an active_project_path; else legacy stub-mode bringup.
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
        # Tear down a bridge started without a SessionContext (start ran but the
        # orchestrator never published).
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
