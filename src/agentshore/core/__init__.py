"""Core orchestrator package.

The public surface is :class:`Orchestrator`. Internal helpers, phase
functions, and dataclasses live in their own modules
(``agentshore.core.phases``, ``agentshore.core.helpers``,
``agentshore.core.context``, ``agentshore.core.mixins.*``) and are imported
from there directly — including by tests, which patch each symbol at its
binding home rather than through a package-level re-export wall.
"""

from __future__ import annotations

from agentshore.core.orchestrator import Orchestrator

__all__ = ["Orchestrator"]
