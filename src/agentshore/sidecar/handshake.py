"""``app.handshake`` response builder.

Per ``docs/design/desktop/DESIGN.md`` §2.6 the first call after spawn is
``app.handshake`` and the response carries ``protocol_version``,
``agentshore_version``, ``sidecar_build_id``, and a ``capabilities`` list. The
shell aborts on a ``build_id`` mismatch.
"""

from __future__ import annotations

from typing import TypedDict

from agentshore import __version__ as agentshore_version
from agentshore.sidecar.build_id import load_build_info

PROTOCOL_VERSION = 1
"""Sidecar JSON-RPC protocol version. Bump on breaking schema changes."""


class HandshakeResponse(TypedDict):
    protocol_version: int
    agentshore_version: str
    sidecar_build_id: str
    capabilities: list[str]


class HandshakeParams(TypedDict):
    client: str
    client_build_id: str


def capabilities() -> list[str]:
    """Return the set of methods the sidecar advertises to the shell.

    The list grows as later ``desktop-c8i`` stories implement methods.
    """
    return [
        "app.handshake",
        "archive.list",
        "archive.fetch_report",
        "archive.fetch_logs",
        "session.start",
        "session.status",
        "session.stop",
        "config.read",
        "config.write",
        "identities.list",
        "identities.add",
        "identities.update",
        "identities.remove",
        "agents.list",
        "agents.configure",
        "$/cancelRequest",
        "project.select",
        "project.inspect",
        "project.branches",
        "project.set_target_branch",
        "project.set_budget",
        "project.set_timelapse",
        "project.install_timelapse",
        "project.deselect",
        "recents.list",
        "recents.touch",
        "recents.remove",
        # Notification methods (sidecar → shell), DESIGN §5.1.
        # Listed so the Rust supervisor can feature-detect support.
        "session.completed",
        "sidecar.health",
        "agent.subprocess_spawned",
        "agent.subprocess_exited",
    ]


def build_response() -> HandshakeResponse:
    info = load_build_info()
    return {
        "protocol_version": PROTOCOL_VERSION,
        "agentshore_version": agentshore_version,
        "sidecar_build_id": info["build_id"],
        "capabilities": capabilities(),
    }


def validate_params(params: object) -> HandshakeParams:
    """Validate ``app.handshake`` params per DESIGN §2.6."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    client = params.get("client")
    client_build_id = params.get("client_build_id")
    if not isinstance(client, str) or not client.strip():
        raise ValueError("params.client must be a non-empty string")
    if not isinstance(client_build_id, str) or not client_build_id.strip():
        raise ValueError("params.client_build_id must be a non-empty string")
    return {"client": client, "client_build_id": client_build_id}
