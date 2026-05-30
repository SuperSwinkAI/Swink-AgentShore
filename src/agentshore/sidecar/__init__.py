"""AgentShore desktop sidecar.

Long-running Python process spawned by the Tauri 2 desktop shell. Speaks
JSON-RPC 2.0 over stdin/stdout per ``docs/design/desktop/DESIGN.md`` §2.2.

This module ships the minimum surface needed to be PyInstaller-frozen and
to satisfy the ``app.handshake`` contract from §2.6. Lifecycle and project
RPC methods (``project.inspect``, ``session.start``, etc.) land in
follow-up stories on the ``desktop-c8i`` epic.

``EmbeddedBridge`` (§1.2 / §2.3) lets the embedded dashboard WebSocket
bridge run as another asyncio task inside the sidecar so a future
``session.start`` can advertise the auto-selected loopback port back to
the WebView.
"""

from __future__ import annotations

from agentshore.sidecar.embedded_bridge import BridgeEndpoint, EmbeddedBridge

__all__ = ["BridgeEndpoint", "EmbeddedBridge"]
