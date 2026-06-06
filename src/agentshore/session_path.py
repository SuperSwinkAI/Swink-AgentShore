"""Session path utilities — well-known IPC and PID locations.

Each AgentShore session writes its IPC state to ``~/.agentshore/sessions/<hash>/``
where ``<hash>`` is a stable SHA-256 prefix derived from the project's
absolute path. The directory holds the default Unix domain socket when used,
PID files, and a JSON ``info.json`` sidecar that external tools (dashboards,
scripts) can consult to introspect the running session.

Stale-detection rule: a session is considered "running" only if the recorded
orchestrator PID is alive. When the PID is absent or its process is gone,
``is_session_running`` and ``discover_ipc_endpoint`` proactively remove stale
files so the next ``agentshore start`` doesn't trip over leftover state.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import socket
import stat
import subprocess  # nosec B404
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

from agentshore.logging import get_logger
from agentshore.paths import GLOBAL_SESSIONS_DIR

_logger = get_logger(__name__)

_SESSIONS_DIR = GLOBAL_SESSIONS_DIR

# Time we'll wait for SIGTERM to land before escalating to SIGKILL.
_STOP_GRACE_SECONDS = 60.0
_STOP_POLL_INTERVAL = 0.1
_DASHBOARD_STOP_GRACE_SECONDS = 5.0

IpcKind = Literal["unix", "tcp"]


@dataclass(frozen=True, slots=True)
class IpcEndpoint:
    """Concrete IPC endpoint for AgentShore control/state traffic."""

    kind: IpcKind
    path: Path | None = None
    host: str = "127.0.0.1"
    port: int = 0

    @classmethod
    def unix(cls, path: Path | str) -> IpcEndpoint:
        return cls(kind="unix", path=Path(path))

    @classmethod
    def tcp(cls, host: str = "127.0.0.1", port: int = 0) -> IpcEndpoint:
        return cls(kind="tcp", host=host, port=port)

    @property
    def label(self) -> str:
        if self.kind == "unix":
            return str(self.path)
        return f"{self.host}:{self.port}"

    def to_json(self) -> dict[str, object]:
        if self.kind == "unix":
            return {"kind": "unix", "path": str(self.path)}
        return {"kind": "tcp", "host": self.host, "port": self.port}


def ipc_endpoint_from_json(raw: object) -> IpcEndpoint | None:
    """Parse an endpoint object from ``info.json``."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind == "unix":
        path = raw.get("path")
        return IpcEndpoint.unix(path) if isinstance(path, str) and path else None
    if kind == "tcp":
        host = raw.get("host")
        port = raw.get("port")
        if not isinstance(host, str) or not host:
            return None
        try:
            parsed_port = int(port) if port is not None else 0
        except (TypeError, ValueError):
            return None
        if parsed_port <= 0:
            return None
        return IpcEndpoint.tcp(host, parsed_port)
    return None


