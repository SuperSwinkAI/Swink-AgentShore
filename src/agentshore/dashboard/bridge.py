"""WebSocket bridge — relays file-backed AgentShore state to browser clients.

The orchestrator writes a coalesced ``dashboard_state.json`` snapshot and
appends every lifecycle event to ``dashboard_events.ndjson`` in the
session directory (see :class:`agentshore.ipc.state_writer.StateWriter`).
The bridge tails both files and fans the contents out to browser
WebSocket clients.

Commands sent by the browser (pause, resume, override, …) are still
forwarded over the legacy IPC command socket; only the *outbound* state
path was migrated. This shape eliminates the engine-side stall guard
(``transport.abort()`` after a 10 s drain timeout) that froze the
dashboard ~20 minutes into every long session.

The bridge also serves the static dashboard assets over HTTP.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.websockets import WebSocket

    from agentshore.session_path import IpcEndpoint

import structlog

from agentshore.ipc.commands import parse_command, validate_command
from agentshore.ipc.state_writer import EVENTS_FILENAME, STATE_FILENAME

_logger = structlog.get_logger()

_DEFAULT_PORT = 9400
_STATE_POLL_INTERVAL: float = 0.5
_EVENTS_POLL_INTERVAL: float = 0.5
_WS_SEND_TIMEOUT: float = 2.0
_WS_CLOSE_TIMEOUT: float = 0.5
_EVENT_REPLAY_LIMIT: int = 8


class DashboardBridge:
    """Relay between the session's file-backed state and browser clients.

    Watches ``dashboard_state.json`` for mtime changes and tails
    ``dashboard_events.ndjson`` by byte offset; both are broadcast to
    every connected WebSocket. The first connected tab is granted a
    local auth token and can send commands; later tabs are read-only
    until promoted.
    """

    def __init__(
        self,
        *,
        ipc_endpoint: IpcEndpoint,
        session_dir: Path,
        port: int = _DEFAULT_PORT,
        static_dir: Path | None = None,
        on_ready: Callable[[], None] | None = None,
        session_id: str | None = None,
        state_poll_interval: float = _STATE_POLL_INTERVAL,
        events_poll_interval: float = _EVENTS_POLL_INTERVAL,
    ) -> None:
        from agentshore.session_path import read_session_id_by_dir

        self._ipc_endpoint = ipc_endpoint
        self._port = port
        self._static_dir = static_dir or (Path(__file__).parent / "static")
        self._on_ready = on_ready

        self._session_dir = session_dir
        self._state_path = session_dir / STATE_FILENAME
        self._events_path = session_dir / EVENTS_FILENAME
        self._state_poll_interval = state_poll_interval
        self._events_poll_interval = events_poll_interval

        # Session identity gate: a bridge serves exactly one session. The id is
        # taken from the caller when known (sidecar threads it through before the
        # orchestrator boots) or resolved from this session_dir's ``info.json``
        # (the standalone ``agentshore dashboard`` attaches to a running session).
        # When still unknown, it is adopted from the first observed snapshot.
        # Any snapshot whose ``session_id`` differs is treated as a prior
        # session's stale file and never adopted or replayed.
        self._current_session_id: str | None = session_id or read_session_id_by_dir(session_dir)
        # True once we've seen an on-disk/live snapshot for the session we serve.
        # Until then we replay nothing on connect — replaying a prior session's
        # cached snapshot would poison the client's monotonic seq de-dup and
        # suppress the new session's low-seq bootstrap frames.
        self._observed_current_state = False

        self._ws_clients: list[WebSocket] = []
        self._latest_state: str | None = None
        self._active_play: dict[str, object] | None = None
        self._event_history: list[dict[str, object]] = []
        # Raw line of the most recent in-progress ``bootstrap_phase`` event so a
        # browser that connects (or reloads) mid-bootstrap can render the loading
        # modal immediately — these events are otherwise broadcast live-only, so a
        # late/reconnecting tab would never see the modal (desktop-afp).
        self._bootstrap_phase_raw: str | None = None

        self._ipc_writer: asyncio.StreamWriter | None = None
        self._state_task: asyncio.Task[None] | None = None
        self._events_task: asyncio.Task[None] | None = None
        self._running = False
        self._session_ended = False
        self._session_ended_raw: str | None = None
        self._session_draining_raw: str | None = None
        self._auth_token = secrets.token_urlsafe(32)
        self._controlling_ws: WebSocket | None = None

        # File tail bookkeeping.
        self._state_mtime: float | None = None
        self._events_offset: int = 0
        self._events_pending: bytes = b""

    async def start(self) -> None:
        """Start the HTTP/WS server and file watchers; block until shutdown."""
        import uvicorn
        from starlette.applications import Starlette
        from starlette.routing import Mount, WebSocketRoute
        from starlette.staticfiles import StaticFiles
        from starlette.websockets import WebSocketDisconnect

        index_html = self._static_dir / "index.html"
        if not index_html.exists():
            msg = (
                f"Dashboard assets not found at {self._static_dir}\n"
                "Build them with: cd dashboard && npm install && npm run build"
            )
            raise FileNotFoundError(msg)

        self._running = True

        # Prime caches from any state already on disk so the first browser
        # tab renders immediately even before the engine emits a new event.
        await self._prime_from_disk()

        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            self._ws_clients.append(websocket)
            await _logger.ainfo(
                "dashboard.ws_client_connected",
                total_clients=len(self._ws_clients),
            )

            try:
                await self._replay_to_ws(websocket)
                if self._controlling_ws is None:
                    self._controlling_ws = websocket
                    await websocket.send_text(
                        json.dumps({"type": "auth_token", "token": self._auth_token})
                    )
                else:
                    await websocket.send_text(json.dumps({"type": "read_only"}))

                while True:
                    raw = await websocket.receive_text()
                    await self._handle_ws_command(raw, websocket)
            except WebSocketDisconnect:
                pass
            except (OSError, ConnectionError) as exc:
                await _logger.aerror(
                    "dashboard.ws_endpoint_failed",
                    error=str(exc),
                    total_clients=len(self._ws_clients),
                )
            finally:
                was_controller = websocket is self._controlling_ws
                if websocket in self._ws_clients:
                    self._ws_clients.remove(websocket)
                if was_controller:
                    self._controlling_ws = None
                    await self._promote_next_client()
                await _logger.ainfo(
                    "dashboard.ws_client_disconnected",
                    total_clients=len(self._ws_clients),
                )

        routes = [
            WebSocketRoute("/ws", ws_endpoint),
            Mount("/", app=StaticFiles(directory=str(self._static_dir), html=True)),
        ]
        app = Starlette(routes=routes)

        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self._port,
            log_level="warning",
        )
        server = uvicorn.Server(config)

        self._state_task = asyncio.create_task(self._state_watcher_loop())
        self._events_task = asyncio.create_task(self._events_tail_loop())

        async def _stop_server_when_session_ends() -> None:
            while self._running and not self._session_ended:
                await asyncio.sleep(self._events_poll_interval)
            if self._session_ended:
                await _logger.ainfo("dashboard.server_exit_after_session_ended")
                server.should_exit = True

        server_exit_task = asyncio.create_task(_stop_server_when_session_ends())

        if self._on_ready is not None:
            callback = self._on_ready

            async def _wait_and_signal() -> None:
                while not server.started:
                    await asyncio.sleep(0.05)
                callback()

            _ready_task = asyncio.create_task(_wait_and_signal())
            _ready_task.add_done_callback(
                lambda t: (
                    _logger.error("ready_signal_failed", error=str(t.exception()))
                    if not t.cancelled() and t.exception() is not None
                    else None
                )
            )

        try:
            await server.serve()
        except (asyncio.CancelledError, KeyboardInterrupt):
            await _logger.ainfo("dashboard.server_cancelled")
            raise
        except Exception as exc:
            # desktop-6a7: the bridge has been observed to die silently after
            # controller_promoted with no traceback. Anything uvicorn raises
            # from .serve() is captured here so operators can see *why* the
            # bridge exited rather than just seeing `lsof -i :9400` empty.
            await _logger.aexception(
                "dashboard.server_crashed",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            self._running = False
            for task in (self._state_task, self._events_task, server_exit_task):
                if task is not None:
                    task.cancel()
            for task in (self._state_task, self._events_task, server_exit_task):
                if task is None:
                    continue
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._state_task = None
            self._events_task = None
            self._ws_clients.clear()
            await self._disconnect_ipc()

    # ------------------------------------------------------------------
    # File tailing
    # ------------------------------------------------------------------

    async def _prime_from_disk(self) -> None:
        """Best-effort load of existing state + tail of events on startup."""
        await self._poll_state_file()
        # If the on-disk snapshot doesn't belong to the session we serve (or
        # there's no snapshot yet), don't prime any event caches — they would be
        # the prior session's bootstrap/play events. Still advance the events
        # offset to the current end so the live tail starts after the stale
        # backlog rather than replaying it (a truncating reset resets the offset
        # back to 0 in _read_new_events_sync).
        if not self._observed_current_state:
            try:
                self._events_offset = self._events_path.stat().st_size
            except OSError:
                self._events_offset = 0
            return
        try:
            size = self._events_path.stat().st_size
        except OSError:
            self._events_offset = 0
            return

        # Read the last ~64KB so we can replay the most recent events on
        # connect without scanning the whole file.
        tail_window = 64 * 1024
        start = max(0, size - tail_window)
        try:
            with self._events_path.open("rb") as fh:
                fh.seek(start)
                data = fh.read()
        except OSError:
            self._events_offset = size
            return

        if start > 0:
            # Drop any partial line at the head of the read window.
            newline = data.find(b"\n")
            if newline != -1:
                data = data[newline + 1 :]

        self._events_offset = size
        for line in data.splitlines():
            if not line:
                continue
            text = line.decode("utf-8", errors="replace")
            self._ingest_event_line(text, broadcast=False)

    async def _state_watcher_loop(self) -> None:
        """Poll the state file; on mtime change, broadcast the new snapshot."""
        try:
            while self._running:
                changed = await asyncio.to_thread(self._poll_state_file_sync)
                if changed and self._latest_state is not None:
                    await self._broadcast(self._latest_state)
                await asyncio.sleep(self._state_poll_interval)
        except asyncio.CancelledError:
            pass

    def _poll_state_file_sync(self) -> bool:
        """Synchronous half of :meth:`_poll_state_file`; returns True on change."""
        try:
            stat = self._state_path.stat()
        except OSError:
            return False
        if self._state_mtime is not None and stat.st_mtime_ns == self._state_mtime:
            return False
        try:
            raw = self._state_path.read_text(encoding="utf-8")
        except OSError:
            return False
        self._state_mtime = stat.st_mtime_ns
        candidate = raw.rstrip("\n")
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            # Unattributable (malformed) snapshot: keep prior behaviour and let
            # the watcher broadcast it, but don't treat it as proof we've seen
            # the current session — it must not gate the replay or be cached.
            self._latest_state = candidate
            return True
        payload = parsed.get("payload") if isinstance(parsed, dict) else None
        snapshot_sid = payload.get("session_id") if isinstance(payload, dict) else None

        # Session-aware gate. When the snapshot names a session different from
        # the one we serve, it's a prior session's stale file (e.g. one the
        # engine failed to unlink on Windows) — drop it without adopting or
        # caching. A snapshot with no session_id (older engines) can't be gated,
        # so fall back to serving it.
        if isinstance(snapshot_sid, str):
            if self._current_session_id is None:
                self._current_session_id = snapshot_sid
            elif snapshot_sid != self._current_session_id:
                return False

        self._latest_state = candidate
        self._observed_current_state = True
        if isinstance(payload, dict) and "active_play" in payload:
            ap = payload.get("active_play")
            self._active_play = ap if isinstance(ap, dict) else None
        return True

    async def _poll_state_file(self) -> None:
        """Async wrapper used only during startup priming."""
        await asyncio.to_thread(self._poll_state_file_sync)

    async def _events_tail_loop(self) -> None:
        """Tail the NDJSON events file by byte offset and fan out new lines."""
        try:
            while self._running:
                lines = await asyncio.to_thread(self._read_new_events_sync)
                for line in lines:
                    self._ingest_event_line(line, broadcast=True)
                    await self._broadcast(line)
                await asyncio.sleep(self._events_poll_interval)
        except asyncio.CancelledError:
            pass

    def _read_new_events_sync(self) -> list[str]:
        """Read newly-appended bytes from the events file, split into lines."""
        try:
            stat = self._events_path.stat()
        except OSError:
            return []

        size = stat.st_size
        # File was truncated/rotated — restart from beginning.
        if size < self._events_offset:
            self._events_offset = 0
            self._events_pending = b""
        if size == self._events_offset:
            return []

        try:
            with self._events_path.open("rb") as fh:
                fh.seek(self._events_offset)
                chunk = fh.read(size - self._events_offset)
        except OSError:
            return []
        self._events_offset = size

        buf = self._events_pending + chunk
        if not buf:
            return []
        if buf.endswith(b"\n"):
            self._events_pending = b""
            parts = buf.split(b"\n")
        else:
            tail_start = buf.rfind(b"\n")
            if tail_start == -1:
                self._events_pending = buf
                return []
            self._events_pending = buf[tail_start + 1 :]
            parts = buf[: tail_start + 1].split(b"\n")

        out: list[str] = []
        for part in parts:
            if not part:
                continue
            out.append(part.decode("utf-8", errors="replace"))
        return out

    def _ingest_event_line(self, raw_line: str, *, broadcast: bool) -> None:
        """Update caches from one events-file line."""
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict):
            return
        msg_type = msg.get("type")

        # Tier 1 backstop: with session_id stamped on every event, reject any
        # that names a session other than the one we serve. Events without an id
        # (older engines) fall through to the Tier 0 prime/replay gating.
        event_sid = fields.get("session_id")
        if (
            isinstance(event_sid, str)
            and self._current_session_id is not None
            and event_sid != self._current_session_id
        ):
            return

        if msg_type == "session_draining":
            if broadcast:
                self._session_draining_raw = raw_line
            return
        if msg_type == "session_ended":
            # Defence in depth alongside the StateWriter per-session reset:
            # never let prime-from-disk replay a stale lifecycle marker
            # into our live state — it would set should_exit on boot.
            if broadcast:
                self._session_ended = True
                self._session_ended_raw = raw_line
            return
        if msg_type == "bootstrap_phase":
            # Cache the current bootstrap phase (set during prime-from-disk and
            # live tail alike) so it can be replayed to a connecting/reloading
            # client. Clear it once bootstrap finishes (``ready``/``completed``)
            # so a post-bootstrap connect shows no stale modal.
            status = fields.get("status")
            phase = fields.get("phase")
            if phase == "ready" and status == "completed":
                self._bootstrap_phase_raw = None
            elif status == "started":
                self._bootstrap_phase_raw = raw_line
            return

        if msg_type != "play_event":
            return

        payload = msg.get("payload")
        if not isinstance(payload, dict):
            return
        fields: dict[str, object] = payload

        event = dict(fields)
        event["type"] = msg_type
        self._event_history.append(event)
        self._event_history = self._event_history[-_EVENT_REPLAY_LIMIT:]

        status = fields.get("status")
        if status == "started":
            self._active_play = {
                "play_type": fields.get("play_type"),
                "agent_id": fields.get("agent_id"),
                "issue_number": fields.get("issue_number"),
                "pr_number": fields.get("pr_number"),
                "branch": fields.get("branch"),
                "play_id": fields.get("play_id"),
                "started_at": fields.get("started_at") or datetime.now(UTC).isoformat(),
                "trigger_agent_id": fields.get("trigger_agent_id"),
                "trigger_agent_type": fields.get("trigger_agent_type"),
                "trigger_error_class": fields.get("trigger_error_class"),
            }
        elif status in {"completed", "failed"}:
            self._active_play = None

        # `broadcast` is informational — the caller is responsible for the
        # actual fan-out so the prime-from-disk path can stay silent.
        _ = broadcast

    # ------------------------------------------------------------------
    # WebSocket fan-out
    # ------------------------------------------------------------------

    async def _broadcast(self, message: str) -> None:
        """Send *message* to every connected WebSocket; drop on failure."""
        if not self._ws_clients:
            return
        stale: list[WebSocket] = []
        for ws in list(self._ws_clients):
            try:
                await asyncio.wait_for(ws.send_text(message), timeout=_WS_SEND_TIMEOUT)
            except (TimeoutError, ConnectionError, OSError) as exc:
                await _logger.awarning(
                    "dashboard.ws_send_failed",
                    error="timeout" if isinstance(exc, TimeoutError) else str(exc),
                )
                stale.append(ws)
            except Exception as exc:
                await _logger.awarning(
                    "dashboard.ws_send_failed",
                    error=str(exc),
                )
                stale.append(ws)
        for ws in stale:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(), timeout=_WS_CLOSE_TIMEOUT)
            if ws in self._ws_clients:
                self._ws_clients.remove(ws)

    async def _send_ws_error(self, websocket: WebSocket, error: str) -> None:
        """Send an error message to a WebSocket client, ignoring failures."""
        with contextlib.suppress(Exception):
            await websocket.send_text(json.dumps({"type": "error", "error": error}))

    async def _replay_to_ws(self, websocket: WebSocket) -> None:
        """Replay cached dashboard state to a newly connected WebSocket.

        No session gate is needed here: ``_poll_state_file_sync`` never caches a
        cross-session snapshot, and ``_prime_from_disk`` never primes the event
        caches from a non-current on-disk state — so every cache below holds
        only current-session data. (A ``bootstrap_phase`` cached live during the
        current run is still replayed so a tab connecting mid-bootstrap renders
        the loading modal — the desktop-afp behaviour.)
        """
        if self._latest_state is not None:
            await websocket.send_text(self._latest_state)
        if self._bootstrap_phase_raw is not None and not self._session_ended:
            await websocket.send_text(self._bootstrap_phase_raw)
        if self._active_play is not None:
            await websocket.send_text(
                json.dumps({"type": "active_play_replay", "active_play": self._active_play})
            )
        if self._event_history:
            await websocket.send_text(
                json.dumps({"type": "event_history_replay", "events": self._event_history})
            )
        if self._session_draining_raw is not None and not self._session_ended:
            await websocket.send_text(self._session_draining_raw)
        if self._session_ended and self._session_ended_raw is not None:
            await websocket.send_text(self._session_ended_raw)

    async def _promote_next_client(self) -> None:
        """Promote the next connected WebSocket client to controller."""
        if not self._ws_clients:
            return
        self._controlling_ws = self._ws_clients[0]
        with contextlib.suppress(Exception):
            await self._controlling_ws.send_text(
                json.dumps({"type": "auth_token", "token": self._auth_token})
            )
        await _logger.ainfo("dashboard.controller_promoted")

    # ------------------------------------------------------------------
    # Inbound commands → IPC writer
    # ------------------------------------------------------------------

    async def _handle_ws_command(self, raw: str, websocket: WebSocket) -> None:
        """Validate and forward a command from a WebSocket client to IPC."""
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            await self._send_ws_error(websocket, "Invalid JSON")
            return

        is_feedback_response = parsed.get("command") == "feedback_response"
        has_control = websocket is self._controlling_ws and parsed.get("token") == self._auth_token
        if not has_control and not is_feedback_response:
            await self._send_ws_error(websocket, "Read-only connection")
            return

        parsed.pop("token", None)
        stripped = json.dumps(parsed)

        try:
            cmd = parse_command(stripped)
            validate_command(cmd)
        except ValueError as exc:
            await self._send_ws_error(websocket, str(exc))
            return

        if not await self._ensure_ipc_writer():
            await self._send_ws_error(websocket, "Not connected to AgentShore")
            return

        assert self._ipc_writer is not None
        try:
            self._ipc_writer.write((stripped.rstrip("\n") + "\n").encode("utf-8"))
            await self._ipc_writer.drain()
        except (ConnectionError, OSError) as exc:
            await _logger.awarning("dashboard.ipc_write_failed", error=str(exc))
            await self._disconnect_ipc()
            await self._send_ws_error(websocket, "IPC connection lost")

    async def _ensure_ipc_writer(self) -> bool:
        """Open (or re-open) the IPC writer if needed. Returns True on success."""
        if self._ipc_writer is not None and not self._ipc_writer.is_closing():
            return True
        return await self._connect_ipc()

    async def _connect_ipc(self) -> bool:
        """Open a connection to the IPC endpoint for sending commands."""
        try:
            if self._ipc_endpoint.kind == "unix":
                _, self._ipc_writer = await asyncio.open_unix_connection(
                    str(self._ipc_endpoint.path)
                )
            else:
                _, self._ipc_writer = await asyncio.open_connection(
                    self._ipc_endpoint.host,
                    self._ipc_endpoint.port,
                )
            await _logger.ainfo("dashboard.ipc_connected", endpoint=self._ipc_endpoint.label)
            return True
        except (ConnectionError, OSError, FileNotFoundError, RuntimeError) as exc:
            await _logger.awarning("dashboard.ipc_connect_failed", error=str(exc))
            self._ipc_writer = None
            return False

    async def _disconnect_ipc(self) -> None:
        """Close the IPC writer if open."""
        if self._ipc_writer is not None:
            try:
                self._ipc_writer.close()
                await self._ipc_writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self._ipc_writer = None
