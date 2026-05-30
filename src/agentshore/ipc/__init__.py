"""IPC layer for embedded agent mode."""

from __future__ import annotations

from agentshore.ipc.provider import IpcStateProvider
from agentshore.ipc.server import IpcServer
from agentshore.ipc.state_writer import NullStateWriter, StateWriter

__all__ = ["IpcServer", "IpcStateProvider", "NullStateWriter", "StateWriter"]
