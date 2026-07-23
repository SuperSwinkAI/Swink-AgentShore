"""Session path utilities — well-known IPC and PID locations.

Each AgentShore session writes its IPC state to ``~/.agentshore/sessions/<hash>/``
where ``<hash>`` is a stable SHA-256 prefix derived from the project's
absolute path. The directory holds the default Unix domain socket when used,
PID files, and a JSON ``info.json`` sidecar that external tools (dashboards,
scripts) can consult to introspect the running session.

This module is pure path/endpoint resolution and on-disk sidecar I/O — no
process liveness checks and no IPC transport. ``cleanup_session`` removes
stale PID/socket/sidecar files given a project path, but the decision of
*when* a session counts as stale (PID liveness, staleness-triggered endpoint
discovery) lives in :mod:`agentshore.session_process`, along with SIGTERM/
SIGKILL escalation and the synchronous IPC client. See that module's
docstring for the split rationale.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import stat
import sys
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


def find_ipc_tcp_port(host: str = "127.0.0.1", start: int = 9411, end: int = 9512) -> int:
    """Return the first free TCP port in a stable application range for IPC.

    Deliberately scans a fixed low range (just above the dashboard's 9400-block)
    instead of letting the OS hand out an ephemeral port via
    :func:`find_free_tcp_port`. On Windows, security suites that proxy loopback
    traffic (e.g. Avast's Web Shield) camp on freshly-freed ports in the
    ephemeral range (49152+). An ephemeral ``bind(port=0)`` then loses a race to
    such a proxy, and a later bind of that exact port fails with WSAEACCES
    (WinError 10013, *not* the in-use 10048) — which crashes the orchestrator's
    IPC server, since it binds a concrete pre-resolved port with no retry. A
    fixed app-range port sidesteps the ephemeral lottery entirely; the dashboard
    bridge already proves this range is reachable under the same AV. Falls back
    to an ephemeral port only if the whole range is occupied (≈100 concurrent
    sessions), which is strictly no worse than the prior behaviour.
    """
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    return find_free_tcp_port(host)


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
            ipc_endpoint = IpcEndpoint.tcp(ipc_endpoint.host, find_ipc_tcp_port(ipc_endpoint.host))
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

    if not is_unix_socket_path(path):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
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


def read_session_id_by_dir(sdir: Path) -> str | None:
    """Read ``session_id`` from a session directory's ``info.json``.

    Like :func:`read_session_info` but keyed by the resolved session directory
    rather than a project path: the dashboard bridge holds the ``session_dir``
    it is tailing (not the project path) yet needs to know *which* session it
    serves so it can reject a prior session's stale snapshot. Returns None when
    the sidecar is absent, unreadable, or carries no string ``session_id``.
    """
    info_path = sdir / "info.json"
    if not info_path.exists():
        return None
    try:
        data = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    session_id = data.get("session_id")
    return session_id if isinstance(session_id, str) else None


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
