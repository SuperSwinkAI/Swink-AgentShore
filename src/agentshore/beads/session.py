"""Session-scoped progress cleanup for beads ``in_progress`` state."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agentshore.beads.lock import BdError
from agentshore.beads.parsing import _as_json_list, _parse_bead
from agentshore.beads.types import BeadStatus
from agentshore.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

_logger = get_logger(__name__)


async def clear_in_progress_beads(project_path: Path) -> int:
    """Reset session-scoped ``in_progress`` beads to ``open``.

    AgentShore treats beads ``in_progress`` as an external mirror of active work,
    not as a durable lock. A crashed or stopped session can leave the status
    behind and block future issue pickup, so lifecycle boundaries clear it.

    Returns the number of beads successfully reset. Failures are logged and do
    not abort the caller's session startup or shutdown path.
    """
    from agentshore.beads import bd  # noqa: PLC0415

    if not (project_path / ".beads").exists():
        return 0

    try:
        raw = await bd("query", "status=in_progress", "--json", cwd=project_path)
        items = _as_json_list(raw)
    except (BdError, json.JSONDecodeError, ValueError) as exc:
        _logger.warning(
            "beads_in_progress_query_failed",
            project_path=str(project_path),
            error=str(exc),
        )
        return 0

    reset_count = 0
    for item in items:
        bead = _parse_bead(item)
        if not bead.bead_id or bead.status != BeadStatus.IN_PROGRESS:
            continue
        try:
            await bd(
                "update",
                bead.bead_id,
                "--status",
                BeadStatus.OPEN.value,
                "--dolt-auto-commit=on",
                cwd=project_path,
            )
            reset_count += 1
        except BdError as exc:
            _logger.warning(
                "beads_in_progress_reset_failed",
                project_path=str(project_path),
                bead_id=bead.bead_id,
                error=str(exc),
            )
    return reset_count
