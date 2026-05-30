"""Happy-path integration: a short session runs to completion."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from agentshore.config import RuntimeConfig
from agentshore.core import Orchestrator
from agentshore.plays.base import PlayParams
from agentshore.plays.selector import FixedPlanSelector
from agentshore.state import PlayType

from .conftest import make_outcome, make_recording_executor

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
async def test_happy_path_runs_three_plays(tmp_path: Path) -> None:
    """A 3-play session runs to completion and the loop terminates cleanly."""
    plan = [
        (PlayType.ISSUE_PICKUP, PlayParams()),
        (PlayType.CODE_REVIEW, PlayParams()),
        (PlayType.END_SESSION, PlayParams()),
    ]
    selector = FixedPlanSelector(plan)
    cfg = RuntimeConfig()

    orch = await Orchestrator.bootstrap(cfg=cfg, repo_root=tmp_path, selector=selector)

    outcomes = [
        make_outcome(PlayType.ISSUE_PICKUP, play_id=1),
        make_outcome(PlayType.CODE_REVIEW, play_id=2),
        make_outcome(PlayType.END_SESSION, play_id=3),
    ]
    mock_exec, recorded = make_recording_executor(outcomes, orch._store, orch._session_id)
    await _clear_cached_github_work(orch)

    with (
        patch.object(orch, "_refresh_issues", new=AsyncMock()),
        patch.object(orch._executor, "execute", side_effect=mock_exec),
    ):
        async with orch:
            await orch.run_until_idle()

    assert len(recorded) == 3
    assert recorded == [PlayType.ISSUE_PICKUP, PlayType.CODE_REVIEW, PlayType.END_SESSION]


@pytest.mark.asyncio
async def test_happy_path_records_plays_in_db(tmp_path: Path) -> None:
    """All executed plays are persisted to the plays table."""
    plan = [
        (PlayType.ISSUE_PICKUP, PlayParams()),
        (PlayType.END_SESSION, PlayParams()),
    ]
    selector = FixedPlanSelector(plan)
    cfg = RuntimeConfig()

    orch = await Orchestrator.bootstrap(cfg=cfg, repo_root=tmp_path, selector=selector)
    sid = orch._session_id

    outcomes = [
        make_outcome(PlayType.ISSUE_PICKUP, play_id=1),
        make_outcome(PlayType.END_SESSION, play_id=2),
    ]
    mock_exec, _ = make_recording_executor(outcomes, orch._store, sid)
    await _clear_cached_github_work(orch)

    with (
        patch.object(orch, "_refresh_issues", new=AsyncMock()),
        patch.object(orch._executor, "execute", side_effect=mock_exec),
    ):
        async with orch:
            await orch.run_until_idle()

    db_path = tmp_path / ".agentshore" / "agentshore.db"
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT COUNT(*) AS n FROM plays WHERE session_id = ?", (sid,)
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row["n"] == 2


@pytest.mark.asyncio
async def test_happy_path_session_completes(tmp_path: Path) -> None:
    """Session status transitions to 'completed' after END_SESSION."""
    plan = [(PlayType.END_SESSION, PlayParams())]
    selector = FixedPlanSelector(plan)
    cfg = RuntimeConfig()

    orch = await Orchestrator.bootstrap(cfg=cfg, repo_root=tmp_path, selector=selector)
    sid = orch._session_id

    outcomes = [make_outcome(PlayType.END_SESSION, play_id=1)]
    mock_exec, _ = make_recording_executor(outcomes, orch._store, sid)
    await _clear_cached_github_work(orch)

    with (
        patch.object(orch, "_refresh_issues", new=AsyncMock()),
        patch.object(orch._executor, "execute", side_effect=mock_exec),
    ):
        async with orch:
            await orch.run_until_idle()

    db_path = tmp_path / ".agentshore" / "agentshore.db"
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT status FROM sessions WHERE session_id = ?", (sid,)) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row["status"] == "completed"
