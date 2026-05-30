"""Agent manager — lifecycle, subprocess management, API adapters."""

from __future__ import annotations

from agentshore.agents.capabilities import AGENT_CAPABILITIES
from agentshore.agents.handle import AgentHandle, AgentInvocationResult
from agentshore.agents.manager import AgentManager

__all__ = [
    "AGENT_CAPABILITIES",
    "AgentHandle",
    "AgentInvocationResult",
    "AgentManager",
]
