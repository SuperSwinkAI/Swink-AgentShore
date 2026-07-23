"""Domain enums and dataclasses for the beads project graph.

``Bead`` is a single graph node as read from bd; ``EpicStatus``/``GraphTask``/
``ProjectGraph`` are the aggregated, dashboard- and RL-facing views built by
``agentshore.beads.graph``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


class BeadType(StrEnum):
    EPIC = "epic"
    STORY = "story"
    TASK = "task"
    BUG = "bug"
    FEATURE = "feature"
    CHORE = "chore"
    DECISION = "decision"


class BeadStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DEFERRED = "deferred"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class Bead:
    """A single node in the beads project graph."""

    bead_id: str
    title: str
    bead_type: BeadType
    status: BeadStatus
    priority: int | None = None
    parent_id: str | None = None
    external_ref: str | None = None
    assignee: str | None = None
    description: str | None = None
    closed_at: str | None = None
    updated_at: str | None = None
    depends_on_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class EpicStatus:
    """Closure-ratio snapshot for a single epic."""

    bead_id: str
    title: str
    total_tasks: int
    closed_tasks: int
    closure_ratio: float


@dataclass(frozen=True, slots=True)
class GraphTask:
    """Dashboard-facing task node from the beads graph."""

    bead_id: str
    title: str
    status: BeadStatus
    parent_id: str | None = None
    epic_id: str | None = None
    epic_title: str | None = None
    external_ref: str | None = None
    issue_number: int | None = None
    ready: bool = False
    depends_on_ids: frozenset[str] = frozenset()
    blocked_by_ids: frozenset[str] = frozenset()
    closed_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectGraph:
    """Aggregate view of the beads graph loaded each orchestrator tick.

    ``has_ready_tasks`` gates issue_pickup preconditions. ``global_closure_ratio``
    and the per-epic list drive the live alignment delta in executor.py.
    """

    epics: list[EpicStatus] = field(default_factory=list)
    tasks: list[GraphTask] = field(default_factory=list)
    tasks_ready: int = 0
    tasks_blocked: int = 0
    tasks_total: int = 0
    global_closure_ratio: float = 0.0

    @property
    def has_ready_tasks(self) -> bool:
        return self.tasks_ready > 0

    @property
    def has_epics(self) -> bool:
        return len(self.epics) > 0


# Preference order for picking the most-actionable bead when several point at
# the same GitHub issue (duplicate-bead routing). OPEN beats anything else;
# CLOSED is the last resort. Used by ``pick_bead_for_issue`` below.
_BEAD_STATUS_PREFERENCE: tuple[BeadStatus, ...] = (
    BeadStatus.OPEN,
    BeadStatus.IN_PROGRESS,
    BeadStatus.BLOCKED,
    BeadStatus.DEFERRED,
    BeadStatus.CLOSED,
)


def pick_bead_for_issue(
    tasks: Iterable[GraphTask],
    issue_number: int,
) -> GraphTask | None:
    """Return the most-actionable bead for ``issue_number``, or None.

    Why: an interrupted audit/import can leave more than one bead pointing
    at the same gh-N. Naive ``next()`` lookups then pick whichever bead
    sorts first, which can be a CLOSED duplicate — permanently locking
    dispatch on the issue even though a live OPEN bead exists. This helper
    picks by ``_BEAD_STATUS_PREFERENCE`` so the OPEN bead wins.
    """
    matches = [t for t in tasks if t.issue_number == issue_number]
    if not matches:
        return None
    return min(matches, key=lambda t: _BEAD_STATUS_PREFERENCE.index(t.status))
