"""Beads integration — foundation types and direct subprocess helpers.

Beads (bd) is the project-graph store: epics own stories own tasks. This
module provides the typed dataclasses and async helpers that the rest of
AgentShore uses to read and write the bead graph. All calls shell out to the
`bd` binary via asyncio subprocesses; there is no wrapper class.

Three-layer architecture:
  BEADS   — project graph (this module talks to it)
  GITHUB  — human-facing issues (mirrored via external_ref = "gh-N")
  SQLITE  — session-scoped RL state (plays, experience, agents)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from collections.abc import Iterable

from agentshore.command import CommandTimeoutError, run_command
from agentshore.logging import get_logger

# Boundary types: narrow bd JSON at the parse boundary so ``Any`` doesn't leak
# into typed downstream code.


class RawDependency(TypedDict, total=False):
    """One entry under a bead's ``dependencies`` list."""

    type: str
    dependency_type: str
    depends_on_id: str
    parent_id: str
    parent: str
    from_id: str
    source_id: str


class RawBead(TypedDict, total=False):
    """Shape of a bead JSON dict as emitted by ``bd list`` / ``bd query``."""

    id: str
    bead_id: str
    title: str
    name: str
    summary: str
    type: str
    issue_type: str
    status: str
    priority: int
    parent_id: str
    parent: str
    external_ref: str
    assignee: str
    description: str
    closed_at: str
    updated_at: str
    dependencies: list[RawDependency]


class RawEpicNested(TypedDict, total=False):
    """Nested ``epic`` object on a ``bd epic-status`` JSON dict."""

    id: str
    epic_id: str
    bead_id: str
    title: str
    name: str
    summary: str
    epic_title: str


class RawEpicStatus(TypedDict, total=False):
    """Shape of a ``bd epic-status`` JSON dict."""

    id: str
    epic_id: str
    bead_id: str
    title: str
    name: str
    summary: str
    epic_title: str
    total_children: int
    total: int
    closed_children: int
    closed: int
    epic: RawEpicNested


_logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level lock — serialises all bd subprocess calls so that concurrent
# plays (e.g. groom_backlog + calibrate_alignment, or two issue_pickups) do
# not race at the bd filesystem layer (C5).
# ---------------------------------------------------------------------------

_BD_LOCK: asyncio.Lock = asyncio.Lock()
_BD_TIMEOUT_SECONDS = 120.0
# The full-graph dump (``bd list --all --json --limit 0``) is O(graph size) and
# legitimately needs more headroom than a point mutation like ``bd close`` on a
# large beads graph. Keep mutations at the tight 120s ceiling; give the dump its
# own larger budget so a big-but-completable graph succeeds instead of timing out
# (#237).
_BD_GRAPH_TIMEOUT_SECONDS = 300.0


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Core subprocess helper
# ---------------------------------------------------------------------------


class BdError(RuntimeError):
    """Raised when a bd subcommand exits with a non-zero return code."""


class BdTimeoutError(BdError):
    """Raised when a bd subcommand exceeds its timeout.

    Distinct from the generic ``BdError`` so callers can tell a "too big / too
    slow" timeout apart from a transient, retry-worthy failure (e.g. lock
    contention). Retrying a timeout cannot help — the command was already given
    its full budget — so the graph reader fails fast on this rather than
    re-paying the timeout N times (#237).
    """


class GraphReadError(BdError):
    """Raised when load_graph exhausts all retries and cannot return a fresh graph.

    Callers must handle this explicitly; returning stale data silently is not
    acceptable because it hides permanent failures (uninstalled bd binary,
    corrupted store, wedged lock) from the RL loop.
    """


def resolve_bd_binary() -> str | None:
    """Resolve the bd binary path from env override first, then PATH."""
    env_value = os.environ.get("AGENTSHORE_BD_BIN")
    if env_value:
        env_path = Path(env_value)
        if env_path.is_file() and os.access(env_path, os.X_OK):
            return str(env_path.resolve())
        _logger.warning("agentshore_bd_bin_invalid", env_path=env_value)
    return shutil.which("bd")


