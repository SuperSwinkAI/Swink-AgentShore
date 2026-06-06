"""Integration tests for config immutability and atomic replacement."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from agentshore.config import BudgetConfig, RuntimeConfig, SessionConfig


@pytest.mark.asyncio
async def test_config_is_frozen_dataclass() -> None:
    """RuntimeConfig is frozen — direct attribute assignment raises."""
    cfg = RuntimeConfig()

    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.mode = "agent"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_config_is_replaceable() -> None:
    """Config can be replaced atomically via dataclasses.replace."""
    cfg = RuntimeConfig()
    assert cfg.budget.enabled is False
    assert cfg.budget.total == 0.0

    new_cfg = dataclasses.replace(cfg, budget=BudgetConfig(enabled=True, total=20.0))
    assert new_cfg.budget.enabled is True
    assert new_cfg.budget.total == 20.0
    assert cfg.budget.total == 0.0  # original unchanged (frozen)

    # Nested replacement also works
    new_cfg2 = dataclasses.replace(
        cfg,
        session=SessionConfig(max_plays=42),
    )
    assert new_cfg2.session.max_plays == 42
    assert cfg.session.max_plays is None  # original untouched

    # The wall-clock cap now lives on the budget dimension (migrated off the
    # former session.timeout_minutes).
    new_cfg3 = dataclasses.replace(
        cfg,
        budget=BudgetConfig(time_enabled=True, time_total_minutes=120),
    )
    assert new_cfg3.budget.time_enabled is True
    assert new_cfg3.budget.time_total_minutes == 120


@pytest.mark.asyncio
async def test_orchestrator_uses_config_budget(tmp_path: Path) -> None:
    """Orchestrator drains when known spend reaches the final $5 reserve."""
    import asyncio

    from agentshore.core import Orchestrator
    from agentshore.data.store import PlayRecord
    from agentshore.plays.base import PlayParams
    from agentshore.plays.selector import FixedPlanSelector
    from agentshore.state import PlayOutcome, PlayType

    cfg = dataclasses.replace(
        RuntimeConfig(),
        budget=BudgetConfig(enabled=True, total=20.0),
        session=SessionConfig(max_plays=10),
    )

    plan = [
        (PlayType.ISSUE_PICKUP, PlayParams()),
        (PlayType.CODE_REVIEW, PlayParams()),
        (PlayType.RUN_QA, PlayParams()),
        (PlayType.ISSUE_PICKUP, PlayParams()),
        (PlayType.ISSUE_PICKUP, PlayParams()),
    ]
    selector = FixedPlanSelector(list(plan))

    orch = await Orchestrator.bootstrap(
        cfg=cfg,
        repo_root=tmp_path,
        selector=selector,
    )

    recorded: list[PlayType] = []

    async def mock_execute(
        play_type: PlayType,
        state: object,
        *,
        override: PlayParams | None = None,
    ) -> PlayOutcome:
        recorded.append(play_type)
        await orch._store.record_play(
            PlayRecord(
                session_id=orch._session_id,
                play_type=play_type.value,
                started_at="2026-01-01T00:00:00+00:00",
                success=True,
                dollar_cost=15.0,
            )
        )
        return PlayOutcome(
            play_type=play_type,
            agent_id="agent-1",
            success=True,
            partial=False,
            duration_seconds=1.0,
            token_cost=100,
            dollar_cost=15.0,
            artifacts=[],
            alignment_delta=0.05,
            play_id=len(recorded),
        )

    orch._executor.execute = mock_execute  # type: ignore[assignment]

    # Clear bootstrap override queue so only the FixedPlanSelector drives plays
    while not orch._overrides.empty():
        orch._overrides.get_nowait()

    async with orch:
        await asyncio.wait_for(orch.run_until_idle(), timeout=10.0)

    assert len(recorded) == 1
    assert recorded[0] == PlayType.ISSUE_PICKUP
    assert orch._draining is True
    assert orch._drain_reason == "budget_reserve_reached"


@pytest.mark.asyncio
async def test_config_loaded_from_yaml(tmp_path: Path) -> None:
    """load_config parses a YAML file into a RuntimeConfig."""
    from agentshore.config import load_config

    config_file = tmp_path / "agentshore.yaml"
    config_file.write_text(
        """\
budget:
  enabled: true
  total: 25.0
  warning_threshold: 0.15
  time_enabled: true
  time_total_minutes: 120

session:
  max_plays: 100

scope:
  strict_mode: true
""",
        encoding="utf-8",
    )

    cfg = load_config(config_file)
    assert cfg.budget.enabled is True
    assert cfg.budget.total == 25.0
    assert cfg.budget.warning_threshold == 0.15
    assert cfg.budget.time_enabled is True
    assert cfg.budget.time_total_minutes == 120
    assert cfg.session.max_plays == 100
    assert cfg.scope.strict_mode is True
    # Unspecified fields retain defaults
    assert cfg.mode == "solo"
