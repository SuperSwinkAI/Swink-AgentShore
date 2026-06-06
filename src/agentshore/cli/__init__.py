"""AgentShore CLI entry point.

The ``main`` Click group is this package's public surface — the ``agentshore``
console script and ``python -m agentshore`` both resolve to it. Subcommands and
their helpers live in :mod:`agentshore.cli.commands` and sibling modules; import
them from their real homes (e.g. ``agentshore.cli.helpers``,
``agentshore.cli_helpers``) rather than through this package namespace.
"""

from __future__ import annotations

import click

from agentshore import __version__
from agentshore.cli.commands.dashboard import dashboard
from agentshore.cli.commands.identity import identity
from agentshore.cli.commands.init import init
from agentshore.cli.commands.start import start
from agentshore.cli.commands.stop import stop
from agentshore.cli.commands.trusted_ids import trusted_ids
from agentshore.platform_compat import ensure_windows_event_loop_policy


@click.group()
@click.version_option(__version__, prog_name="agentshore")
def main() -> None:
    """AgentShore -- RL-based multi-agent coding orchestrator."""
    ensure_windows_event_loop_policy()


# Subcommands attach their own sub-subcommands (e.g. ``archive create``,
# ``trusted-ids add-gh``) via decorators at import time, so importing the
# top-level group object is sufficient to register the full command tree.
main.add_command(start)
main.add_command(init)
main.add_command(identity)
main.add_command(trusted_ids)
main.add_command(dashboard)
main.add_command(stop)


__all__ = ["main"]
