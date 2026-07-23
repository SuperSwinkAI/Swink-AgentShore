"""Session-boundary durability push for the beads Dolt store.

``bd dolt push`` mirrors the local, embedded Dolt store to whatever remote
``bd init`` auto-configured from the repo's git origin (if any). Verified
behavior (bd 1.1.0): with no remote configured, it is a graceful no-op — exit
0, stdout "No remote is configured — skipping." — so it is safe to call
unconditionally at every session boundary, project or no project remote.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.beads import BdError, bd
from agentshore.beads.lock import BD_TIMEOUT_SECONDS as _PUSH_TIMEOUT_SECONDS
from agentshore.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

_logger = get_logger(__name__)

_NO_REMOTE_MARKER = "no remote is configured"


async def push_beads_remote(project_path: Path) -> bool:
    """Best-effort ``bd dolt push`` at a session boundary. Never raises.

    Upstream warns that concurrent pushes can corrupt a git-protocol Dolt
    remote's history; AgentShore's session-end path is single-writer by
    construction (one orchestrator owns the local store at shutdown), so that
    hazard does not apply here.

    Returns ``True`` when the push ran (including the no-remote no-op),
    ``False`` on any failure.
    """
    try:
        stdout = await bd(
            "dolt",
            "push",
            cwd=project_path,
            timeout_seconds=_PUSH_TIMEOUT_SECONDS,
        )
    except BdError as exc:
        _logger.warning(
            "beads_dolt_push_failed",
            project_path=str(project_path),
            error=str(exc),
        )
        return False

    if _NO_REMOTE_MARKER in stdout.lower():
        _logger.info("beads_dolt_push_skipped_no_remote", project_path=str(project_path))
    else:
        _logger.info("beads_dolt_push_ok", project_path=str(project_path))
    return True
