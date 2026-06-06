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
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from collections.abc import Iterable

from agentshore.command import CommandTimeoutError, run_command
from agentshore.logging import get_logger

# ---------------------------------------------------------------------------
# Boundary types — shape of JSON emitted by ``bd list`` / ``bd query``.
# These exist so helpers below narrow at the parse boundary instead of
# letting ``Any`` propagate into typed AgentShore code downstream.
# ---------------------------------------------------------------------------


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


def managed_bd_dir() -> Path:
    r"""Canonical beads install directory.

    This is the *same* location beads' own ``install.ps1`` / ``install.sh`` use,
    so an AgentShore-provisioned ``bd`` is indistinguishable from a user's
    standalone beads install and lands on the conventional per-platform PATH:

      * Windows — ``%LOCALAPPDATA%\Programs\bd`` (matches ``install.ps1``).
      * macOS/Linux — ``/usr/local/bin`` when writable, else ``~/.local/bin``
        (matches ``install.sh``).

    Installing here (rather than a swink-private dir) means the binary is found
    by any tool that respects PATH — including the ``bd`` the agent subprocesses
    shell out to from the skills — not just by AgentShore's own absolute-path
    resolution.
    """
    if sys.platform.startswith("win"):
        local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local_app_data) / "Programs" / "bd"
    usr_local_bin = Path("/usr/local/bin")
    if os.access(usr_local_bin, os.W_OK):
        return usr_local_bin
    return Path.home() / ".local" / "bin"


def _managed_bd_path() -> Path:
    name = "bd.exe" if sys.platform.startswith("win") else "bd"
    return managed_bd_dir() / name


def ensure_bd_dir_on_path() -> None:
    """Prepend the beads install dir to this process's ``PATH`` (idempotent).

    Beads' own installers only *print* a PATH hint; they never modify PATH. So a
    freshly provisioned ``bd`` in the canonical dir may not be on the PATH this
    process inherited. Agent subprocesses inherit this process's environment, so
    prepending the dir here makes a bare ``bd`` (the form the skills invoke)
    resolve for every agent — without persisting any change to the user's shell.
    """
    bd_dir_path = managed_bd_dir()
    if not bd_dir_path.is_dir():
        # Nothing installed there yet — don't pollute PATH with a phantom dir.
        return
    bd_dir = str(bd_dir_path)
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    target = os.path.normcase(os.path.normpath(bd_dir))
    if any(os.path.normcase(os.path.normpath(p)) == target for p in parts if p):
        return
    os.environ["PATH"] = (bd_dir + os.pathsep + current) if current else bd_dir
    _logger.info("bd_dir_added_to_path", bd_dir=bd_dir)


def resolve_bd_binary() -> str | None:
    """Resolve the bd binary: env override, then PATH, then the managed dir.

    The managed dir (:func:`managed_bd_dir`) is where ``agentshore init``
    drops an auto-provisioned bd, so a fresh CLI/pip install finds it without
    any PATH change once init has run.
    """
    env_value = os.environ.get("AGENTSHORE_BD_BIN")
    if env_value:
        env_path = Path(env_value)
        if env_path.is_file() and os.access(env_path, os.X_OK):
            return str(env_path.resolve())
        _logger.warning("agentshore_bd_bin_invalid", env_path=env_value)
    on_path = shutil.which("bd")
    if on_path:
        return on_path
    managed = _managed_bd_path()
    if managed.is_file() and os.access(managed, os.X_OK):
        return str(managed.resolve())
    return None


async def bd(
    *args: str,
    cwd: Path,
    stdin_data: bytes | None = None,
) -> str:
    """Run a bd subcommand in *cwd* and return stdout as a string.

    Raises BdError on non-zero exit.

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
                timeout_seconds=_BD_TIMEOUT_SECONDS,
                resolve_executable=False,
            )
        except (CommandTimeoutError, OSError) as exc:
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
# `bd dep add` / `bd link` (its default); ``depends-on`` is kept for forward/
# back compatibility with other bd revisions. ``parent-child`` is containment
# (a rollup, handled by ``_parent_id_from_raw``) and must NOT block; ``tracks``,
# ``related`` and ``discovered-from`` are informational and also non-blocking.
#
# Recognising ``blocks`` here keeps this parser's readiness view consistent
# with bd's own ``bd ready`` (which treats ``blocks`` as blocking). Before this,
# the parser only matched ``depends-on`` — a string bd never emits — so every
# blocking edge was silently dropped and genuinely-blocked tasks parsed as
# ready, disagreeing with bd and the dispatch path.
_BLOCKING_DEPENDENCY_TYPES: frozenset[str] = frozenset({"depends-on", "blocks"})


def _depends_on_ids_from_raw(raw: RawBead) -> frozenset[str]:
    """Extract blocking-dependency IDs (``blocks`` / ``depends-on``) from a bead."""
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


async def load_graph(project_path: Path) -> ProjectGraph | None:
    """Load the beads project graph for *project_path*.

    Returns ``None`` when beads is not initialised for the project
    (no ``.beads/`` directory). Returns an empty ``ProjectGraph`` when
    beads is present but has no epics yet.
    """
    if not (project_path / ".beads").exists():
        return None

    try:
        raw = await bd("list", "--all", "--json", "--limit", "0", cwd=project_path)
        bead_items = _as_json_list(raw)
    except BdError as exc:
        _logger.warning("beads_graph_load_failed", project_path=str(project_path), error=str(exc))
        return None
    except (json.JSONDecodeError, ValueError) as exc:
        _logger.warning("beads_graph_parse_failed", project_path=str(project_path), error=str(exc))
        return None

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
