"""Stdio serve loop and synchronous entry points.

``_serve_async`` / ``serve`` / ``run`` live here.  Everything that touches
``stdin`` / ``stdout`` is in this module; the routing and protocol layers are
in :mod:`.router` and :mod:`.protocol`.

Monkeypatch note: ``_reader_loop``, ``_serve_async``, ``EOF_IN_FLIGHT_GRACE_SECONDS``,
``EOF_TEARDOWN_DEADLINE_SECONDS``, and ``ServerState`` are all referenced by
tests that patch them on ``agentshore.sidecar.server``.  The thin re-export
shim in ``server.py`` re-exports all of them so patches land on that module
object; the serve loop then calls them as ``server``-module globals so the
patches are seen.  This module owns the *definitions*; ``server.py`` owns the
*patchable names*.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import sys
import threading
import time
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from agentshore.sidecar.rpc.protocol import MethodHandler

from agentshore.ipc.wire import frame
from agentshore.platform_compat import ensure_windows_event_loop_policy, force_utf8_stdio
from agentshore.sidecar.rpc.protocol import (
    INTERNAL_ERROR,
    PARSE_ERROR,
    REQUEST_CANCELLED,
    JsonRpcNotification,
    JsonRpcResponse,
    ServerState,
    _error,
)
from agentshore.sidecar.rpc.router import handle_request as _handle_request
from agentshore.sidecar.rpc.router_helpers import build_sidecar_health_notification

DEFAULT_HEALTH_INTERVAL_SECONDS: float = 30.0
"""Default cadence for ``sidecar.health`` liveness pings (DESIGN §5.1)."""

EOF_IN_FLIGHT_GRACE_SECONDS: float = 5.0
"""How long after stdin EOF in-flight handlers may finish naturally.