def default_ipc_endpoint(
    project_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> IpcEndpoint:
    """Return the platform-default IPC endpoint for a project."""
    if sys.platform.startswith("win"):
        return IpcEndpoint.tcp(host, port)
    return IpcEndpoint.unix(session_socket_path(project_path))


def find_free_tcp_port(host: str = "127.0.0.1") -> int:
    """Return an available local TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def find_dashboard_port(start: int = 9400, end: int = 9410) -> int:
    """Return the first free TCP port in ``[start, end)``, or *start* if all busy.

    The dashboard bridge prefers the stable 9400-range so users get a
    predictable ``localhost:<port>`` across runs, unlike the OS-assigned port
    from :func:`find_free_tcp_port`.
    """
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def resolve_start_ipc_endpoint(
    project_path: Path,
    *,
    socket_override: str | None,
    ipc_host: str,
    ipc_port: int,
) -> tuple[IpcEndpoint, str]:
    """Resolve the IPC endpoint and on-disk socket path for ``agentshore start``.

    Returns ``(ipc_endpoint, resolved_socket)``. With no ``socket_override`` the
    platform-default endpoint is used (auto-selecting a free TCP port when the
    requested port is 0) and the resolved socket is the well-known per-project
    path. With an explicit ``socket_override`` a Unix endpoint is bound to it,
    and a best-effort symlink is planted at the well-known path so
    ``agentshore dashboard`` auto-discovery (which hashes the project dir) keeps
    working; the symlink is skipped when the override already *is* the well-known
    path (the backgrounded dashboard launcher re-passes the resolved path to the
    child, so symlinking would create ``socket.sock -> socket.sock`` and later
    ``bind()`` would fail with ``ELOOP``). Filesystems without symlink support
    fall back to ``info.json`` discovery, so symlink ``OSError`` is swallowed.
    """
    well_known_socket = session_socket_path(project_path)
    if socket_override is None:
        ipc_endpoint = default_ipc_endpoint(project_path, host=ipc_host, port=ipc_port)
        if ipc_endpoint.kind == "tcp" and ipc_endpoint.port == 0:
            ipc_endpoint = IpcEndpoint.tcp(ipc_endpoint.host, find_free_tcp_port(ipc_endpoint.host))
        return ipc_endpoint, str(well_known_socket)

    resolved_socket = socket_override
    ipc_endpoint = IpcEndpoint.unix(resolved_socket)
    explicit = Path(resolved_socket)
    if explicit.resolve() != well_known_socket.resolve():
        try:
            if well_known_socket.exists() or well_known_socket.is_symlink():
                well_known_socket.unlink()
            well_known_socket.symlink_to(explicit.resolve())
        except OSError:
            pass
    return ipc_endpoint, resolved_socket


def _project_hash(project_path: Path) -> str:
    """Stable 16-char hex hash of an absolute project path."""
    return hashlib.sha256(str(project_path).encode()).hexdigest()[:16]


def session_dir(project_path: Path) -> Path:
    """Return ``<GLOBAL_SESSIONS_DIR>/<hash>/`` for the given project.

    ``GLOBAL_SESSIONS_DIR`` is the platformdirs user-config sessions directory
    (e.g. ``~/Library/Application Support/agentshore/sessions`` on macOS), so the
    concrete prefix is platform-dependent and not hardcoded here.
    """
    return _SESSIONS_DIR / _project_hash(project_path.resolve())


def session_socket_path(project_path: Path) -> Path:
    """Return the well-known socket path for a project.

    ``<GLOBAL_SESSIONS_DIR>/<hash>/socket.sock`` (see ``session_dir``).
    """
    return session_dir(project_path) / "socket.sock"


def session_pid_path(project_path: Path) -> Path:
    """Return the PID file path for a project session."""
    return session_dir(project_path) / "agentshore.pid"


def dashboard_pid_path(project_path: Path) -> Path:
    """Return the PID file path for the dashboard subprocess."""
    return session_dir(project_path) / "dashboard.pid"


def is_unix_socket_path(path: Path) -> bool:
    """Return True only for a real Unix socket path, not symlinks or files."""
    if sys.platform.startswith("win"):
        return False
    try:
        return stat.S_ISSOCK(path.lstat().st_mode)
    except OSError:
        return False


def unlink_socket_if_present(path: Path) -> bool:
    """Unlink *path* only when it is a real Unix socket."""
    if not is_unix_socket_path(path):
        return False
    path.unlink(missing_ok=True)
    return True


def _has_live_unix_socket_listener(path: Path, *, connect_timeout: float = 0.3) -> bool:
    """Return True if a process is actively listening on the Unix socket at *path*.

    Used by ``cleanup_session`` to refuse to unlink a socket that an
    orchestrator is still bound to — observed 2026-05-18 (desktop-6e1): a
    bridge crash left ``cleanup_session`` to run while the orchestrator (with
    no recorded session.pid) was still accepting commands on its socket FD;
    unlinking the file made the running session unreachable to ``agentshore
    dashboard``.
    """
    if sys.platform.startswith("win"):
        return False

    import socket as _socket

    if not is_unix_socket_path(path):
        return False
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
            sock.settimeout(connect_timeout)
            sock.connect(str(path))
            return True
    except (OSError, TimeoutError):
        return False


def session_info_path(project_path: Path) -> Path:
    """Return the JSON sidecar path for a project session.

    The ``info.json`` sidecar records PID, started_at (UTC ISO-8601),
    project_path, and the actual IPC endpoint. External tools can consult it
    to introspect the session
    without having to recompute the project hash.
    """
    return session_dir(project_path) / "info.json"


def discover_ipc_endpoint(project_path: Path) -> IpcEndpoint | None:
    """Find the live IPC endpoint for a running session, or None if not found.

    An endpoint is considered "live" only if the recorded session PID (if any)
    is still running. If the endpoint exists but the orchestrator PID is dead,
    this calls :func:`cleanup_session` and returns ``None`` — matching the
    stale-endpoint detection requirement so callers can report
    "no running session" cleanly.

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


def write_pid(project_path: Path) -> None:
    """Write the current process PID to the session directory."""
    pid_path = session_pid_path(project_path)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")


def write_dashboard_pid(project_path: Path, pid: int) -> None:
    """Record the dashboard subprocess PID alongside the session PID."""
    pid_path = dashboard_pid_path(project_path)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(pid), encoding="utf-8")


