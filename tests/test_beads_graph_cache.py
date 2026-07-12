"""Tests for the beads graph snapshot cache + request coalescing (``load_graph``).

Covers: TTL-fresh cache hits, ``max_age_seconds=0.0`` forcing a live read,
TTL expiry triggering a re-fetch, concurrent callers coalescing onto one
subprocess, a failed read never poisoning the cache, and a successful
mutation through ``bd()`` invalidating the cached graph for that cwd.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agentshore import beads as beads_module
from agentshore.beads import BdError, GraphReadError, bd, load_graph


@pytest.fixture(autouse=True)
def _clear_graph_cache() -> None:
    """Isolate each test from cross-test cache/coalescing state.

    Keys are per-tmp_path so collisions across tests are unlikely anyway,
    but clearing keeps intent explicit and avoids unbounded dict growth
    across the module's test session.
    """
    beads_module._graph_cache.clear()
    beads_module._graph_load_tasks.clear()
    yield
    beads_module._graph_cache.clear()
    beads_module._graph_load_tasks.clear()


def _beads_json(*, title: str = "Task") -> str:
    return json.dumps([{"id": "t-1", "title": title, "type": "task", "status": "open"}])


@pytest.mark.asyncio
async def test_load_graph_serves_cached_result_within_ttl(tmp_path: Path) -> None:
    (tmp_path / ".beads").mkdir()
    call_count = 0

    async def _fake_bd(*args: str, cwd: object, **kwargs: object) -> str:
        nonlocal call_count
        call_count += 1
        return _beads_json()

    with patch("agentshore.beads.bd", new=_fake_bd):
        first = await load_graph(tmp_path)
        second = await load_graph(tmp_path)

    assert call_count == 1, "a second call within the TTL must reuse the cached graph"
    assert first is not None
    assert second is first


@pytest.mark.asyncio
async def test_load_graph_max_age_zero_forces_live_read(tmp_path: Path) -> None:
    (tmp_path / ".beads").mkdir()
    call_count = 0

    async def _fake_bd(*args: str, cwd: object, **kwargs: object) -> str:
        nonlocal call_count
        call_count += 1
        return _beads_json()

    with patch("agentshore.beads.bd", new=_fake_bd):
        await load_graph(tmp_path)
        await load_graph(tmp_path, max_age_seconds=0.0)

    assert call_count == 2, "max_age_seconds=0.0 must bypass the cache"


@pytest.mark.asyncio
async def test_load_graph_refetches_after_ttl_expiry(tmp_path: Path) -> None:
    (tmp_path / ".beads").mkdir()
    call_count = 0

    async def _fake_bd(*args: str, cwd: object, **kwargs: object) -> str:
        nonlocal call_count
        call_count += 1
        return _beads_json()

    fake_clock = [1_000.0]

    def _fake_monotonic() -> float:
        return fake_clock[0]

    with (
        patch("agentshore.beads.bd", new=_fake_bd),
        patch("agentshore.beads.time.monotonic", new=_fake_monotonic),
    ):
        await load_graph(tmp_path)
        fake_clock[0] += beads_module._GRAPH_CACHE_TTL_SECONDS + 0.1
        await load_graph(tmp_path)

    assert call_count == 2, "a call after TTL expiry must re-fetch rather than serve stale data"


@pytest.mark.asyncio
async def test_load_graph_coalesces_concurrent_callers(tmp_path: Path) -> None:
    """Two concurrent load_graph() calls for the same path share one subprocess result."""
    (tmp_path / ".beads").mkdir()
    call_count = 0

    async def _fake_bd(*args: str, cwd: object, **kwargs: object) -> str:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.02)
        return _beads_json()

    with patch("agentshore.beads.bd", new=_fake_bd):
        first, second = await asyncio.gather(
            load_graph(tmp_path),
            load_graph(tmp_path),
        )

    assert call_count == 1, "concurrent callers must coalesce onto one subprocess"
    assert first is second


@pytest.mark.asyncio
async def test_load_graph_does_not_cache_a_failed_read(tmp_path: Path) -> None:
    (tmp_path / ".beads").mkdir()

    async def _failing_bd(*args: str, cwd: object, **kwargs: object) -> str:
        raise BdError("bd unavailable")

    with (
        patch("agentshore.beads.bd", new=_failing_bd),
        pytest.raises(GraphReadError),
    ):
        await load_graph(tmp_path)

    call_count = 0

    async def _fake_bd(*args: str, cwd: object, **kwargs: object) -> str:
        nonlocal call_count
        call_count += 1
        return _beads_json()

    with patch("agentshore.beads.bd", new=_fake_bd):
        result = await load_graph(tmp_path)

    assert result is not None
    assert call_count == 1, "a prior GraphReadError must not poison the cache with stale data"


@pytest.mark.asyncio
async def test_bd_mutation_invalidates_graph_cache(tmp_path: Path) -> None:
    """A successful mutation run through the real ``bd()`` drops the cached graph."""
    (tmp_path / ".beads").mkdir()
    read_call_count = 0

    async def _fake_read_bd(*args: str, cwd: object, **kwargs: object) -> str:
        nonlocal read_call_count
        read_call_count += 1
        return _beads_json()

    async def _fake_run_command(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("agentshore.beads.bd", new=_fake_read_bd):
        await load_graph(tmp_path)
    assert read_call_count == 1

    with (
        patch("agentshore.beads.resolve_bd_binary", return_value="bd"),
        patch("agentshore.beads.run_command", new=_fake_run_command),
    ):
        await bd("update", "t-1", "--status", "closed", cwd=tmp_path)

    with patch("agentshore.beads.bd", new=_fake_read_bd):
        await load_graph(tmp_path)

    assert read_call_count == 2, "a mutation must invalidate the cached graph for its cwd"


@pytest.mark.asyncio
async def test_bd_read_command_does_not_invalidate_graph_cache(tmp_path: Path) -> None:
    """A read command run through the real ``bd()`` leaves the cached graph intact."""
    (tmp_path / ".beads").mkdir()
    read_call_count = 0

    async def _fake_read_bd(*args: str, cwd: object, **kwargs: object) -> str:
        nonlocal read_call_count
        read_call_count += 1
        return _beads_json()

    async def _fake_run_command(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    with patch("agentshore.beads.bd", new=_fake_read_bd):
        await load_graph(tmp_path)
    assert read_call_count == 1

    with (
        patch("agentshore.beads.resolve_bd_binary", return_value="bd"),
        patch("agentshore.beads.run_command", new=_fake_run_command),
    ):
        await bd("query", "status=open", "--json", cwd=tmp_path)

    with patch("agentshore.beads.bd", new=_fake_read_bd):
        await load_graph(tmp_path)

    assert read_call_count == 1, "a read command must not invalidate the cached graph"