Long enough for quick requests to drain and emit their responses (the
documented "request then close stdin" pattern tests rely on); short enough
that a wedged handler — e.g. a graceful ``session.stop`` whose drain will
never finish — cannot keep the sidecar alive after the shell is gone (#155).
"""

EOF_TEARDOWN_DEADLINE_SECONDS: float = 10.0
"""Hard bound on post-EOF teardown (cancelled handlers + orchestrator task).

stdin EOF means the desktop shell exited or the pipe broke; "sidecar death ==
orchestrator death" (DESIGN §1.2) only holds if the serve loop is guaranteed
to return promptly after EOF, so every post-EOF wait is bounded (#155).
"""


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


async def _serve_async(
    stdin: IO[str],
    stdout: IO[str],
    *,
    health_interval_seconds: float = DEFAULT_HEALTH_INTERVAL_SECONDS,
    method_handlers: dict[str, MethodHandler] | None = None,
) -> None:
    """Read line-framed JSON-RPC from ``stdin``, write responses to ``stdout``.

    The single stdio serve loop (DESIGN §1.2). Carries the request-cancellation
    machinery (``$/cancelRequest``, in-flight tracking, background drain of
    cancelled handlers) and fires ``sidecar.health`` JSON-RPC notifications on a
    fixed interval (DESIGN §5.1) so the Tauri shell can detect a stalled
    sidecar. Pass ``health_interval_seconds <= 0`` to disable the heartbeat
    (used by tests that drive a quick request and then close stdin). Returns
    when ``stdin`` reaches EOF and in-flight async requests have drained —
    bounded by ``EOF_IN_FLIGHT_GRACE_SECONDS`` / ``EOF_TEARDOWN_DEADLINE_SECONDS``
    and with the orchestrator task hard-cancelled, so EOF always means a
    prompt exit even mid-drain (#155).
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
        except Exception as exc:  # noqa: BLE001 — real failures → INTERNAL_ERROR, not -32800
            if req_id not in cancelled_ids:
                _emit(_error(req_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"))
                emitted = True
        finally:
            if not emitted and req_id not in cancelled_ids:
                _emit(_error(req_id, REQUEST_CANCELLED, "request cancelled"))
            in_flight.pop(req_id, None)

    health_task: asyncio.Task[None] | None = None
    if health_interval_seconds > 0:
        health_task = asyncio.create_task(_health_emitter())

    _mh = method_handlers if method_handlers is not None else {}

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
            response = _handle_request(
                payload_obj, notify=_write_notification, state=state, method_handlers=_mh
            )
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

    # stdin EOF: the desktop shell is gone (window closed, host exited, or the
    # pipe broke). EOF is always HARD-stop semantics — nobody is left to
    # receive a graceful drain's result, so never wait for one (#155).
    #
    # 1. Cancel the supervised orchestrator run loop immediately. An in-flight
    #    graceful ``session.stop`` awaits it through ``asyncio.shield``, so
    #    cancelling only the handler would leave the orchestrator running
    #    headless; the task itself must be cancelled.
    # 2. Give in-flight handlers a short grace to finish naturally (quick
    #    requests still drain and emit, as documented), then cancel stragglers.
    # 3. Bound the final wait so the serve loop is guaranteed to return and
    #    ``asyncio.run`` can tear the loop down — the sidecar process must
    #    always exit promptly once the pipe closes.
    orch_task = state.orchestrator_task
    if orch_task is not None and not orch_task.done():
        orch_task.cancel()
    if in_flight:
        _done, pending = await asyncio.wait(
            set(in_flight.values()), timeout=EOF_IN_FLIGHT_GRACE_SECONDS
        )
        for task in pending:
            task.cancel()
    remaining: list[asyncio.Task[None]] = [
        task
        for task in (orch_task, *in_flight.values(), *drain_tasks)
        if task is not None and not task.done()
    ]
    if remaining:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*remaining, return_exceptions=True),
                timeout=EOF_TEARDOWN_DEADLINE_SECONDS,
            )


def serve(
    stdin: IO[str],
    stdout: IO[str],
    *,
    health_interval_seconds: float = DEFAULT_HEALTH_INTERVAL_SECONDS,
) -> None:
    """Read line-framed JSON-RPC from ``stdin``, write responses to ``stdout``.

    Synchronous wrapper around :func:`_serve_async`. Returns when ``stdin``
    reaches EOF and in-flight async requests have drained (bounded — see
    :func:`_serve_async`).
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


def _preload_native_libraries() -> None:
    """Import numpy/torch up front, while the sidecar is still single-threaded.

    Windows loader-lock deadlock guard. numpy (OpenBLAS) and torch spawn native
    worker threads during their C-extension initialization, and that runs under
    the OS loader lock held by the importing thread. When the import happens
    later instead — the ``from agentshore.core import Orchestrator`` inside
    ``session.start`` runs on the asyncio event loop while the ``project.inspect``
    probe pool and the ``asyncio.to_thread`` executor already have live threads —
    those native libs' freshly spawned threads block in ``DllMain(THREAD_ATTACH)``
    waiting for the loader lock the importing thread still holds, and the sidecar
    wedges at 0 CPU forever (observed live: a 9-minute hang in
    ``numpy/_core/multiarray`` ``create_module``; reproduced deterministically
    with 6 live worker threads, while the same import is 0.33s single-threaded).

    Importing here — before :func:`serve` starts the stdin reader thread, the
    event loop, or any executor — maps both native DLLs once in a single-threaded
    context (~3s); every later import is then a ``sys.modules`` no-op, so
    ``session.start`` no longer pays (or deadlocks on) the native load. Off-loading
    the import to a worker thread does NOT help: the deadlock fires whenever the
    import runs while *other* threads are alive, regardless of which thread does
    it. POSIX ``dlopen`` has no equivalent loader-lock/thread-attach hazard, so
    this is win32-only — it also keeps torch's import cost off macOS/Linux boots.
    """
    if sys.platform != "win32":
        return
    import structlog

    log = structlog.get_logger()
    started = time.perf_counter()
    try:
        import numpy  # noqa: F401
        import torch  # noqa: F401
    except Exception as exc:  # pragma: no cover - a failed import is fatal regardless
        log.warning("sidecar_preload_native_libs_failed", error=str(exc))
        return
    log.info(
        "sidecar_preload_native_libs",
        elapsed_seconds=round(time.perf_counter() - started, 2),
    )


def run() -> None:
    """Sync entry point. Wraps :func:`serve` against the real stdio streams."""
    force_utf8_stdio()
    ensure_windows_event_loop_policy()
    _configure_sidecar_logging()
    # Map numpy/torch native DLLs while single-threaded — see docstring. Must run
    # before serve() spawns the reader thread / event loop / executors, or the
    # later session.start import deadlocks on the Windows loader lock.
    _preload_native_libraries()
    serve(sys.stdin, sys.stdout)