def write_session_info(
    project_path: Path,
    *,
    socket_path: Path | str | None = None,
    ipc_endpoint: IpcEndpoint | None = None,
    extra: Mapping[str, object] | None = None,
) -> Path:
    """Write the ``info.json`` sidecar for the session.

    Records ``pid``, ``started_at`` (UTC ISO-8601), ``project_path``, and
    ``socket`` (the actual socket path).  ``extra`` lets callers attach
    additional fields (e.g. mode, dashboard URL) without touching this
    helper.  Returns the path written.
    """
    info_path = session_info_path(project_path)
    info_path.parent.mkdir(parents=True, exist_ok=True)
    if ipc_endpoint is None:
        ipc_endpoint = (
            IpcEndpoint.unix(socket_path)
            if socket_path is not None
            else default_ipc_endpoint(project_path)
        )
    resolved_socket = (
        str(ipc_endpoint.path)
        if ipc_endpoint.kind == "unix" and ipc_endpoint.path is not None
        else str(Path(socket_path))
        if socket_path is not None
        else str(session_socket_path(project_path))
    )
    payload: dict[str, object] = {
        "pid": os.getpid(),
        "started_at": datetime.now(UTC).isoformat(),
        "project_path": str(project_path.resolve()),
        "socket": resolved_socket,
        "ipc": ipc_endpoint.to_json(),
    }
    if extra:
        payload.update(dict(extra))
    info_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return info_path


def read_session_info(project_path: Path) -> dict[str, object] | None:
    """Read the ``info.json`` sidecar, or None if absent or unreadable."""
    info_path = session_info_path(project_path)
    if not info_path.exists():
        return None
    try:
        data = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def timelapse_info_path(project_path: Path) -> Path:
    """Return the ``timelapse.json`` sidecar path for a project session.

    Records the active dashboard timelapse capture's run-id and working dir so
    the detached ``agentshore start --dashboard`` launcher (which starts the
    capture) and the separate ``agentshore stop`` command (which finalises the
    render) can coordinate across processes.
    """
    return session_dir(project_path) / "timelapse.json"


def write_timelapse_info(project_path: Path, *, run_id: str, runs_cwd: Path | str) -> Path:
    """Persist the active timelapse capture handle. Returns the path written."""
    info_path = timelapse_info_path(project_path)
    info_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"run_id": run_id, "runs_cwd": str(runs_cwd)}
    info_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return info_path


def read_timelapse_info(project_path: Path) -> dict[str, object] | None:
    """Read the ``timelapse.json`` sidecar, or None if absent or unreadable."""
    info_path = timelapse_info_path(project_path)
    if not info_path.exists():
        return None
    try:
        data = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def clear_timelapse_info(project_path: Path) -> None:
    """Remove the ``timelapse.json`` sidecar (best-effort)."""
    timelapse_info_path(project_path).unlink(missing_ok=True)


def read_pid(project_path: Path) -> int | None:
    """Read the PID from the session directory, or None if not found."""
    return _read_pid_file(session_pid_path(project_path))


def read_dashboard_pid(project_path: Path) -> int | None:
    """Read the dashboard PID from the session directory, or None if missing."""
    return _read_pid_file(dashboard_pid_path(project_path))


def _read_pid_file(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


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


# ---------------------------------------------------------------------------
# Process lifecycle management
# ---------------------------------------------------------------------------


# -- low-level process utilities --------------------------------------------


def _process_alive(pid: int) -> bool:
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


# -- high-level operations --------------------------------------------------


def request_drain(
    project_path: Path,
    *,
    end_session_report: bool = False,
    open_report: bool = True,
) -> str:
    """Send a graceful drain request to the running orchestrator over IPC.

    Returns a status string.
    """
    import json as _json
    import socket as _socket

    endpoint = discover_ipc_endpoint(project_path)
    if endpoint is None:
        return "fallback_hard"

    try:
        family = _socket.AF_UNIX if endpoint.kind == "unix" else _socket.AF_INET
        with _socket.socket(family, _socket.SOCK_STREAM) as sock:
            sock.settimeout(5.0)
            if endpoint.kind == "unix":
                if endpoint.path is None:
                    return "fallback_hard"
                sock.connect(str(endpoint.path))
            else:
                sock.connect((endpoint.host, endpoint.port))
            cmd = {
                "command": "drain",
                "reason": "cli_request",
                "end_session_report": end_session_report,
                "open_report": open_report,
            }
            encoded = _json.dumps(cmd) + "\n"
            sock.sendall(encoded.encode())
        return "sent"
    except TimeoutError:
        return "timeout"
    except (AttributeError, OSError):
        return "error"


def budget_from_state_line(line: bytes | None) -> dict[str, object] | None:
    """Extract the ``budget`` mapping from a single ``state_update`` reply line.

    ``get_state`` replies with the enveloped ``state_update`` message
    (``{"type": "state_update", ..., "payload": {..., "budget": {...}}}``), so the
    budget lives under ``payload``. Tolerates a flat shape too. Returns ``None``
    for a blank/unparseable/error line or a missing budget.
    """
    import json as _json

    if line is None or not line.strip():
        return None
    try:
        env = _json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, _json.JSONDecodeError):
        return None
    if not isinstance(env, dict) or env.get("type") == "error":
        return None
    payload = env.get("payload")
    container = payload if isinstance(payload, dict) else env
    budget = container.get("budget")
    return budget if isinstance(budget, dict) else None


