"""Tests for the version-proof cycle-detection fallback (#4).

``add_blocking_dependency``'s fast path matches the "would create a cycle"
substring bd 1.1.0 still emits. These tests cover the fallback that fires
when a ``bd link`` failure does NOT match that substring: a client-side BFS
reachability check over a force-freshly-reloaded graph, so an upstream
stderr wording change can't silently regress the needs-human escalation
(commit 78555b3) back to an ordinary, auto-clearable label gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agentshore.beads import (
    BdError,
    BeadStatus,
    BlockingDependencyOutcome,
    GraphTask,
    ProjectGraph,
    _would_create_cycle,
    add_blocking_dependency,
)


def _task(bead_id: str, *, depends_on: frozenset[str] = frozenset()) -> GraphTask:
    return GraphTask(
        bead_id=bead_id,
        title=bead_id,
        status=BeadStatus.OPEN,
        depends_on_ids=depends_on,
    )


# _would_create_cycle — pure BFS reachability


def test_would_create_cycle_direct_reverse_edge() -> None:
    """The blocker already depends_on the blocked bead (the opposite edge exists)."""
    graph = ProjectGraph(
        tasks=[
            _task("blocked"),
            _task("blocker", depends_on=frozenset({"blocked"})),
        ]
    )
    assert _would_create_cycle(graph, "blocked", "blocker") is True


def test_would_create_cycle_transitive_chain() -> None:
    """blocker -> mid -> blocked: adding blocked -> blocker closes the loop."""
    graph = ProjectGraph(
        tasks=[
            _task("blocked"),
            _task("mid", depends_on=frozenset({"blocked"})),
            _task("blocker", depends_on=frozenset({"mid"})),
        ]
    )
    assert _would_create_cycle(graph, "blocked", "blocker") is True


def test_would_create_cycle_returns_false_when_unrelated() -> None:
    graph = ProjectGraph(
        tasks=[
            _task("blocked"),
            _task("blocker"),
            _task("unrelated", depends_on=frozenset({"blocked"})),
        ]
    )
    assert _would_create_cycle(graph, "blocked", "blocker") is False


def test_would_create_cycle_returns_false_for_empty_graph() -> None:
    assert _would_create_cycle(ProjectGraph(), "blocked", "blocker") is False


# add_blocking_dependency — integration with the fallback


@pytest.mark.asyncio
async def test_fast_path_skips_reachability_check_entirely(tmp_path: Path) -> None:
    """A stderr match short-circuits — no extra graph reload is attempted."""
    (tmp_path / ".beads").mkdir()
    call_log: list[str] = []

    async def _fake_bd(*args: str, cwd: object, **kwargs: object) -> str:
        call_log.append(args[0])
        raise BdError("bd link failed (rc=1): Error: adding dependency would create a cycle")

    with patch("agentshore.beads.bd", new=_fake_bd):
        outcome = await add_blocking_dependency(tmp_path, "blocked", "blocker")

    assert outcome is BlockingDependencyOutcome.CYCLE_CONFLICT
    assert call_log == ["link"], "the fast path must not trigger the reachability fallback"


@pytest.mark.asyncio
async def test_fallback_confirms_cycle_on_reworded_bd_error(tmp_path: Path) -> None:
    """A reworded bd error still gets classified as CYCLE_CONFLICT via reachability."""
    (tmp_path / ".beads").mkdir()
    call_log: list[str] = []

    async def _fake_bd(*args: str, cwd: object, **kwargs: object) -> str:
        call_log.append(args[0])
        if args[0] == "link":
            raise BdError("bd link failed (rc=1): dependency graph rejects this edge")
        assert args[0] == "list", f"unexpected bd call: {args}"
        # blocker already depends on blocked -> direct reverse edge.
        return json.dumps(
            [
                {"id": "blocked", "title": "Blocked", "type": "task", "status": "open"},
                {
                    "id": "blocker",
                    "title": "Blocker",
                    "type": "task",
                    "status": "open",
                    "dependencies": [{"type": "blocks", "depends_on_id": "blocked"}],
                },
            ]
        )

    with patch("agentshore.beads.bd", new=_fake_bd):
        outcome = await add_blocking_dependency(tmp_path, "blocked", "blocker")

    assert outcome is BlockingDependencyOutcome.CYCLE_CONFLICT
    assert "list" in call_log, "the fallback must force a fresh graph reload"


@pytest.mark.asyncio
async def test_fallback_returns_unavailable_when_no_cycle_confirmed(tmp_path: Path) -> None:
    """A reworded, non-cycle bd error stays UNAVAILABLE when reachability finds no cycle."""
    (tmp_path / ".beads").mkdir()

    async def _fake_bd(*args: str, cwd: object, **kwargs: object) -> str:
        if args[0] == "link":
            raise BdError("bd link failed (rc=1): some other unrelated failure")
        return json.dumps(
            [
                {"id": "blocked", "title": "Blocked", "type": "task", "status": "open"},
                {"id": "blocker", "title": "Blocker", "type": "task", "status": "open"},
            ]
        )

    with patch("agentshore.beads.bd", new=_fake_bd):
        outcome = await add_blocking_dependency(tmp_path, "blocked", "blocker")

    assert outcome is BlockingDependencyOutcome.UNAVAILABLE


@pytest.mark.asyncio
async def test_fallback_returns_unavailable_when_reload_itself_fails(tmp_path: Path) -> None:
    """If the fallback's own graph reload fails, it can't confirm a cycle — stays UNAVAILABLE."""
    (tmp_path / ".beads").mkdir()

    async def _fake_bd(*args: str, cwd: object, **kwargs: object) -> str:
        raise BdError("bd unavailable")

    with patch("agentshore.beads.bd", new=_fake_bd):
        outcome = await add_blocking_dependency(tmp_path, "blocked", "blocker")

    assert outcome is BlockingDependencyOutcome.UNAVAILABLE
