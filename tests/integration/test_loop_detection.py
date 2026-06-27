"""Loop-detection integration: repeated same-type failures trigger escalation."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from agentshore.config import LoopDetectionConfig, RuntimeConfig
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
async def test_loop_detection_escalates_after_threshold(tmp_path: Path) -> None:
    """Consecutive same-type failures >= escalate_after triggers a pause."""
    cfg = dataclasses.replace(
        RuntimeConfig(),
        rl=dataclasses.replace(
            RuntimeConfig().rl,
            loop_detection=LoopDetectionConfig(
                warn_after=2,
                force_switch_after=3,
                escalate_after=5,
            ),
        ),
    )

    # Provide plenty of plays so the selector never returns None
    plan = [(PlayType.ISSUE_PICKUP, PlayParams()) for _ in range(30)]
    selector = FixedPlanSelector(plan)

    orch = await Orchestrator.bootstrap(cfg=cfg, repo_root=tmp_path, selector=selector)
    sid = orch._session_id
    call_count = 0

    async def mock_execute(
        play_type: PlayType,
        state: object,
        *,
        override: PlayParams | None = None,
    ) -> object:
        nonlocal call_count
        call_count += 1
        # Record a failing play row so _build_state computes the streak
        from agentshore.data.store import PlayRecord

        await orch._store.record_play(
            PlayRecord(
                session_id=sid,
                play_type=play_type.value,
                started_at="2026-01-01T00:00:00+00:00",
                ended_at="2026-01-01T00:00:01+00:00",
                success=False,
                dollar_cost=0.001,
            )
        )
        return make_outcome(play_type, success=False, alignment_delta=0.0, dollar_cost=0.001)

    with patch.object(orch._executor, "execute", side_effect=mock_execute):
        async with orch:
            task = asyncio.create_task(orch.run_until_idle())
            for _ in range(40):
                if not orch._pause_event.is_set():
                    break
                await asyncio.sleep(0.1)

            assert not orch._pause_event.is_set(), (
                "Loop should pause after escalate_after consecutive failures"
            )
            assert call_count >= 1

            orch._stop_requested = True
            orch._pause_event.set()
            await asyncio.wait_for(task, timeout=5.0)


@pytest.mark.asyncio
async def test_loop_detection_does_not_trigger_on_mixed_types(tmp_path: Path) -> None:
    """Alternating play types do not trigger the loop detector."""
    cfg = dataclasses.replace(
        RuntimeConfig(),
        rl=dataclasses.replace(
            RuntimeConfig().rl,
            loop_detection=LoopDetectionConfig(
                warn_after=2,
                force_switch_after=3,
                escalate_after=4,
            ),
        ),
    )

    # Alternate between two play types — failures but of different types,
    # so no same-type streak builds.  End with END_SESSION.
    plan = [
        (PlayType.ISSUE_PICKUP, PlayParams()),
        (PlayType.CODE_REVIEW, PlayParams()),
        (PlayType.ISSUE_PICKUP, PlayParams()),
        (PlayType.CODE_REVIEW, PlayParams()),
        (PlayType.END_SESSION, PlayParams()),
    ]
    selector = FixedPlanSelector(plan)
    orch = await Orchestrator.bootstrap(cfg=cfg, repo_root=tmp_path, selector=selector)
    sid = orch._session_id
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

        success = play_type == PlayType.END_SESSION
        await orch._store.record_play(
            PlayRecord(
                session_id=sid,
                play_type=play_type.value,
                started_at="2026-01-01T00:00:00+00:00",
                success=success,
                dollar_cost=0.001,
            )
        )
        return make_outcome(play_type, success=success, alignment_delta=0.0, dollar_cost=0.001)

    await _clear_cached_github_work(orch)
    with (
        patch.object(orch._completion, "refresh_issues", new=AsyncMock()),
        patch.object(orch._executor, "execute", side_effect=mock_execute),
    ):
        async with orch:
            await asyncio.wait_for(orch.run_until_idle(), timeout=5.0)

    assert call_count == 5
    # pause_event still set (from resume) means the loop detector never fired.
    assert orch._pause_event.is_set()
