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
import os
import re
import subprocess
from typing import TYPE_CHECKING

import structlog

from agentshore.beads import BdError, bd, resolve_bd_binary
from agentshore.state import AgentType

if TYPE_CHECKING:
    from pathlib import Path

_logger = structlog.get_logger(__name__)

# Supply-chain + change-control pin for the `bd` (beads) binary. bd's CLI
# semantics directly shape the beads graph — e.g. `bd link`'s default
# dependency type changed across releases and silently inverted epic/task
# linkage (blocking every leaf task → zero implementation work). Pinning the
# binary means such a change can't slip in unannounced. Override with the
# AGENTSHORE_BD_VERSION env var (set it empty to disable the check) only after
# re-verifying the skill-template `bd` calls against the new release.
REQUIRED_BD_VERSION = "1.0.4"


def _check_bd_version(bd_binary: str) -> None:
    """Assert the resolved bd binary matches the pinned version.

    Raises RuntimeError on mismatch. Honours AGENTSHORE_BD_VERSION as an
    override (empty value disables the check entirely).
    """
    expected = os.environ.get("AGENTSHORE_BD_VERSION", REQUIRED_BD_VERSION).strip()
    if not expected:
        return
    try:
        completed = subprocess.run(
            [bd_binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
            # Never inherit the sidecar's stdin (the live Tauri JSON-RPC pipe);
            # a subprocess probing it can wedge session startup (#155).
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"could not determine bd version via `{bd_binary} --version`: {exc}"
        ) from exc
    match = re.search(r"\d+\.\d+\.\d+", completed.stdout or completed.stderr or "")
    found = match.group(0) if match else (completed.stdout or "").strip()
    if found != expected:
        raise RuntimeError(
            f"bd version {found!r} does not match AgentShore's pinned version {expected!r}. "
            "bd is pinned because its CLI semantics affect the beads graph (e.g. `bd link`'s "
            "default dependency type changed between releases and silently broke epic/task "
            "linkage). Install bd "
            f"{expected} from https://github.com/gastownhall/beads, or set AGENTSHORE_BD_VERSION "
            "to override after re-verifying the skill-template `bd` calls."
        )


# Map AgentType enum values to the bd hooks actor name.  API-only agents
# have no bd setup target and are omitted.
_BD_ACTOR_NAMES: dict[AgentType, str] = {
    AgentType.CLAUDE_CODE: "claude",
    AgentType.CODEX: "codex",
    AgentType.GROK: "grok",
}


def ensure_bd_installed() -> None:
    """Verify that `bd` is on PATH and matches the pinned version.

    If bd is absent, delegates to ``downloader.provision_bd`` which enforces
    the consent gate: interactive sessions may prompt the user; headless
    sessions fail with instructions unless ``AGENTSHORE_AUTO_INSTALL_BD=1``
    is set. This function never silently downloads a binary in headless mode.

    Raises RuntimeError with install instructions if bd is not found (and
    consent for download is absent), or if its version does not match
    REQUIRED_BD_VERSION (see AGENTSHORE_BD_VERSION override). This check is
    intentionally synchronous so it can be called from the Click-based
    `agentshore init` command without an event loop.
    """
    from agentshore.beads.downloader import provision_bd

    # provision_bd returns the existing bd path when already installed,
    # downloads + returns the installed path when consent is present, or raises
    # with instructions when bd is absent and no consent is given (the
    # headless-fail invariant).
    bd_binary = resolve_bd_binary()
    if bd_binary is None:
        bd_binary = provision_bd(REQUIRED_BD_VERSION)
    if bd_binary is None:
        # Reachable only when the download was attempted and failed (best-effort
        # path returns None); the no-consent path raises inside provision_bd.
        raise RuntimeError(
            "The bd binary was not found. Set AGENTSHORE_BD_BIN to a bundled binary or install "
            "bd from https://github.com/gastownhall/beads and re-run agentshore init."
        )
    _check_bd_version(bd_binary)
    _logger.info("bd_available", path=bd_binary, required_version=REQUIRED_BD_VERSION)


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
