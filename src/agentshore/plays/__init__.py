"""Play system — 20 play definitions, execution contracts, parameter resolution."""

from __future__ import annotations

from agentshore.plays.base import Play, PlayParams
from agentshore.plays.executor import PlayExecutor
from agentshore.plays.registry import PlayRegistry
from agentshore.plays.selector import PlaySelector

__all__ = ["Play", "PlayExecutor", "PlayParams", "PlayRegistry", "PlaySelector"]