async def bd(
    *args: str,
    cwd: Path,
    stdin_data: bytes | None = None,
    timeout_seconds: float = _BD_TIMEOUT_SECONDS,
) -> str:
    """Run a bd subcommand in *cwd* and return stdout as a string.

    Raises ``BdTimeoutError`` when the command exceeds *timeout_seconds* and
    ``BdError`` on any other failure (non-zero exit, OSError, missing binary).

    All calls are serialised through ``_BD_LOCK`` (C5) to avoid concurrent
    plays racing at the bd filesystem layer.
    """
    bd_binary = resolve_bd_binary()
    if bd_binary is None:
        raise BdError("bd binary not found; set AGENTSHORE_BD_BIN or install bd on PATH")

    async with _BD_LOCK:
        try:
            result = await run_command(
                bd_binary,
                *args,
                cwd=cwd,
                stdin_data=stdin_data,
                timeout_seconds=timeout_seconds,
                resolve_executable=False,
            )
        except CommandTimeoutError as exc:
            raise BdTimeoutError(f"bd {' '.join(args)} timed out: {exc}") from exc
        except OSError as exc:
            raise BdError(f"bd {' '.join(args)} failed: {exc}") from exc
    if result.returncode != 0:
        raise BdError(
            f"bd {' '.join(args)} failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


# ---------------------------------------------------------------------------
# Graph loading
# ---------------------------------------------------------------------------


def _parse_bead(raw: RawBead) -> Bead:
    """Parse a single bead JSON dict into a typed Bead."""
    raw_type = raw.get("type") or raw.get("issue_type") or "task"
    raw_status = raw.get("status", "open")
    try:
        bead_type = BeadType(raw_type)
    except ValueError:
        bead_type = BeadType.TASK
    try:
        bead_status = BeadStatus(raw_status)
    except ValueError:
        bead_status = BeadStatus.OPEN
    return Bead(
        bead_id=raw.get("id") or raw.get("bead_id") or "",
        title=_title_from_raw(raw),
        bead_type=bead_type,
        status=bead_status,
        priority=raw.get("priority"),
        parent_id=_parent_id_from_raw(raw),
        external_ref=raw.get("external_ref"),
        assignee=raw.get("assignee"),
        description=raw.get("description"),
        closed_at=raw.get("closed_at"),
        updated_at=raw.get("updated_at"),
        depends_on_ids=_depends_on_ids_from_raw(raw),
    )


def _title_from_raw(raw: RawBead) -> str:
    for value in (raw.get("title"), raw.get("name"), raw.get("summary")):
        if not isinstance(value, str):
            continue
        title = value.strip()
        if title:
            return title
    return ""


def _id_from_value(value: object) -> str | None:
    if isinstance(value, str):
        candidate = value.strip()
        return candidate or None
    if isinstance(value, dict):
        for key in ("id", "bead_id", "issue_id"):
            nested_candidate = _id_from_value(value.get(key))
            if nested_candidate is not None:
                return nested_candidate
    return None


def _parent_id_from_raw(raw: RawBead) -> str | None:
    for parent_value in (raw.get("parent_id"), raw.get("parent")):
        candidate = _id_from_value(parent_value)
        if candidate is not None:
            return candidate

    dependencies = raw.get("dependencies")
    if not isinstance(dependencies, list):
        return None

    bead_id = _id_from_value(raw.get("id") or raw.get("bead_id"))
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            continue
        dependency_type = (
            dependency.get("type") or dependency.get("dependency_type") or ""
        ).strip()
        if dependency_type != "parent-child":
            continue
        for dep_value in (
            dependency.get("depends_on_id"),
            dependency.get("parent_id"),
            dependency.get("parent"),
            dependency.get("from_id"),
            dependency.get("source_id"),
        ):
            candidate = _id_from_value(dep_value)
            if candidate is not None and candidate != bead_id:
                return candidate
    return None


# Dependency-relationship types (bd CLI: blocks | tracks | related |
# parent-child | discovered-from) that represent a true *blocking* edge — the
# bead is not ready until the referenced bead closes. bd emits ``blocks`` for
# `bd dep add` / `bd link` (its default). ``parent-child`` is containment (a
# rollup, handled by ``_parent_id_from_raw``) and must NOT block; ``tracks``,
# ``related`` and ``discovered-from`` are informational and also non-blocking.
#
# Recognising ``blocks`` here keeps this parser's readiness view consistent
# with bd's own ``bd ready`` (which treats ``blocks`` as blocking).
_BLOCKING_DEPENDENCY_TYPES: frozenset[str] = frozenset({"blocks"})


def _depends_on_ids_from_raw(raw: RawBead) -> frozenset[str]:
    """Extract blocking-dependency IDs (``blocks``) from a bead."""
    dependencies = raw.get("dependencies")
    if not isinstance(dependencies, list):
        return frozenset()
    bead_id = _id_from_value(raw.get("id") or raw.get("bead_id"))
    result: set[str] = set()
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            continue
        dep_type = (dependency.get("type") or dependency.get("dependency_type") or "").strip()
        if dep_type not in _BLOCKING_DEPENDENCY_TYPES:
            continue
        for dep_value in (
            dependency.get("depends_on_id"),
            dependency.get("from_id"),
            dependency.get("source_id"),
        ):
            candidate = _id_from_value(dep_value)
            if candidate is not None and candidate != bead_id:
                result.add(candidate)
    return frozenset(result)


def _parse_epic_status(raw: RawEpicStatus) -> EpicStatus:
    """Parse a single bd epic-status JSON dict into a typed EpicStatus."""
    epic_raw = raw.get("epic")
    epic: RawEpicNested = epic_raw if isinstance(epic_raw, dict) else cast("RawEpicNested", {})
    total = int(raw.get("total_children", 0) or raw.get("total", 0) or 0)
    closed = int(raw.get("closed_children", 0) or raw.get("closed", 0) or 0)
    ratio = closed / total if total > 0 else 0.0
    bead_id = (
        raw.get("id")
        or raw.get("epic_id")
        or raw.get("bead_id")
        or epic.get("id")
        or epic.get("epic_id")
        or epic.get("bead_id")
        or ""
    )
    title = bead_id
    for candidate in (
        raw.get("title"),
        raw.get("name"),
        raw.get("summary"),
        raw.get("epic_title"),
        epic.get("title"),
        epic.get("name"),
        epic.get("summary"),
        epic.get("epic_title"),
    ):
        if not isinstance(candidate, str):
            continue
        candidate_title = candidate.strip()
        if candidate_title:
            title = candidate_title
            break
    return EpicStatus(
        bead_id=bead_id,
        title=title,
        total_tasks=total,
        closed_tasks=closed,
        closure_ratio=ratio,
    )


def _issue_number_from_external_ref(external_ref: str | None) -> int | None:
    if not external_ref or not external_ref.startswith("gh-"):
        return None
    with contextlib.suppress(ValueError):
        return int(external_ref[3:])
    return None


def _as_json_list(raw: str) -> list[RawBead]:
    """Parse a ``bd ... --json`` response into a list of bead dicts.

    bd emits either a top-level list or an envelope object with the list
    under ``items``/``beads``/``tasks``/``stories``/``epics``. We narrow
    once here so callers get a typed ``list[RawBead]`` instead of ``Any``.
    """
    data: object = json.loads(raw) if raw.strip() else []
    items: list[object]
    if isinstance(data, dict):
        items = []
        for key in ("items", "beads", "tasks", "stories", "epics"):
            value = data.get(key)
            if isinstance(value, list):
                items = value
                break
    elif isinstance(data, list):
        items = data
    else:
        items = []
    return [cast("RawBead", item) for item in items if isinstance(item, dict)]


async def _query_beads(project_path: Path, query: str) -> list[Bead]:
    raw = await bd("query", query, "--json", cwd=project_path)
    return [_parse_bead(item) for item in _as_json_list(raw)]


def _resolve_epic_for_task(
    task: Bead,
    *,
    epics_by_id: dict[str, Bead],
    beads_by_id: dict[str, Bead],
) -> tuple[str | None, str | None]:
    parent_id = task.parent_id
    seen: set[str] = set()
    while parent_id is not None and parent_id not in seen:
        seen.add(parent_id)
        epic = epics_by_id.get(parent_id)
        if epic is not None:
            return epic.bead_id, epic.title or epic.bead_id
        parent = beads_by_id.get(parent_id)
        if parent is None:
            break
        parent_id = parent.parent_id
    return None, None


def _graph_task_from_bead(
    bead: Bead,
    *,
    epics_by_id: dict[str, Bead],
    beads_by_id: dict[str, Bead],
) -> GraphTask:
    epic_id, epic_title = _resolve_epic_for_task(
        bead,
        epics_by_id=epics_by_id,
        beads_by_id=beads_by_id,
    )
    blocked_by = frozenset(
        dep_id
        for dep_id in bead.depends_on_ids
        if dep_id in beads_by_id and beads_by_id[dep_id].status != BeadStatus.CLOSED
    )
    return GraphTask(
        bead_id=bead.bead_id,
        title=bead.title,
        status=bead.status,
        parent_id=bead.parent_id,
        epic_id=epic_id,
        epic_title=epic_title,
        external_ref=bead.external_ref,
        issue_number=_issue_number_from_external_ref(bead.external_ref),
        ready=bead.status == BeadStatus.OPEN and not blocked_by,
        depends_on_ids=bead.depends_on_ids,
        blocked_by_ids=blocked_by,
        closed_at=bead.closed_at,
        updated_at=bead.updated_at,
    )


_GRAPH_READ_RETRIES = 3
_GRAPH_READ_RETRY_DELAY = 0.5  # seconds between attempts


async def _read_graph_raw(project_path: Path) -> list[RawBead]:
    """Run ``bd list --all --json`` with retries, returning the raw bead list.

    Raises ``GraphReadError`` after exhausting all retries.  This ensures
    callers cannot silently consume stale data — a persistent failure surfaces
    immediately rather than being hidden behind a fallback cache.

    A *timeout* (``BdTimeoutError``) is not retried: the command already ran for
    its full ``_BD_GRAPH_TIMEOUT_SECONDS`` budget, so re-running it would just
    re-pay that cost N times (the #237 360s = 3×120s pathology). Only transient
    failures (lock contention, a parse blip) are worth a retry.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _GRAPH_READ_RETRIES + 1):
        try:
            raw = await bd(
                "list",
                "--all",
                "--json",
                "--limit",
                "0",
                cwd=project_path,
                timeout_seconds=_BD_GRAPH_TIMEOUT_SECONDS,
            )
            return _as_json_list(raw)
        except BdTimeoutError as exc:
            # Fail fast — a timeout means "too big to dump in budget", not a
            # transient blip; retrying only multiplies the wasted wall-clock.
            _logger.warning(
                "beads_graph_load_timed_out",
                project_path=str(project_path),
                timeout_seconds=_BD_GRAPH_TIMEOUT_SECONDS,
                error=str(exc),
            )
            raise GraphReadError(
                f"bd list timed out after {_BD_GRAPH_TIMEOUT_SECONDS}s for {project_path}"
            ) from exc
        except BdError as exc:
            last_exc = exc
            _logger.warning(
                "beads_graph_load_failed",
                project_path=str(project_path),
                attempt=attempt,
                max_attempts=_GRAPH_READ_RETRIES,
                error=str(exc),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            last_exc = exc
            _logger.warning(
                "beads_graph_parse_failed",
                project_path=str(project_path),
                attempt=attempt,
                max_attempts=_GRAPH_READ_RETRIES,
                error=str(exc),
            )
        if attempt < _GRAPH_READ_RETRIES:
            await asyncio.sleep(_GRAPH_READ_RETRY_DELAY)

    raise GraphReadError(
        f"bd list failed after {_GRAPH_READ_RETRIES} attempts for {project_path}"
    ) from last_exc


async def load_graph(project_path: Path) -> ProjectGraph | None:
    """Load the beads project graph for *project_path*.

    Returns ``None`` when beads is not initialised for the project
    (no ``.beads/`` directory). Returns an empty ``ProjectGraph`` when
    beads is present but has no epics yet.

    Raises ``GraphReadError`` if the bd binary fails after all retries.
    Callers must handle this explicitly — silent stale-graph fallback is
    not acceptable because it hides permanent failures from the RL loop.
    """
    if not (project_path / ".beads").exists():
        return None

    bead_items = await _read_graph_raw(project_path)

    beads = [_parse_bead(item) for item in bead_items]
    beads_by_id = {bead.bead_id: bead for bead in beads if bead.bead_id}
    epic_beads = [bead for bead in beads if bead.bead_type == BeadType.EPIC]
    task_beads = [bead for bead in beads if bead.bead_type == BeadType.TASK]
    epics_by_id = {epic.bead_id: epic for epic in epic_beads if epic.bead_id}

    total_by_epic = {epic.bead_id: 0 for epic in epic_beads}
    closed_by_epic = {epic.bead_id: 0 for epic in epic_beads}
    for task in task_beads:
        epic_id, _ = _resolve_epic_for_task(
            task,
            epics_by_id=epics_by_id,
            beads_by_id=beads_by_id,
        )
        if epic_id is None:
            continue
        total_by_epic[epic_id] = total_by_epic.get(epic_id, 0) + 1
        if task.status == BeadStatus.CLOSED:
            closed_by_epic[epic_id] = closed_by_epic.get(epic_id, 0) + 1

    epics = [
        EpicStatus(
            bead_id=epic.bead_id,
            title=epic.title or epic.bead_id,
            total_tasks=total_by_epic.get(epic.bead_id, 0),
            closed_tasks=closed_by_epic.get(epic.bead_id, 0),
            closure_ratio=(
                closed_by_epic.get(epic.bead_id, 0) / total_by_epic[epic.bead_id]
                if total_by_epic.get(epic.bead_id, 0) > 0
                else 0.0
            ),
        )
        for epic in epic_beads
    ]
    tasks = [
        _graph_task_from_bead(
            task,
            epics_by_id=epics_by_id,
            beads_by_id=beads_by_id,
        )
        for task in task_beads
    ]

    total_tasks = len(task_beads)
    closed_tasks = sum(1 for task in task_beads if task.status == BeadStatus.CLOSED)
    global_ratio = closed_tasks / total_tasks if total_tasks > 0 else 0.0
    tasks_ready = sum(1 for task in tasks if task.ready)
    tasks_blocked = sum(1 for task in tasks if task.blocked_by_ids)

    return ProjectGraph(
        epics=epics,
        tasks=tasks,
        tasks_ready=tasks_ready,
        tasks_blocked=tasks_blocked,
        tasks_total=total_tasks,
        global_closure_ratio=global_ratio,
    )


# ---------------------------------------------------------------------------
# Ready-task enumeration
# ---------------------------------------------------------------------------


async def ready_tasks(project_path: Path) -> list[Bead]:
    """Return open tasks from the beads graph.

    Uses ``bd query`` to find open tasks. The caller is responsible for
    filtering further (e.g., by ``external_ref`` to restrict to
    GH-mirrored tasks).

    Returns an empty list when beads is not initialised or the query fails.
    """
    if not (project_path / ".beads").exists():
        return []
    try:
        raw = await bd("query", "status=open type=task", "--json", cwd=project_path)
        return [_parse_bead(item) for item in _as_json_list(raw)]
    except (BdError, json.JSONDecodeError, ValueError):
        return []


# ---------------------------------------------------------------------------
# Session-scoped progress cleanup
# ---------------------------------------------------------------------------


async def clear_in_progress_beads(project_path: Path) -> int:
    """Reset session-scoped ``in_progress`` beads to ``open``.

    AgentShore treats beads ``in_progress`` as an external mirror of active work,
    not as a durable lock. A crashed or stopped session can leave the status
    behind and block future issue pickup, so lifecycle boundaries clear it.

    Returns the number of beads successfully reset. Failures are logged and do
    not abort the caller's session startup or shutdown path.
    """
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


# ---------------------------------------------------------------------------
# Graph mutation
# ---------------------------------------------------------------------------


async def add_blocking_dependency(
    project_path: Path,
    blocked_bead_id: str,
    blocker_bead_id: str,
) -> bool:
    """Add a ``blocks`` edge so *blocked_bead_id* is blocked by *blocker_bead_id*.

    Mirrors a body-declared ``depends on #N`` dependency into the beads graph
    the moment ``issue_pickup`` discovers it at execute time, so the candidate
    mask excludes the dependent issue before another agent is dispatched (#14)
    rather than waiting for the next ``groom_backlog`` pass to mirror it.

    Runs ``bd link <blocked> <blocker> --type blocks`` (id2 blocks id1, so the
    second arg is the blocker — matching the groom/seed skills and bd's own
    ``bd link`` semantics). No-ops and returns ``False`` when beads is not
    initialised, either id is empty, or the two ids are equal. Returns ``False``
    on a bd error so the caller can fall back to a label gate; the call is
    swallowed rather than raised so a beads hiccup never fails the play.
    """
    if not (project_path / ".beads").exists():
        return False
    if not blocked_bead_id or not blocker_bead_id or blocked_bead_id == blocker_bead_id:
        return False
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
        _logger.warning(
            "beads_add_blocking_dependency_failed",
            project_path=str(project_path),
            blocked=blocked_bead_id,
            blocker=blocker_bead_id,
            error=str(exc),
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Persistent memory helpers
# ---------------------------------------------------------------------------


async def remember(project_path: Path, key: str, value: str) -> None:
    """Store a persistent memory under *key* in the beads kv store.

    Uses ``bd kv set``. Silently no-ops if beads is not initialised.
    """
    if not (project_path / ".beads").exists():
        return
    with contextlib.suppress(BdError):
        await bd("kv", "set", key, value, "--dolt-auto-commit=on", cwd=project_path)
