"""Budget-reserve integration: the loop drains before budget is exhausted."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from agentshore.config import BudgetConfig, RuntimeConfig
from agentshore.core import Orchestrator
from agentshore.plays.base import PlayParams
from agentshore.plays.selector import FixedPlanSelector
from agentshore.state import PlayType

from .conftest import make_outcome

if TYPE_CHECKING:
    from pathlib import Path


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
async def test_budget_reserve_allows_work_below_threshold(tmp_path: Path) -> None:
    """Session continues assigning work while known spend is below total minus $5."""
    cfg = dataclasses.replace(
        RuntimeConfig(),
        budget=BudgetConfig(enabled=True, total=20.0),
    )

    plan = [(PlayType.ISSUE_PICKUP, PlayParams())]
    selector = FixedPlanSelector(plan)

    orch = await Orchestrator.bootstrap(cfg=cfg, repo_root=tmp_path, selector=selector)
    sid = orch._session_id
    await _clear_cached_github_work(orch)

    call_count = 0

    async def mock_execute(
        play_type: PlayType,
        state: object,
        *,
        override: PlayParams | None = None,
    ) -> object:
        nonlocal call_count
        call_count += 1
        # Record a play row so _build_state tallies the cost correctly
        from agentshore.data.store import PlayRecord

        await orch._store.record_play(
            PlayRecord(
                session_id=sid,
                play_type=play_type.value,
                started_at="2026-01-01T00:00:00+00:00",
                success=True,
                dollar_cost=14.99,
            )
        )
        return make_outcome(play_type, dollar_cost=14.99)

    with (
        patch.object(orch._executor, "execute", side_effect=mock_execute),
        patch.object(orch, "_refresh_issues", new=AsyncMock()),
    ):
        async with orch:
            await asyncio.wait_for(orch.run_until_idle(), timeout=5.0)

            assert call_count == 1
            assert orch._draining is False
            assert orch._pause_event.is_set()


@pytest.mark.asyncio
async def test_budget_reserve_starts_drain_at_threshold(tmp_path: Path) -> None:
    """At $15 known spend on a $20 budget, AgentShore drains instead of assigning more work."""
    cfg = dataclasses.replace(
        RuntimeConfig(),
        budget=BudgetConfig(enabled=True, total=20.0),
    )

    plan = [(PlayType.ISSUE_PICKUP, PlayParams()) for _ in range(20)]
    selector = FixedPlanSelector(plan)

    orch = await Orchestrator.bootstrap(cfg=cfg, repo_root=tmp_path, selector=selector)
    sid = orch._session_id
    await _clear_cached_github_work(orch)

    call_count = 0

    async def mock_execute(
        play_type: PlayType,
        state: object,
        *,
        override: PlayParams | None = None,
    ) -> object:
        nonlocal call_count
        call_count += 1
        from agentshore.data.store import PlayRecord

        await orch._store.record_play(
            PlayRecord(
                session_id=sid,
                play_type=play_type.value,
                started_at="2026-01-01T00:00:00+00:00",
                success=True,
                dollar_cost=15.0,
            )
        )
        return make_outcome(play_type, dollar_cost=15.0)

    with (
        patch.object(orch._executor, "execute", side_effect=mock_execute),
        patch.object(orch, "_refresh_issues", new=AsyncMock()),
    ):
        async with orch:
            await asyncio.wait_for(orch.run_until_idle(), timeout=5.0)

            assert call_count == 1
            assert orch._draining is True
            assert orch._drain_reason == "budget_reserve_reached"
            assert orch._pause_event.is_set()
