"""Local IPC server — inbound command channel only.

Manages a Unix domain socket or TCP endpoint, parses inbound NDJSON
commands, validates them, and places them on a shared command queue for
the orchestrator to consume.

State updates and lifecycle events are *not* broadcast over this socket.
They are persisted by :class:`agentshore.ipc.state_writer.StateWriter` to
files in the session directory; the dashboard bridge tails those files.
This shape removes the engine-side drain-timeout / abort policy that
previously froze the dashboard ~20 minutes into long sessions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

import structlog

from agentshore.ipc.commands import parse_command, validate_command
from agentshore.session_path import IpcEndpoint, is_unix_socket_path, unlink_socket_if_present

if TYPE_CHECKING:
    from pathlib import Path

_logger = structlog.get_logger()


class IpcServer:
    """Async local IPC server for inbound commands.

    Each connected client may send NDJSON command lines (one per line).
    Parsed + validated commands land on :attr:`command_queue`; the
    orchestrator consumes them. Outbound traffic is limited to error
    responses for malformed commands — there is no streaming push from
    the server.
    """

    def __init__(self, endpoint: IpcEndpoint | str | Path) -> None:
        self._endpoint = (
            endpoint if isinstance(endpoint, IpcEndpoint) else IpcEndpoint.unix(endpoint)
        )
        self._socket_path = self._endpoint.path
        self._server: asyncio.AbstractServer | None = None
        self._command_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self._client_count = 0
        self._cached_state: str | None = None

    def set_cached_state(self, message: str) -> None:
        """Cache the latest serialized ``state_update`` envelope for ``get_state``.

        The provider calls this after every state snapshot so that an
        on-demand ``get_state`` command can be served from memory without
        waiting for the next heartbeat. A trailing newline (if present)
        is stripped so the handler can write the envelope back with a
        single appended ``\\n`` and produce a valid NDJSON line.
        """
        self._cached_state = message.rstrip("\n")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start listening on the configured IPC endpoint.

        Removes a stale Unix socket file if one already exists at the
        configured path before binding.
        """
        if self._endpoint.kind == "unix":
            await self._prepare_unix_path()
        self._server = await self._bind()
        await _logger.ainfo("ipc.server_started", endpoint=self._endpoint.label)

    async def _prepare_unix_path(self) -> None:
        """Unlink a stale entry at the Unix bind path before binding.

        ``Path.exists()`` follows symlinks, so a dangling symlink reads as
        "missing" and used to slip through — ``bind()`` would then create the
        real socket inside whatever directory the symlink pointed at (e.g. a
        vanished pytest tmpdir). Check symlink status with lstat-aware probes
        first.
        """
        if self._socket_path is None:
            return
        if self._socket_path.is_symlink():
            target = self._socket_path.readlink()
            await _logger.awarning(
                "ipc.stale_symlink",
                path=str(self._socket_path),
                target=str(target),
            )
            self._socket_path.unlink()
        elif self._socket_path.exists():
            if not is_unix_socket_path(self._socket_path):
                raise RuntimeError(f"refusing to unlink non-socket IPC path: {self._socket_path}")
            await _logger.awarning(
                "ipc.stale_socket",
                path=str(self._socket_path),
            )
            unlink_socket_if_present(self._socket_path)

    async def _bind(self) -> asyncio.AbstractServer:
        """Bind the configured endpoint and return the listening server.

        For TCP, ``self._endpoint`` is rewritten to the concrete bound
        host/port (so an ephemeral ``port=0`` request resolves to the real
        port).
        """
        if self._endpoint.kind == "unix":
            if self._socket_path is None:
                raise RuntimeError("Unix IPC endpoint missing path")
            server = await asyncio.start_unix_server(
                self._handle_client,
                path=str(self._socket_path),
            )
            self._socket_path.chmod(0o600)
            return server
        server = await asyncio.start_server(
            self._handle_client,
            host=self._endpoint.host,
            port=self._endpoint.port,
        )
        sock = server.sockets[0] if server.sockets else None
        if sock is not None:
            host, port = sock.getsockname()[:2]
            self._endpoint = IpcEndpoint.tcp(str(host), int(port))
        return server

    async def stop(self) -> None:
        """Stop the server and remove the Unix socket file if present."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        if (
            self._endpoint.kind == "unix"
            and self._socket_path is not None
            and self._socket_path.exists()
        ):
            unlink_socket_if_present(self._socket_path)

        await _logger.ainfo("ipc.server_stopped", endpoint=self._endpoint.label)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def command_queue(self) -> asyncio.Queue[dict[str, object]]:
        """The shared inbound command queue for the orchestrator."""
        return self._command_queue

    @property
    def endpoint(self) -> IpcEndpoint:
        """The concrete endpoint the server is listening on."""
        return self._endpoint

    @property
    def client_count(self) -> int:
        """Number of clients currently connected (for diagnostics/tests)."""
        return self._client_count

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single client connection — read commands, enqueue them."""
        self._client_count += 1
        await _logger.ainfo("ipc.client_connected")

        try:
            while True:
                line_bytes = await reader.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8").strip()
                if not line:
                    continue

                try:
                    cmd = parse_command(line)
                    validate_command(cmd)
                except ValueError as exc:
                    await _logger.awarning(
                        "ipc.invalid_command",
                        error=str(exc),
                        raw_line=line,
                    )
                    error_response = json.dumps({"type": "error", "error": str(exc)}) + "\n"
                    try:
                        writer.write(error_response.encode("utf-8"))
                        await writer.drain()
                    except (ConnectionError, OSError):
                        break
                    continue

                if cmd.get("command") == "get_state":
                    # Capture into a local before any await so a concurrent
                    # set_cached_state from the provider cannot tear the read.
                    cached = self._cached_state
                    if cached is None:
                        reply = (
                            json.dumps({"type": "error", "error": "no cached state available"})
                            + "\n"
                        )
                    else:
                        reply = cached + "\n"
                    try:
                        writer.write(reply.encode("utf-8"))
                        await writer.drain()
                    except (ConnectionError, OSError):
                        break
                    continue

                await self._command_queue.put(cmd)
        except (ConnectionError, OSError):
            pass
        finally:
            self._client_count = max(0, self._client_count - 1)
            await _logger.ainfo("ipc.client_disconnected")
            with contextlib.suppress(ConnectionError, OSError):
                writer.close()
                await writer.wait_closed()
