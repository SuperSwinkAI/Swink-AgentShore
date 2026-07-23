"""JSON boundary parsing: narrow raw bd JSON into typed dataclasses.

``RawBead``/``RawDependency``/``RawEpicNested``/``RawEpicStatus`` describe the
untyped shape bd emits over ``--json``; every other function here converts
that shape into ``Bead``/``EpicStatus`` so ``Any`` doesn't leak into
downstream code.
"""

from __future__ import annotations

import contextlib
import json
from typing import TypedDict, cast

from agentshore.beads.types import Bead, BeadStatus, BeadType, EpicStatus
from agentshore.logging import get_logger

_logger = get_logger(__name__)


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


def _parse_bead(raw: RawBead) -> Bead:
    """Parse a single bead JSON dict into a typed Bead."""
    raw_type = raw.get("type") or raw.get("issue_type") or "task"
    raw_status = raw.get("status", "open")
    bead_id = raw.get("id") or raw.get("bead_id") or ""
    try:
        bead_type = BeadType(raw_type)
    except ValueError:
        _logger.warning("beads_unknown_bead_type", bead_id=bead_id, raw_value=raw_type)
        bead_type = BeadType.TASK
    try:
        bead_status = BeadStatus(raw_status)
    except ValueError:
        _logger.warning("beads_unknown_bead_status", bead_id=bead_id, raw_value=raw_status)
        bead_status = BeadStatus.OPEN
    return Bead(
        bead_id=bead_id,
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
