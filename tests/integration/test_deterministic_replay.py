"""Integration tests for deterministic play-sequence replay."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agentshore.config import RuntimeConfig, SessionConfig
from agentshore.core import Orchestrator
from agentshore.data.store import DataStore, PlayRecord
from agentshore.plays.base import PlayParams
from agentshore.plays.selector import FixedPlanSelector
from agentshore.state import PlayOutcome, PlayType


def _make_outcome(
    play_type: PlayType,
    play_id: int,
    dollar_cost: float = 0.01,
) -> PlayOutcome:
    return PlayOutcome(
        play_type=play_type,
        agent_id="agent-1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=100,
        dollar_cost=dollar_cost,
        artifacts=[],
        alignment_delta=0.05,
        play_id=play_id,
    )


def _build_mock_execute(
    results: list[PlayType],
    store: DataStore,
    session_id: str,
) -> object:
    """Build a mock execute coroutine that is properly bound to its store/session."""
    idx = 0

    async def mock_execute(
        play_type: PlayType,
        state: object,
        *,
        override: PlayParams | None = None,
    ) -> PlayOutcome:
        nonlocal idx
        idx += 1
        results.append(play_type)
        await store.record_play(
            PlayRecord(
                session_id=session_id,
                play_type=play_type.value,
                started_at="2026-01-01T00:00:00+00:00",
                success=True,
                dollar_cost=0.01,
            )
        )
        return _make_outcome(play_type, play_id=idx)

    return mock_execute


async def _clear_cached_github_work(orch: Orchestrator) -> None:
    await orch._store._conn.execute(  # type: ignore[union-attr]
        "DELETE FROM github_issues WHERE session_id = ?",
        (orch._session_id,),
    )
    await orch._store._conn.execute(  # type: ignore[union-attr]
        "DELETE FROM pull_requests WHERE session_id = ?",
        (orch._session_id,),
    )
    await orch._store._conn.commit()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_deterministic_replay(tmp_path: Path) -> None:
    """Same FixedPlanSelector plan produces identical play sequences across two runs."""
    plan = [
        (PlayType.ISSUE_PICKUP, PlayParams()),
        (PlayType.CODE_REVIEW, PlayParams()),
    ]

    results_a: list[PlayType] = []
    results_b: list[PlayType] = []

    for results, sub_dir in [(results_a, "run_a"), (results_b, "run_b")]:
        run_dir = tmp_path / sub_dir
        run_dir.mkdir()

        selector = FixedPlanSelector(list(plan))
        cfg = dataclasses.replace(
            RuntimeConfig(),
            session=SessionConfig(max_plays=2),
        )

        orch = await Orchestrator.bootstrap(
            cfg=cfg,
            repo_root=run_dir,
            selector=selector,
        )

        orch._executor.execute = _build_mock_execute(  # type: ignore[assignment]
            results, orch._store, orch._session_id
        )
        await _clear_cached_github_work(orch)

        with patch.object(orch._completion, "refresh_issues", new=AsyncMock()):
            async with orch:
                await orch.run_until_idle()

    assert results_a == results_b
    assert results_a == [PlayType.ISSUE_PICKUP, PlayType.CODE_REVIEW]


@pytest.mark.asyncio
async def test_fixed_plan_exhaustion_stops_loop(tmp_path: Path) -> None:
    """Orchestrator stops when FixedPlanSelector exhausts its plan."""
    plan = [(PlayType.RUN_QA, PlayParams())]
    selector = FixedPlanSelector(plan)
    cfg = dataclasses.replace(
        RuntimeConfig(),
        session=SessionConfig(max_plays=10),
    )

    orch = await Orchestrator.bootstrap(
        cfg=cfg,
        repo_root=tmp_path,
        selector=selector,
    )

    recorded: list[PlayType] = []
    orch._executor.execute = _build_mock_execute(  # type: ignore[assignment]
        recorded, orch._store, orch._session_id
    )
    await _clear_cached_github_work(orch)

    with patch.object(orch._completion, "refresh_issues", new=AsyncMock()):
        async with orch:
            await orch.run_until_idle()

    assert recorded == [PlayType.RUN_QA]
