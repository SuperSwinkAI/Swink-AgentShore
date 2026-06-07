"""Embed the dashboard WebSocket bridge inside the sidecar process.

Per ``docs/design/desktop/DESIGN.md`` §1.2 and §2.3, the JSON-RPC sidecar,
the Orchestrator, the existing IPC server, and the dashboard WebSocket
bridge share a single asyncio loop as cooperative tasks.
``EmbeddedBridge`` is the thin lifecycle wrapper around
:class:`agentshore.dashboard.bridge.DashboardBridge` that:

* selects a free TCP loopback port at construction so the WebView address
  can be advertised back in ``session.start``'s response payload (§2.3);
* runs the bridge as a supervised ``asyncio.Task`` in the same loop; and
* exposes the resolved endpoint via :meth:`endpoint`.

The ``session.start`` RPC method (story ``desktop-0vc.11``) instantiates
one ``EmbeddedBridge`` per session, returns the endpoint payload to the
shell, and stops it during ``session.stop``.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, TypedDict

from agentshore.dashboard.bridge import DashboardBridge
from agentshore.session_path import find_dashboard_port

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.session_path import IpcEndpoint


class BridgeEndpoint(TypedDict):
    """Shape advertised in the ``session.start`` response (§2.3)."""

    kind: str
    host: str
    port: int
    url: str


class EmbeddedBridge:
    """Run a :class:`DashboardBridge` as an asyncio task in the sidecar process."""

    def __init__(
        self,
        ipc_endpoint: IpcEndpoint,
        *,
        session_dir: Path,
        host: str = "127.0.0.1",
        port: int | None = None,
        static_dir: Path | None = None,
        session_id: str | None = None,
    ) -> None:
        self._host = host
        # Stable 9400-range (not an ephemeral port) for a predictable browser URL
        # and to dodge the Windows AV loopback-proxy that camps the ephemeral
        # range — see agentshore.session_path.find_ipc_tcp_port.
        self._port = find_dashboard_port() if port is None else port
        self._ready: asyncio.Event = asyncio.Event()
        self._bridge = DashboardBridge(
            ipc_endpoint=ipc_endpoint,
            session_dir=session_dir,
            port=self._port,
            static_dir=static_dir,
            on_ready=self._ready.set,
            # Pin the session identity before the orchestrator boots: the bridge
            # primes (phase 5) before the first snapshot is written (phase 6),
            # and info.json isn't written until after, so without this the
            # bridge could adopt a prior session's stale snapshot as "current".
            session_id=session_id,
        )
        self._task: asyncio.Task[None] | None = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def endpoint(self) -> BridgeEndpoint:
        """Return the WebSocket endpoint payload for ``session.start``."""
        return {
            "kind": "ws",
            "host": self._host,
            "port": self._port,
            "url": f"ws://{self._host}:{self._port}/ws",
        }

    async def start(self) -> None:
        """Launch the bridge task and wait for uvicorn to bind the port.

        Raises whatever the underlying bridge raises if it fails before the
        ``on_ready`` callback fires (e.g. missing static assets).
        """
        if self._task is not None:
            return

        self._task = asyncio.create_task(self._bridge.start(), name="dashboard-bridge")
        ready_task = asyncio.create_task(self._ready.wait(), name="dashboard-bridge-ready")
        try:
            done, _pending = await asyncio.wait(
                {ready_task, self._task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._task in done:
                # The bridge task completed (or failed) before signalling ready.
                exc = self._task.exception() if not self._task.cancelled() else None
                self._task = None
                if exc is not None:
                    raise exc
        finally:
            ready_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ready_task

    async def stop(self) -> None:
        """Cancel the bridge task and wait for it to unwind."""
        task = self._task
        if task is None:
            return
        self._task = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._ready.clear()