def request_add_budget(
    project_path: Path,
    *,
    delta_usd: float | None,
    delta_minutes: int | None,
) -> str | dict[str, object]:
    """Additively top up / extend the live session budget over IPC.

    The NDJSON control channel is fire-and-forget for mutating commands, but the
    server answers ``get_state`` from its cached ``state_update``. Because
    ``add_budget`` is applied asynchronously by the orchestrator's command pump,
    a single immediate ``get_state`` can race ahead of the change. So this reads a
    baseline, sends ``add_budget``, then polls ``get_state`` until the cached
    budget reflects the new caps (or a short deadline elapses) and returns it, so
    the CLI reports the *applied* caps rather than the pre-change snapshot.

    Returns ``"no_session"`` when no IPC endpoint is discoverable, ``"error"`` /
    ``"timeout"`` on transport failure, or the budget dict on success (``{}`` if
    the snapshot never carried a budget).
    """
    import json as _json
    import socket as _socket
    import time as _time

    endpoint = discover_ipc_endpoint(project_path)
    if endpoint is None:
        return "no_session"

    add_cmd: dict[str, object] = {"command": "add_budget"}
    if delta_usd is not None:
        add_cmd["delta_usd"] = delta_usd
    if delta_minutes is not None:
        add_cmd["delta_minutes"] = delta_minutes
    get_state = (_json.dumps({"command": "get_state"}) + "\n").encode()

    def _read_line(sock: _socket.socket, buf: bytes) -> tuple[bytes | None, bytes]:
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                return None, buf
            buf += chunk
        line, _, rest = buf.partition(b"\n")
        return line, rest

    try:
        family = _socket.AF_UNIX if endpoint.kind == "unix" else _socket.AF_INET
        with _socket.socket(family, _socket.SOCK_STREAM) as sock:
            sock.settimeout(5.0)
            if endpoint.kind == "unix":
                if endpoint.path is None:
                    return "no_session"
                sock.connect(str(endpoint.path))
            else:
                sock.connect((endpoint.host, endpoint.port))

            buf = b""
            # Baseline so we can tell when the async apply lands.
            sock.sendall(get_state)
            line, buf = _read_line(sock, buf)
            baseline = budget_from_state_line(line)

            sock.sendall((_json.dumps(add_cmd) + "\n").encode())

            applied = baseline
            deadline = _time.monotonic() + 3.0
            while _time.monotonic() < deadline:
                _time.sleep(0.15)
                sock.sendall(get_state)
                line, buf = _read_line(sock, buf)
                current = budget_from_state_line(line)
                if current is not None and current != baseline:
                    applied = current
                    break
    except TimeoutError:
        return "timeout"
    except (AttributeError, OSError):
        return "error"

    return applied if isinstance(applied, dict) else {}


def stop_dashboard_process(project_path: Path) -> bool:
    """Terminate the recorded dashboard bridge process, if one exists."""
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


def cleanup_session(project_path: Path) -> None:
    """Remove stale PID, dashboard PID, info, and Unix socket files."""
    info = read_session_info(project_path)

    for path in (
        session_pid_path(project_path),
        dashboard_pid_path(project_path),
        session_info_path(project_path),
        timelapse_info_path(project_path),
    ):
        if path.exists() or path.is_symlink():
            path.unlink(missing_ok=True)
    well_known_socket = session_socket_path(project_path)
    if not _has_live_unix_socket_listener(well_known_socket):
        unlink_socket_if_present(well_known_socket)

    if info is not None:
        recorded = info.get("socket")
        if isinstance(recorded, str):
            external = Path(recorded)
            with contextlib.suppress(OSError):
                if not _has_live_unix_socket_listener(external):
                    unlink_socket_if_present(external)

    sd = session_dir(project_path)
    if sd.exists():
        try:
            next(sd.iterdir())
        except StopIteration:
            with contextlib.suppress(OSError):
                sd.rmdir()
