"""``agents/cli`` — leaf submodules extracted from ``cli_agent``.

This package exists as a structured home for the four concern groups; the
canonical public surface is still ``agentshore.agents.cli_agent`` (which
re-exports everything). Internal code within the package should import
directly from the submodules; external code uses ``cli_agent``.
"""

from __future__ import annotations
