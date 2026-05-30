"""Beads project setup helpers called by `agentshore init`.

Three steps, in order:
  1. ensure_bd_installed()        — verify `bd` is on PATH
  2. bd_init_project(path)        — run `bd init` if .beads/ is absent
  3. bd_setup_for_agent_types(...)— run `bd hooks install` for git integration

These are synchronous-safe wrappers that delegate to asyncio subprocesses
via the helpers in agentshore.beads. They are intentionally kept separate from
the core beads module so the CLI can import them without pulling in the full
async graph-loading machinery.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from agentshore.beads import BdError, bd, resolve_bd_binary
from agentshore.state import AgentType

if TYPE_CHECKING:
    from pathlib import Path

_logger = structlog.get_logger(__name__)

# Map AgentType enum values to the bd hooks actor name.  API-only agents
# have no bd setup target and are omitted.
_BD_ACTOR_NAMES: dict[AgentType, str] = {
    AgentType.CLAUDE_CODE: "claude",
    AgentType.CODEX: "codex",
}


def ensure_bd_installed() -> None:
    """Verify that `bd` is on PATH.

    Raises RuntimeError with install instructions if bd is not found.
    This check is intentionally synchronous so it can be called from the
    Click-based `agentshore init` command without spinning up an event loop.
    """
    bd_binary = resolve_bd_binary()
    if bd_binary is None:
        raise RuntimeError(
            "The bd binary was not found. Set AGENTSHORE_BD_BIN to a bundled binary or install "
            "bd from https://github.com/gastownhall/beads and re-run agentshore init."
        )
    _logger.info("bd_available", path=bd_binary)


async def bd_init_project(project_path: Path) -> None:
    """Run `bd init` in *project_path* if the beads store does not yet exist.

    Idempotent — if ``.beads/`` already exists the call is a no-op.
    Also writes ``.beads/.gitignore`` containing ``*`` so the local bead
    store is never committed to version control.
    """
    beads_dir = project_path / ".beads"
    if beads_dir.exists():
        _logger.info("bd_init_skipped", reason="already_initialised", path=str(beads_dir))
        return

    _logger.info("bd_init_running", project_path=str(project_path))
    await bd("init", cwd=project_path)
    _logger.info("bd_init_done", path=str(beads_dir))

    # Ensure the store is gitignored.
    gitignore = beads_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")


async def bd_setup_for_agent_types(
    project_path: Path,
    enabled_agent_types: set[AgentType],
) -> list[str]:
    """Install bd git hooks for agent-identity tracking.

    Runs ``bd hooks install`` once for the project (bd v1.0.x has a single
    hooks install step, not a per-agent one). The *enabled_agent_types* set
    is used to log which actors are relevant; only CLI agents are relevant
    for beads integration — API agents have no local git identity.

    Returns the list of bd actor names that are active in this project.
    """
    if not (project_path / ".beads").exists():
        _logger.warning("bd_setup_skipped", reason="no_beads_dir")
        return []

    # Install git hooks (idempotent in bd).
    try:
        await bd("hooks", "install", cwd=project_path)
        _logger.info("bd_hooks_installed", project_path=str(project_path))
    except BdError as exc:
        _logger.warning("bd_hooks_install_failed", error=str(exc))

    actors = [_BD_ACTOR_NAMES[at] for at in enabled_agent_types if at in _BD_ACTOR_NAMES]
    if actors:
        _logger.info("bd_agent_actors_configured", actors=actors)
    return actors


def run_beads_init(
    project_path: Path,
    enabled_agent_types: set[AgentType],
) -> None:
    """Synchronous entry point called from `agentshore init` (Click context).

    Runs the full beads setup sequence:
      1. ensure_bd_installed (synchronous check)
      2. bd_init_project      (async, run in a new event loop)
      3. bd_setup_for_agent_types (async, same event loop)

    Any failure in step 1 propagates to the caller; steps 2–3 log warnings
    rather than aborting init so a failed beads step never blocks the rest
    of `agentshore init`.
    """
    ensure_bd_installed()

    async def _run() -> None:
        await bd_init_project(project_path)
        await bd_setup_for_agent_types(project_path, enabled_agent_types)

    asyncio.run(_run())
