"""Process lifecycle control and the synchronous IPC client for a session.

Split out of :mod:`agentshore.session_path` (which keeps genuine path/endpoint
resolution): this module owns everything that touches a *live* orchestrator
process — PID liveness probes, SIGTERM/SIGKILL escalation for the
orchestrator and dashboard subprocesses, and the short-lived blocking socket
client the CLI uses to send ``drain``/``reload_config``/``add_budget``
commands and read synchronous replies.

``discover_ipc_endpoint``/``discover_socket`` live here too rather than in
``session_path``: their stale-endpoint detection depends on the same PID
liveness probe (``_process_alive``) that drives ``is_session_running``, so
keeping them together avoids a liveness-check import cycle between the two
modules.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess  # nosec B404
import sys
import time
from pathlib import Path

from agentshore.logging import get_logger
from agentshore.session_path import (
    IpcEndpoint,
    cleanup_session,
    ipc_endpoint_from_json,
    is_unix_socket_path,
    read_dashboard_pid,
    read_pid,
    read_session_info,
    session_socket_path,
)

_logger = get_logger(__name__)

# Win32 constants for the no-side-effect liveness probe (_process_alive_windows).
_WIN_SYNCHRONIZE = 0x00100000
_WIN_WAIT_TIMEOUT = 0x00000102
_WIN_ERROR_INVALID_PARAMETER = 87

# Time we'll wait for SIGTERM to land before escalating to SIGKILL.
_STOP_GRACE_SECONDS = 60.0
_STOP_POLL_INTERVAL = 0.1
_DASHBOARD_STOP_GRACE_SECONDS = 5.0


def discover_ipc_endpoint(project_path: Path) -> IpcEndpoint | None:
    """Find the live IPC endpoint for a running session, or None if not found.

    An endpoint is considered "live" only if the recorded session PID (if any)
    is still running. If the endpoint exists but the orchestrator PID is dead,
    this calls :func:`agentshore.session_path.cleanup_session` and returns
    ``None`` — matching the stale-endpoint detection requirement so callers can
    report "no running session" cleanly.

    When an ``info.json`` sidecar records an explicit ``socket`` path (e.g.
    when ``agentshore start --socket PATH`` was used), that path is returned in
    preference to the well-known location.
    """
    pid = read_pid(project_path)
    if pid is not None and not _process_alive(pid):
        cleanup_session(project_path)
        return None

    info = read_session_info(project_path)
    if info is not None:
        endpoint = ipc_endpoint_from_json(info.get("ipc"))
        if endpoint is not None:
            if endpoint.kind == "tcp":
                return endpoint
            if endpoint.path is not None and is_unix_socket_path(endpoint.path):
                return endpoint
        recorded = info.get("socket")
        if isinstance(recorded, str):
            recorded_path = Path(recorded)
            if is_unix_socket_path(recorded_path):
                return IpcEndpoint.unix(recorded_path)

    path = session_socket_path(project_path)
    if is_unix_socket_path(path):
        return IpcEndpoint.unix(path)
    return None


def discover_socket(project_path: Path) -> Path | None:
    """Backward-compatible helper that returns only Unix socket paths."""
    endpoint = discover_ipc_endpoint(project_path)
    if endpoint is None or endpoint.kind != "unix":
        return None
    return endpoint.path


def is_session_running(project_path: Path) -> bool:
    """Check whether the recorded session PID still exists.

    Considers the orchestrator authoritative — the dashboard alone is not
    "a session." If the orchestrator is gone, we proactively clean up any
    stale dashboard PID and IPC files left behind.
    """
    pid = read_pid(project_path)
    if pid is None:
        if read_dashboard_pid(project_path) is not None:
            stop_dashboard_process(project_path)
            cleanup_session(project_path)
        return False
    if _process_alive(pid):
        return True
    stop_dashboard_process(project_path)
    cleanup_session(project_path)
    return False


def _process_alive_windows(pid: int) -> bool:
    """Liveness probe for Windows that has no side effects.

    ``os.kill(pid, 0)`` is **not** a null-signal probe on Windows: signal ``0``
    is ``signal.CTRL_C_EVENT``, so CPython routes the call to
    ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)``. That delivers a Ctrl+C to a
    console process group and its success/failure depends on whether the caller
    shares the target's console — not on whether the process is alive. For a
    detached, no-window orchestrator a fresh ``agentshore stop`` shares no
    console with it, so the call raises and the old probe wrongly reported the
    live session as dead (it then cleaned up the PID/sidecar, so ``stop`` said
    "no running session" and ``dashboard``/``add_budget`` lost the endpoint).

    Probe via the Win32 API instead: open the process and wait zero seconds on
    its handle. ``WAIT_TIMEOUT`` means the process object is unsignalled — still
    running; ``WAIT_OBJECT_0`` means it has exited. If ``OpenProcess`` fails with
    ``ERROR_INVALID_PARAMETER`` the PID does not exist (dead); any other failure
    (e.g. access denied) means the process exists but we lack rights — treat as
    alive so we never wrongly discard a running session.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)

    handle = kernel32.OpenProcess(_WIN_SYNCHRONIZE, False, pid)
    if not handle:
        last_err: int = ctypes.get_last_error()  # type: ignore[attr-defined]
        return last_err != _WIN_ERROR_INVALID_PARAMETER
    try:
        return bool(kernel32.WaitForSingleObject(handle, 0) == _WIN_WAIT_TIMEOUT)
    finally:
        kernel32.CloseHandle(handle)


