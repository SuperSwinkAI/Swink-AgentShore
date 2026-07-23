"""Dependency-cycle mutation: mirroring a ``depends on #N`` edge into beads.

``add_blocking_dependency`` is called the moment ``issue_pickup`` discovers a
body-declared dependency at execute time (#14), so the candidate mask
excludes the dependent issue immediately rather than waiting for the next
``groom_backlog`` pass. ``_would_create_cycle`` / ``_confirm_cycle_via_reachability``
back up bd's own cycle rejection with a client-side reachability check so an
upstream stderr wording change can't silently regress the needs-human
escalation (commit 78555b3).
"""

from __future__ import annotations

from collections import deque
from enum import StrEnum
from typing import TYPE_CHECKING

from agentshore.beads.graph import load_graph
from agentshore.beads.lock import BdError, GraphReadError
from agentshore.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.beads.types import ProjectGraph

_logger = get_logger(__name__)


class BlockingDependencyOutcome(StrEnum):
    """Result of :func:`add_blocking_dependency`."""

    LINKED = "linked"
    #: bd rejected the edge because the opposite edge already exists between
    #: the same two beads. This is not retry-worthy: the edge can never land
    #: without first removing the conflicting one, so the caller should
    #: escalate to a human rather than fall back to an ordinary label gate.
    CYCLE_CONFLICT = "cycle_conflict"
    #: Beads not initialised, empty/equal ids, or a bd error unrelated to a
    #: cycle. The caller falls back to a label gate.
    UNAVAILABLE = "unavailable"


def _would_create_cycle(graph: ProjectGraph, blocked_bead_id: str, blocker_bead_id: str) -> bool:
    """Client-side reachability check: would ``blocked -> blocker`` create a cycle?

    A ``blocks`` edge records "blocked depends_on blocker" (see
    ``_depends_on_ids_from_raw``): ``blocked`` cannot proceed until
    ``blocker`` closes. Adding that edge creates a cycle iff ``blocker`` can
    already (transitively) reach ``blocked`` by following existing
    depends_on edges forward — i.e. the blocker already, directly or
    transitively, depends on the very bead we're about to block. Plain BFS
    over ``graph.tasks`` (the already-parsed snapshot); no extra bd calls.

    Scoped to task-type beads: every caller of ``add_blocking_dependency``
    resolves both ids from ``state.graph.tasks`` (see
    ``executor.py``'s ``_apply_block_issue_on``), so task-only reachability
    covers every case this fallback needs; a cycle routed through a
    non-task bead (story/epic) would not be found here, but bd's own
    stderr-substring fast path still catches those.
    """
    depends_on_by_id = {task.bead_id: task.depends_on_ids for task in graph.tasks}
    seen: set[str] = set()
    queue: deque[str] = deque([blocker_bead_id])
    while queue:
        current = queue.popleft()
        if current == blocked_bead_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        queue.extend(depends_on_by_id.get(current, frozenset()) - seen)
    return False


async def _confirm_cycle_via_reachability(
    project_path: Path, blocked_bead_id: str, blocker_bead_id: str
) -> bool:
    """Force-fresh graph reload + reachability check for the version-proof fallback.

    Used only when bd's ``link`` failure did NOT match the known
    "would create a cycle" stderr substring — protects the needs-human
    escalation (commit 78555b3) from an upstream error-string rewording.
    Swallows ``GraphReadError``: if we can't even confirm, the caller falls
    back to ``UNAVAILABLE`` (an ordinary label gate) rather than escalating
    on a guess.
    """
    try:
        graph = await load_graph(project_path, max_age_seconds=0.0)
    except GraphReadError:
        return False
    if graph is None:
        return False
    return _would_create_cycle(graph, blocked_bead_id, blocker_bead_id)


async def add_blocking_dependency(
    project_path: Path,
    blocked_bead_id: str,
    blocker_bead_id: str,
) -> BlockingDependencyOutcome:
    """Add a ``blocks`` edge so *blocked_bead_id* is blocked by *blocker_bead_id*.

    Mirrors a body-declared ``depends on #N`` dependency into the beads graph
    the moment ``issue_pickup`` discovers it at execute time, so the candidate
    mask excludes the dependent issue before another agent is dispatched (#14)
    rather than waiting for the next ``groom_backlog`` pass to mirror it.

    Runs ``bd link <blocked> <blocker> --type blocks`` (id2 blocks id1, so the
    second arg is the blocker — matching the groom/seed skills and bd's own
    ``bd link`` semantics). Returns ``UNAVAILABLE`` when beads is not
    initialised, either id is empty, or the two ids are equal. A bd error is
    swallowed rather than raised so a beads hiccup never fails the play: it
    comes back as ``CYCLE_CONFLICT`` when bd reports the edge would create a
    cycle (the caller should escalate, not just fall back to a plain label —
    see ``needs-human`` handling in ``_apply_block_issue_on``), or
    ``UNAVAILABLE`` for any other bd error.
    """
    from agentshore.beads import bd  # noqa: PLC0415

    if not (project_path / ".beads").exists():
        return BlockingDependencyOutcome.UNAVAILABLE
    if not blocked_bead_id or not blocker_bead_id or blocked_bead_id == blocker_bead_id:
        return BlockingDependencyOutcome.UNAVAILABLE
    try:
        await bd(
            "link",
            blocked_bead_id,
            blocker_bead_id,
            "--type",
            "blocks",
            "--dolt-auto-commit=on",
            cwd=project_path,
        )
    except BdError as exc:
        if "would create a cycle" in str(exc):
            _logger.warning(
                "beads_add_blocking_dependency_cycle_conflict",
                project_path=str(project_path),
                blocked=blocked_bead_id,
                blocker=blocker_bead_id,
                error=str(exc),
            )
            return BlockingDependencyOutcome.CYCLE_CONFLICT
        # Version-proof fallback (#4): the stderr substring didn't match —
        # bd may have reworded its error text — so confirm client-side via a
        # fresh graph reachability check before conceding UNAVAILABLE.
        if await _confirm_cycle_via_reachability(project_path, blocked_bead_id, blocker_bead_id):
            _logger.warning(
                "beads_add_blocking_dependency_cycle_conflict_reachability",
                project_path=str(project_path),
                blocked=blocked_bead_id,
                blocker=blocker_bead_id,
                error=str(exc),
            )
            return BlockingDependencyOutcome.CYCLE_CONFLICT
        _logger.warning(
            "beads_add_blocking_dependency_failed",
            project_path=str(project_path),
            blocked=blocked_bead_id,
            blocker=blocker_bead_id,
            error=str(exc),
        )
        return BlockingDependencyOutcome.UNAVAILABLE
    return BlockingDependencyOutcome.LINKED
