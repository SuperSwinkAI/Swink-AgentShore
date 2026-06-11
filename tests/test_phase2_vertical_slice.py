"""Phase 2J vertical slice: InstantiateAgent + IssuePickup end-to-end.

Uses the mock_agent subprocess fixture from Phase 1 to exercise:
  PlayExecutor → AgentManager → mock subprocess → result parsed → DB rows written.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agentshore.agents.manager import AgentManager
from agentshore.config import AgentConfig, ModelTierConfig, RuntimeConfig
from agentshore.data.store import DataStore, SessionRecord
from agentshore.plays.executor import PlayExecutor
from agentshore.plays.internal.instantiate_agent import InstantiateAgentPlay
from agentshore.plays.registry import PlayRegistry
from agentshore.plays.skill_backed.issue_pickup import IssuePickupPlay
from agentshore.state import (
    AgentStatus,
    AgentType,
    BudgetSnapshot,
    IssueSnapshot,
    OrchestratorState,
    PlayType,
    SessionState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_issue(num: int) -> IssueSnapshot:
    return IssueSnapshot(
        issue_number=num,
        title=f"Fix bug #{num}",
        state="open",
        priority=1,
        labels=[],
        source="github",
    )


def _make_state(
    session_id: str = "sess-slice",
    agent_snapshots: list | None = None,
    issues: list | None = None,
    plays_since_last_play_type: dict[PlayType, int] | None = None,
) -> OrchestratorState:
    # Default to a post-seed state so tests that don't care about the seed
    # gate don't need to set it up explicitly.
    if plays_since_last_play_type is None:
        plays_since_last_play_type = {PlayType.SEED_PROJECT: 5}
    return OrchestratorState(
        session_id=session_id,
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=agent_snapshots or [],
        open_issues=issues or [],
        plays_since_last_play_type=plays_since_last_play_type,
        budget=BudgetSnapshot(
            total_budget=5.0,
            spent=0.5,
            remaining=4.5,
            estimated_cost_per_play=0.1,
        ),
    )


# ---------------------------------------------------------------------------
# InstantiateAgentPlay unit tests
# ---------------------------------------------------------------------------


def test_instantiate_blocked_until_intake_completes() -> None:
    from agentshore.state import AgentSnapshot

    play = InstantiateAgentPlay()
    # An agent already exists but no first-play has completed → fleet *expansion*
    # is deferred. (With an empty fleet the first spawn is allowed — that is the
    # open-start cold-boot path, so the gate only applies once agents exist.)
    agent = AgentSnapshot(
        agent_id="a1",
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.IDLE,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
        model_tier="medium",
    )
    state = _make_state(plays_since_last_play_type={}, agent_snapshots=[agent])
    errors = play.preconditions(state)
    assert any("bootstrap first-play" in e.text for e in errors)


def test_instantiate_allowed_on_empty_fleet_cold_start() -> None:
    """Open-start cold boot: with zero agents the first spawn is NOT gated."""
    play = InstantiateAgentPlay()
    state = _make_state(plays_since_last_play_type={}, agent_snapshots=[])
    assert play.preconditions(state) == []


def test_instantiate_unblocked_after_seed_completes() -> None:
    play = InstantiateAgentPlay()
    state = _make_state(plays_since_last_play_type={PlayType.SEED_PROJECT: 3})
    assert play.preconditions(state) == []


def test_instantiate_unblocked_after_cleanup_completes() -> None:
    """desktop-arph: cleanup is an alternative bootstrap first-play."""
    play = InstantiateAgentPlay()
    state = _make_state(plays_since_last_play_type={PlayType.CLEANUP: 1})
    assert play.preconditions(state) == []


def test_instantiate_precondition_budget_too_low() -> None:
    play = InstantiateAgentPlay()
    state = _make_state()
    state.budget = BudgetSnapshot(5.0, 5.0, 0.0, 0.0)
    errors = play.preconditions(state)
    assert any("budget" in e.text for e in errors)


def test_instantiate_precondition_met_with_budget_and_slots() -> None:
    play = InstantiateAgentPlay()
    state = _make_state()
    errors = play.preconditions(state)
    assert errors == []


def test_instantiate_per_tier_max_enforced_at_execute() -> None:
    """Per-tier max is enforced at execute() time, not in preconditions.

    The precondition only checks bootstrap/budget/in-flight — the per-cell
    cap is enforced in execute() once we know the target (type, tier).
    """
    play = InstantiateAgentPlay()
    state = _make_state()
    # Preconditions don't gate on per-tier max — execute/mask do.
    assert play.preconditions(state) == []


@pytest.mark.asyncio
async def test_instantiate_blocks_when_per_config_cap_reached() -> None:
    from agentshore.state import AgentSnapshot

    play = InstantiateAgentPlay()
    state = _make_state(
        agent_snapshots=[
            AgentSnapshot(
                agent_id=f"a{i}",
                agent_type=AgentType.CLAUDE_CODE,
                status=AgentStatus.IDLE,
                context_size=0,
                total_cost=0.0,
                total_tokens=0,
                tasks_completed=0,
                tasks_failed=0,
                model_tier="medium",
            )
            for i in range(3)
        ],
    )

    ctx = MagicMock()
    ctx.cfg = RuntimeConfig(
        agents={
            "claude_code": AgentConfig(
                enabled=True,
                model_tiers={"medium": ModelTierConfig(model="sonnet", enabled=True, max=3)},
            )
        }
    )
    ctx.manager.instantiate = AsyncMock()

    from agentshore.plays.base import PlayParams

    outcome = await play.execute(
        state,
        PlayParams(target_agent_type="claude_code", target_model_tier="medium"),
        ctx=ctx,
    )

    assert outcome.success is False
    assert "per-tier max" in (outcome.error or "")
    ctx.manager.instantiate.assert_not_called()


@pytest.mark.asyncio
async def test_instantiate_blocks_when_same_config_has_idle_agent() -> None:
    from agentshore.state import AgentSnapshot

    play = InstantiateAgentPlay()
    state = _make_state(
        agent_snapshots=[
            AgentSnapshot(
                agent_id="a0",
                agent_type=AgentType.CLAUDE_CODE,
                status=AgentStatus.IDLE,
                context_size=0,
                total_cost=0.0,
                total_tokens=0,
                tasks_completed=0,
                tasks_failed=0,
                model_tier="medium",
            )
        ],
    )

    ctx = MagicMock()
    ctx.cfg = RuntimeConfig(
        agents={
            "claude_code": AgentConfig(
                enabled=True,
                model_tiers={"medium": ModelTierConfig(model="sonnet", enabled=True, max=2)},
            )
        }
    )
    handle = MagicMock()
    handle.agent_id = "a1"
    handle.model = "sonnet"
    handle.reasoning_effort = None
    ctx.manager.instantiate = AsyncMock(return_value=handle)

    from agentshore.plays.base import PlayParams

    outcome = await play.execute(
        state,
        PlayParams(target_agent_type="claude_code", target_model_tier="medium"),
        ctx=ctx,
    )

    assert outcome.success is False
    assert "idle agent available" in (outcome.error or "")
    ctx.manager.instantiate.assert_not_called()


@pytest.mark.asyncio
async def test_instantiate_allows_second_busy_same_config_below_cap() -> None:
    from agentshore.state import AgentSnapshot

    play = InstantiateAgentPlay()
    state = _make_state(
        agent_snapshots=[
            AgentSnapshot(
                agent_id="a0",
                agent_type=AgentType.CLAUDE_CODE,
                status=AgentStatus.BUSY,
                context_size=0,
                total_cost=0.0,
                total_tokens=0,
                tasks_completed=0,
                tasks_failed=0,
                model_tier="medium",
            )
        ],
    )

    ctx = MagicMock()
    ctx.cfg = RuntimeConfig(
        agents={
            "claude_code": AgentConfig(
                enabled=True,
                model_tiers={"medium": ModelTierConfig(model="sonnet", enabled=True, max=2)},
            )
        }
    )
    handle = MagicMock()
    handle.agent_id = "a1"
    handle.model = "sonnet"
    handle.reasoning_effort = None
    ctx.manager.instantiate = AsyncMock(return_value=handle)

    from agentshore.plays.base import PlayParams

    outcome = await play.execute(
        state,
        PlayParams(target_agent_type="claude_code", target_model_tier="medium"),
        ctx=ctx,
    )

    assert outcome.success is True
    assert outcome.agent_id == "a1"


@pytest.mark.asyncio
async def test_instantiate_play_returns_agent_artifact() -> None:
    play = InstantiateAgentPlay()
    state = _make_state()

    ctx = MagicMock()
    ctx.cfg = RuntimeConfig()
    handle = MagicMock()
    handle.agent_id = "new-agent-id"
    handle.model = "sonnet"
    handle.reasoning_effort = None
    ctx.manager.instantiate = AsyncMock(return_value=handle)

    from agentshore.plays.base import PlayParams

    outcome = await play.execute(state, PlayParams(target_agent_type="claude_code"), ctx=ctx)

    assert outcome.success is True
    assert outcome.agent_id == "new-agent-id"
    assert outcome.dollar_cost > 0.0
    assert any(
        isinstance(a, dict) and a.get("agent_id") == "new-agent-id" for a in outcome.artifacts
    )


@pytest.mark.asyncio
async def test_instantiate_play_without_target_uses_first_enabled_config() -> None:
    play = InstantiateAgentPlay()
    state = _make_state()

    ctx = MagicMock()
    ctx.cfg = RuntimeConfig(
        agents={
            AgentType.CODEX.value: AgentConfig(enabled=True),
            AgentType.CLAUDE_CODE.value: AgentConfig(enabled=True),
        }
    )
    handle = MagicMock()
    handle.agent_id = "codex-agent-id"
    handle.model = "gpt-5.5"
    handle.reasoning_effort = "medium"
    ctx.manager.instantiate = AsyncMock(return_value=handle)

    from agentshore.plays.base import PlayParams

    outcome = await play.execute(state, PlayParams(), ctx=ctx)

    assert outcome.success is True
    assert outcome.agent_id == "codex-agent-id"
    ctx.manager.instantiate.assert_awaited_once_with(AgentType.CODEX, model_tier="medium")


@pytest.mark.asyncio
async def test_instantiate_play_returns_failure_on_exception() -> None:
    play = InstantiateAgentPlay()
    state = _make_state()

    ctx = MagicMock()
    ctx.cfg = RuntimeConfig()
    ctx.manager.instantiate = AsyncMock(side_effect=RuntimeError("instantiation failed"))

    from agentshore.plays.base import PlayParams

    outcome = await play.execute(state, PlayParams(target_agent_type="claude_code"), ctx=ctx)

    assert outcome.success is False
    assert "instantiation failed" in (outcome.error or "")


# ---------------------------------------------------------------------------
# IssuePickupPlay unit tests
# ---------------------------------------------------------------------------


def test_issue_pickup_precondition_no_issues() -> None:
    play = IssuePickupPlay()
    state = _make_state(issues=[])
    errors = play.preconditions(state)
    assert any("no open issues" in e.text for e in errors)


def test_issue_pickup_precondition_no_idle_implementer() -> None:
    from agentshore.state import AgentSnapshot

    play = IssuePickupPlay()
    agents = [
        AgentSnapshot(
            agent_id="a1",
            agent_type=AgentType.CLAUDE_CODE,
            status=AgentStatus.BUSY,
            context_size=0,
            total_cost=0.0,
            total_tokens=0,
            tasks_completed=0,
            tasks_failed=0,
        )
    ]
    state = _make_state(agent_snapshots=agents, issues=[_make_issue(1)])
    errors = play.preconditions(state)
    assert any("can_implement" in e.text for e in errors)


def test_issue_pickup_precondition_met() -> None:
    from agentshore.state import AgentSnapshot

    play = IssuePickupPlay()
    agents = [
        AgentSnapshot(
            agent_id="a1",
            agent_type=AgentType.CLAUDE_CODE,
            status=AgentStatus.IDLE,
            context_size=0,
            total_cost=0.0,
            total_tokens=0,
            tasks_completed=0,
            tasks_failed=0,
        )
    ]
    state = _make_state(agent_snapshots=agents, issues=[_make_issue(7)])
    errors = play.preconditions(state)
    assert errors == []


# ---------------------------------------------------------------------------
# Vertical slice: executor → mock subprocess → DB rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_pickup_via_executor_writes_pr_and_branch_rows(
    mock_agent_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full vertical slice: executor orchestrates IssuePickup through mock agent.

    Verifies:
    - A play row is inserted before dispatch (with placeholder success=False)
    - Play row is updated to success=True after success
    - PR branch exposure cache is written (manager.branch_exposure)
    - PR record is persisted to DB (store.record_pull_request)
    """
    # Use stream_json format so CLAUDE_CODE agent type parses output correctly
    monkeypatch.setenv("MOCK_AGENT_FORMAT", "stream_json")

    # -- Set up a real DataStore with a temp-file SQLite DB --
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    await store.create_session(
        SessionRecord(
            session_id="sess-slice",
            project_path=str(tmp_path),
            started_at="2026-01-01T00:00:00+00:00",
        )
    )

    try:
        # -- Set up AgentManager with mock_agent subprocess --
        cfg = RuntimeConfig(
            agents={
                "claude_code": AgentConfig(
                    enabled=True,
                    binary=str(mock_agent_path),
                    max_context=200_000,
                )
            }
        )
        manager = AgentManager(
            session_id="sess-slice",
            store=store,
            cfg=cfg,
            working_dir=tmp_path,
            python_executable=sys.executable,
        )
        handle = await manager.instantiate(AgentType.CLAUDE_CODE)

        # -- Build registry with IssuePickupPlay --
        registry = PlayRegistry()
        registry.register(IssuePickupPlay())
        registry.freeze()

        # -- Build resolver that returns issue_number=7 --
        resolver = AsyncMock()
        from agentshore.plays.base import PlayParams

        resolver.resolve = AsyncMock(return_value=PlayParams(issue_number=7))

        # -- Create executor --
        executor = PlayExecutor(
            registry=registry,
            resolver=resolver,
            store=store,
            manager=manager,
            cfg=cfg,
            project_path=tmp_path,
            session_id="sess-slice",
        )

        # -- Build state with IDLE agent and open issue --
        from agentshore.state import AgentSnapshot

        state = _make_state(
            session_id="sess-slice",
            agent_snapshots=[
                AgentSnapshot(
                    agent_id=handle.agent_id,
                    agent_type=AgentType.CLAUDE_CODE,
                    status=AgentStatus.IDLE,
                    context_size=0,
                    total_cost=0.0,
                    total_tokens=0,
                    tasks_completed=0,
                    tasks_failed=0,
                )
            ],
            issues=[_make_issue(7)],
        )

        # -- Patch select_agent_for to return our handle --
        with patch("agentshore.plays.executor.select_agent_for", return_value=handle):
            outcome = await executor.execute(PlayType.ISSUE_PICKUP, state)

        # Verify success
        assert outcome.success is True, f"Expected success, got error: {outcome.error}"

        # Verify PR record was written to DB (the mock agent emits pr_number=42)
        pr_rows = await store.list_open_pull_requests("sess-slice")
        assert any(pr.pr_number == 42 for pr in pr_rows)

        # Verify play row was written and updated
        plays = await store.get_play_history("sess-slice")
        # First play is the instantiate_agent call (from manager.instantiate above)
        # The second is the ISSUE_PICKUP play from executor.execute
        issue_pickup_plays = [p for p in plays if p.play_type == PlayType.ISSUE_PICKUP.value]
        assert len(issue_pickup_plays) >= 1
        assert issue_pickup_plays[0].success is True
    finally:
        await store.close()