def _process_alive(pid: int) -> bool:
    if sys.platform.startswith("win"):
        return _process_alive_windows(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _signal_group(pid: int, sig: int) -> None:
    """Signal the process's group, falling back to the bare PID.

    A desktop-spawned sidecar may not be a process-group leader (the
    launcher didn't call setsid / start_new_session), so ``killpg(pid)``
    raises ``ProcessLookupError`` even though the process is alive.
    Previously that early-returned and the sidecar was never signalled —
    ``agentshore stop`` reported success while the orchestrator kept
    running (#31). Fall back to ``os.kill(pid)`` whenever the group signal
    doesn't land.
    """
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        try:
            killpg(pid, sig)
            return
        except OSError:
            # No group with this pgid (non-leader pid) or not permitted —
            # signal the individual process instead.
            pass
    with contextlib.suppress(OSError):
        os.kill(pid, sig)


def _terminate_process_tree(pid: int, *, force: bool) -> None:
    if sys.platform.startswith("win"):
        args = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            args.append("/F")
        try:
            completed = subprocess.run(  # nosec B603
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            # taskkill could not be launched at all — never raise from teardown.
            return
        # taskkill returns non-zero for an already-dead PID (e.g. 128 "process
        # not found") as well as for genuine privilege failures. Only the
        # latter is worth a warning — if the process is actually gone, teardown
        # succeeded regardless of the exit code.
        if completed.returncode != 0 and _process_alive(pid):
            _logger.warning(
                "taskkill_failed",
                pid=pid,
                returncode=completed.returncode,
                force=force,
            )
        return

    sig = getattr(signal, "SIGKILL", signal.SIGTERM) if force else signal.SIGTERM
    _signal_group(pid, sig)


def _connect_ipc(endpoint: IpcEndpoint, sock: socket.socket) -> bool:
    """Connect *sock* to *endpoint*. Returns False for an unaddressable unix path."""
    if endpoint.kind == "unix":
        if endpoint.path is None:
            return False
        sock.connect(str(endpoint.path))
    else:
        sock.connect((endpoint.host, endpoint.port))
    return True


def _send_ipc_command(
    project_path: Path,
    cmd: dict[str, object],
    *,
    timeout: float = 5.0,
    no_endpoint: str,
) -> str:
    """Fire-and-forget a single newline-framed JSON command to the orchestrator.

    Shared transport for the fire-and-forget control commands (``drain`` and
    ``reload_config``): discover the endpoint, connect, send one framed command,
    and map transport outcomes to a status string. Returns *no_endpoint* when no
    endpoint is discoverable, ``"sent"`` on success, ``"timeout"`` /
    ``"error"`` on failure.
    """
    from agentshore.ipc.wire import frame

    endpoint = discover_ipc_endpoint(project_path)
    if endpoint is None:
        return no_endpoint

    try:
        family = socket.AF_UNIX if endpoint.kind == "unix" else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            if not _connect_ipc(endpoint, sock):
                return no_endpoint
            sock.sendall(frame(cmd).encode())
        return "sent"
    except TimeoutError:
        return "timeout"
    except (AttributeError, OSError):
        return "error"


def request_drain(
    project_path: Path,
    *,
    end_session_report: bool = False,
    open_report: bool = True,
) -> str:
    """Send a graceful drain request to the running orchestrator over IPC.

    Returns a status string.
    """
    return _send_ipc_command(
        project_path,
        {
            "command": "drain",
            "reason": "cli_request",
            "end_session_report": end_session_report,
            "open_report": open_report,
        },
        no_endpoint="fallback_hard",
    )


def request_reload_config(project_path: Path) -> str:
    """Send a reload_config request to the running orchestrator over IPC.

    Instructs the orchestrator to re-read ``agentshore.yaml`` and apply the
    updated configuration atomically.  This is the cross-platform equivalent of
    ``kill -HUP`` for callers on Windows where SIGHUP does not exist.

    Returns a status string: ``"sent"``, ``"timeout"``, ``"error"``, or
    ``"fallback_hard"`` (no IPC endpoint found).
    """
    return _send_ipc_command(
        project_path, {"command": "reload_config"}, no_endpoint="fallback_hard"
    )


def request_add_budget(
    project_path: Path,
    *,
    delta_usd: float | None,
    delta_minutes: int | None,
) -> str | dict[str, object]:
    """Additively top up / extend the live session budget over IPC.

    Sends ``add_budget`` and reads the orchestrator's synchronous reply on the
    same connection (``{"type": "add_budget_ok", ...applied caps...}`` on
    success, ``{"type": "error", ...}`` on rejection).

    Returns ``"no_session"`` when no IPC endpoint is discoverable,
    ``"timeout"`` when the server does not reply within 12 s,
    ``"error"`` on any other transport or protocol failure,
    ``"rejected:<msg>"`` when the orchestrator rejects the request, or
    the applied-caps dict on success.
    """
    from agentshore.ipc.wire import frame

    endpoint = discover_ipc_endpoint(project_path)
    if endpoint is None:
        return "no_session"

    add_cmd: dict[str, object] = {"command": "add_budget"}
    if delta_usd is not None:
        add_cmd["delta_usd"] = delta_usd
    if delta_minutes is not None:
        add_cmd["delta_minutes"] = delta_minutes

    def _read_line(sock: socket.socket, buf: bytes) -> tuple[bytes | None, bytes]:
        while b"\n" not in buf:
            try:
                chunk = sock.recv(65536)
            except OSError:
                return None, buf
            if not chunk:
                return None, buf
            buf += chunk
        line, _, rest = buf.partition(b"\n")
        return line, rest

    try:
        family = socket.AF_UNIX if endpoint.kind == "unix" else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(12.0)
            if not _connect_ipc(endpoint, sock):
                return "no_session"

            sock.sendall(frame(add_cmd).encode())

            buf = b""
            line, _buf = _read_line(sock, buf)
    except TimeoutError:
        return "timeout"
    except (AttributeError, OSError):
        return "error"

    if line is None or not line.strip():
        return "error"
    try:
        msg = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "error"
    if not isinstance(msg, dict):
        return "error"
    msg_type = msg.get("type")
    if msg_type == "add_budget_ok":
        return {k: v for k, v in msg.items() if k != "type"}
    if msg_type == "error":
        return f"rejected:{msg.get('error', 'unknown')}"
    return "error"


def stop_dashboard_process(project_path: Path, *, pid: int | None = None) -> bool:
    """Terminate the recorded dashboard bridge process, if one exists.

    ``pid`` lets a caller terminate a specific, already-validated process
    instead of re-reading ``dashboard.pid``. The supervisor path writes the new
    child's pid into that file concurrently, so a caller superseding a *prior*
    dashboard must pin the pid it checked to avoid killing the freshly-written
    one (itself).
    """
    if pid is None:
        pid = read_dashboard_pid(project_path)
    if pid is None:
        return False

    _terminate_process_tree(pid, force=False)

    deadline = time.monotonic() + _DASHBOARD_STOP_GRACE_SECONDS
    while _process_alive(pid) and time.monotonic() < deadline:
        time.sleep(_STOP_POLL_INTERVAL)

    if _process_alive(pid):
        _terminate_process_tree(pid, force=True)

    return True


def hard_stop_session(project_path: Path) -> bool:
    """Forcibly stop the orchestrator and dashboard subprocesses for a project session.

    Sends SIGTERM to each recorded PID's process group, waits up to the
    grace period, then escalates to SIGKILL. Cleans up PID and IPC files.

    Returns True only when every recorded PID is confirmed gone (or there
    were none to stop-and-stop succeeded). Returns False if a process is
    still alive after the SIGKILL escalation, so callers never report a
    clean stop while the orchestrator keeps running (#31).
    """
    pids = [
        ("orchestrator", read_pid(project_path)),
        ("dashboard", read_dashboard_pid(project_path)),
    ]
    live = [(label, pid) for label, pid in pids if pid is not None]
    if not live:
        cleanup_session(project_path)
        return False

    for _label, pid in live:
        _terminate_process_tree(pid, force=False)

    deadline = time.monotonic() + _STOP_GRACE_SECONDS
    survivors = [pid for _label, pid in live if _process_alive(pid)]
    while survivors and time.monotonic() < deadline:
        time.sleep(_STOP_POLL_INTERVAL)
        survivors = [pid for pid in survivors if _process_alive(pid)]

    for pid in survivors:
        _terminate_process_tree(pid, force=True)

    # SIGKILL is delivered asynchronously — poll briefly to confirm the
    # process actually exited before claiming success (#31).
    kill_deadline = time.monotonic() + _DASHBOARD_STOP_GRACE_SECONDS
    survivors = [pid for _label, pid in live if _process_alive(pid)]
    while survivors and time.monotonic() < kill_deadline:
        time.sleep(_STOP_POLL_INTERVAL)
        survivors = [pid for pid in survivors if _process_alive(pid)]

    cleanup_session(project_path)
    return not survivors
