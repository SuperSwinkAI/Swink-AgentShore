"""Canonical session-preflight sequencing shared by both launch paths.

Deliberately dependency-free (no click, no CLI helpers): the sidecar imports
this at session-start time, and routing it through ``session.bootstrap`` would
recreate the bootstrap -> cli.helpers -> cli.commands.start -> bootstrap
circular import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


def run_session_preflight(
    *,
    identities: Callable[[], None],
    cli_agent_auth: Callable[[], None],
    git_auth: Callable[[], None],
    skip_auth_preflight: bool = False,
    skip_git_auth_preflight: bool = False,
) -> None:
    """Pin the preflight gate order: identities, CLI backend auth, git auth.

    Both launch paths — ``agentshore start`` and the desktop sidecar's
    ``check_agent_auth`` phase — call this so the set and order of gates can
    never silently drift between them again. The git-remote gate is the
    desktop sidecar's #151/#178/#179 class of bug: it ran identities +
    backend auth but never the git-remote probe, so a broken git credential
    slipped past launch and wedged mid-session instead of failing fast here.

    Each gate's actual work AND failure handling (banner + ``SystemExit`` for
    the CLI, ``SessionStartError`` for the sidecar) is owned by the
    caller-supplied hook — this function only pins the order and the two
    existing skip flags. ``identities`` always runs; ``cli_agent_auth`` and
    ``git_auth`` are each skippable independently, matching the CLI's
    ``--skip-auth-preflight`` / ``--skip-git-auth-preflight`` flags (the
    sidecar has no such flags yet, so it always passes both as ``False``).
    """
    identities()
    if not skip_auth_preflight:
        cli_agent_auth()
    if not skip_git_auth_preflight:
        git_auth()
