"""Graph loading: bd queries, epic/task aggregation, and the snapshot cache.

``load_graph`` is the main entry point — it loads (or serves a cached)
``ProjectGraph`` for a project, retrying transient ``bd list`` failures and
raising ``GraphReadError``/``BeadsSchemaDriftError`` rather than ever
returning stale data silently. ``ready_tasks`` is a narrower ``bd query`` for
issue_pickup's candidate search.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentshore.beads.lock import (
    BdError,
    BdTimeoutError,
    BeadsSchemaDriftError,
    GraphReadError,
    is_schema_drift_error,
)
from agentshore.beads.parsing import (
    RawBead,
    _as_json_list,
    _issue_number_from_external_ref,
    _parse_bead,
)
from agentshore.beads.types import Bead, BeadStatus, BeadType, EpicStatus, GraphTask, ProjectGraph
from agentshore.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

_logger = get_logger(__name__)


async def _query_beads(project_path: Path, query: str) -> list[Bead]:
    from agentshore.beads import bd  # noqa: PLC0415

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

# The full-graph dump (``bd list --all --json --limit 0``) is O(graph size) and
# legitimately needs more headroom than a point mutation like ``bd close`` on a
# large beads graph. Keep mutations at bd()'s tight 120s ceiling; give the dump
# its own larger budget so a big-but-completable graph succeeds instead of
# timing out (#237).
_BD_GRAPH_TIMEOUT_SECONDS = 300.0


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
    from agentshore.beads import bd  # noqa: PLC0415

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
            if is_schema_drift_error(str(exc)):
                # Fail fast — same rationale as the BdTimeoutError branch above:
                # this is a structural refusal (bd's #4259 remote-migration
                # gate), not a transient blip, so retrying 3x just re-pays the
                # same deterministic failure. Raising a distinct exception type
                # (rather than falling through to the generic GraphReadError
                # below) lets callers tell "graph unreadable due to schema
                # drift" apart from "graph load failed" / "graph is empty" —
                # conflating those previously caused a bogus seed_project.
                _logger.warning(
                    "beads_graph_load_schema_drift",
                    project_path=str(project_path),
                    error=str(exc),
                )
                raise BeadsSchemaDriftError(
                    f"bd list refused due to schema drift for {project_path}: {exc}"
                ) from exc
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


# ---------------------------------------------------------------------------
# Graph snapshot cache + request coalescing
# ---------------------------------------------------------------------------
#
# ``load_graph`` runs at least once per orchestrator tick (build_state),
# again after every play completes (post-play alignment reload), again on
# the selector's live-drift confirm, and again on every issue_syncer full
# sync — each historically a fresh ``bd list --all --json --limit 0``
# subprocess behind the bd lock. A short TTL cache plus in-flight
# coalescing collapses concurrent/back-to-back callers onto one subprocess
# without hiding a genuinely stale graph for long: the callers that need a
# guaranteed-live read (selector confirm, post-play alignment — both drift
# checks right after a mutation) pass ``max_age_seconds=0.0`` to force one.

_GRAPH_CACHE_TTL_SECONDS = 2.0


@dataclass(slots=True)
class _GraphCacheEntry:
    graph: ProjectGraph | None
    loaded_at: float


_graph_cache: dict[Path, _GraphCacheEntry] = {}
_graph_cache_guard: asyncio.Lock = asyncio.Lock()
_graph_load_tasks: dict[Path, asyncio.Task[ProjectGraph | None]] = {}


async def _invalidate_graph_cache(project_path: Path) -> None:
    """Drop the cached graph for *project_path* after our own mutation.

    External agent processes also mutate the store and this cannot see
    those — that is exactly why the TTL is short and why the live-read
    callers force freshness instead of relying on invalidation alone.
    """
    async with _graph_cache_guard:
        _graph_cache.pop(project_path, None)


def _build_project_graph(bead_items: list[RawBead]) -> ProjectGraph:
    """Pure transformation: raw ``bd list`` JSON dicts -> aggregated ``ProjectGraph``."""
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


async def _load_and_cache_graph(project_path: Path) -> ProjectGraph | None:
    """Uncached full-dump read. Called at most once per coalesced group."""
    bead_items = await _read_graph_raw(project_path)
    graph = _build_project_graph(bead_items)
    async with _graph_cache_guard:
        _graph_cache[project_path] = _GraphCacheEntry(graph=graph, loaded_at=time.monotonic())
    return graph


async def load_graph(
    project_path: Path, *, max_age_seconds: float | None = None
) -> ProjectGraph | None:
    """Load the beads project graph for *project_path*.

    Returns ``None`` when beads is not initialised for the project
    (no ``.beads/`` directory). Returns an empty ``ProjectGraph`` when
    beads is present but has no epics yet.

    Raises ``GraphReadError`` if the bd binary fails after all retries.
    Callers must handle this explicitly — silent stale-graph fallback is
    not acceptable because it hides permanent failures from the RL loop.

    *max_age_seconds* controls freshness: ``None`` (the default) accepts a
    cached snapshot up to ``_GRAPH_CACHE_TTL_SECONDS`` old; ``0.0`` forces a
    live read, bypassing the cache (but still coalescing with a read
    already in flight for this path — a fresh reload here has to spawn a
    real ``bd list``, but two callers racing to force one only need to pay
    for it once). A failed read is never cached, so a persistent failure
    surfaces to every caller rather than being papered over by stale data.
    """
    if not (project_path / ".beads").exists():
        return None

    ttl = _GRAPH_CACHE_TTL_SECONDS if max_age_seconds is None else max_age_seconds

    async with _graph_cache_guard:
        if ttl > 0.0:
            entry = _graph_cache.get(project_path)
            if entry is not None and (time.monotonic() - entry.loaded_at) < ttl:
                return entry.graph
        shared_task = _graph_load_tasks.get(project_path)
        if shared_task is None:
            shared_task = asyncio.ensure_future(_load_and_cache_graph(project_path))
            _graph_load_tasks[project_path] = shared_task

    try:
        return await shared_task
    finally:
        async with _graph_cache_guard:
            if _graph_load_tasks.get(project_path) is shared_task:
                del _graph_load_tasks[project_path]


async def ready_tasks(project_path: Path) -> list[Bead]:
    """Return open tasks from the beads graph.

    Uses ``bd query`` to find open tasks. The caller is responsible for
    filtering further (e.g., by ``external_ref`` to restrict to
    GH-mirrored tasks).

    Returns an empty list when beads is not initialised or the query fails.

    Evaluated switching to bd 1.1.0's own ``bd ready --json --limit 0`` and
    rejected it: verified empirically against a live bd 1.1.0 store that
    ``bd ready --json`` does NOT include ``external_ref`` in its output
    (fields are id/title/status/priority/issue_type/owner/created_at/
    created_by/updated_at/dependency_count/dependent_count/comment_count
    only). The sole consumer of this function
    (``PlayCandidateService._issue_pickup_candidates``) matches beads to
    GitHub issues by ``external_ref``, so switching would silently zero out
    that GH-issue correlation on every call. Separately, this codebase's
    ``bd query "status=open type=task"`` plus the graph's client-side
    blocked-dependency filtering (``_depends_on_ids_from_raw`` /
    ``GraphTask.blocked_by_ids``, applied upstream of this function's
    result) already deliberately mirrors ``bd ready``'s blocking semantics
    (see the comment above ``_BLOCKING_DEPENDENCY_TYPES``), so there's no
    readiness-accuracy gain here to trade the external_ref away for.
    """
    from agentshore.beads import bd  # noqa: PLC0415

    if not (project_path / ".beads").exists():
        return []
    try:
        raw = await bd("query", "status=open type=task", "--json", cwd=project_path)
        return [_parse_bead(item) for item in _as_json_list(raw)]
    except (BdError, json.JSONDecodeError, ValueError):
        return []
