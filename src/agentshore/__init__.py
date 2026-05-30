"""AgentShore — RL-based multi-agent coding orchestrator."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agentshore")
except PackageNotFoundError:
    __version__ = "0.0.0"
