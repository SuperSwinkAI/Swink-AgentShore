"""Phase 4E: StateProvider protocol extension + orchestrator event ordering."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.plays.base import PlayParams
from agentshore.state import (
    AgentStatus,
    AgentType,
    NullStateProvider,
    OrchestratorState,
    PlayType,
    StateProvider,
)

# ---------------------------------------------------------------------------
# NullStateProvider: updated signature for on_play_started
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_provider_on_play_started_new_signature() -> None:
    """NullStateProvider.on_play_started accepts (play_type, params)."""
    provider = NullStateProvider()
    await provider.on_play_started(PlayType.ISSUE_PICKUP, PlayParams())
    await provider.on_play_started(PlayType.SEED_PROJECT, PlayParams(seed_path="PRD.md"))


@pytest.mark.asyncio
async def test_null_provider_all_hooks_new() -> None:
    provider = NullStateProvider()
    await provider.on_feedback_requested("loop_detected")
    await provider.on_session_paused("budget_exhaustion")


def test_null_provider_is_state_provider_runtime_check() -> None:
    assert isinstance(NullStateProvider(), StateProvider)


# ---------------------------------------------------------------------------
# Orchestrator: events emitted in correct order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_emits_on_play_started_before_execute(tmp_path: Path) -> None:
    """on_play_started fires after on_state_update and before on_play_completed."""
    from agentshore.config import RuntimeConfig
    from agentshore.core import Orchestrator

    cfg = RuntimeConfig()
    events: list[str] = []

    class TrackingProvider:
        async def on_state_update(self, state: OrchestratorState) -> None:
            events.append("state_update")

        async def on_play_started(self, play_type: PlayType, params: PlayParams) -> None:
            events.append(f"play_started:{play_type.value}")

        async def on_play_completed(self, play: object) -> None:
            events.append("play_completed")

        async def on_agent_changed(self, agent_id: str, status: AgentStatus) -> None:
            pass

        async def on_agent_subprocess_spawned(
            self, agent_id: str, agent_type: AgentType, pid: int
        ) -> None:
            pass

        async def on_agent_subprocess_exited(
            self, agent_id: str, agent_type: AgentType, pid: int, exit_code: int | None
        ) -> None:
            pass

        async def on_feedback_requested(self, reason: str) -> None:
            pass

        async def on_session_paused(self, reason: str) -> None:
            pass

        async def on_session_draining(self, reason: str) -> None:
            pass

        async def on_session_ended(self, reason: str) -> None:
            pass

        async def on_bootstrap_phase(self, phase: str, status: str, elapsed_ms: float) -> None:
            pass

    provider = TrackingProvider()

    mock_outcome = MagicMock()
    mock_outcome.play_type = PlayType.ISSUE_PICKUP
    mock_outcome.success = True
    mock_outcome.partial = False
    mock_outcome.dollar_cost = 0.01
    mock_outcome.duration_seconds = 1.0
    mock_outcome.alignment_delta = 0.1
    mock_outcome.play_id = 1
    mock_outcome.inflation_raised = False

    # Selector returns one play then None
    call_count = 0

    class OneShotSelector:
        def consume_pending(self) -> None:
            return None

        def should_update(self) -> bool:
            return False

        def should_checkpoint(self, total_plays: int) -> bool:
            return False

        async def select(self, state: OrchestratorState) -> tuple[PlayType, PlayParams] | None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (PlayType.ISSUE_PICKUP, PlayParams())
            return None

        async def on_play_completed(self, **kwargs: object) -> None:
            pass

    with (
        patch("agentshore.core.DataStore") as mock_ds_cls,
        patch("agentshore.core.AgentManager"),
        patch("agentshore.core.PlayExecutor") as mock_exec_cls,
        patch("agentshore.core.build_default_registry", return_value=MagicMock()),
        patch("agentshore.core.ParameterResolver"),
        patch("agentshore.skills.install_skills"),
        patch("agentshore.core.setup_logging"),
        patch("agentshore.github.adapter.GitHubAdapter"),
    ):
        mock_ds = AsyncMock()
        mock_ds_cls.return_value = mock_ds
        mock_ds.get_open_issues = AsyncMock(return_value=[])
        mock_ds.get_play_history = AsyncMock(return_value=[])
        mock_ds.get_latest_trajectory = AsyncMock(return_value=None)
        mock_ds.create_session = AsyncMock()
        mock_ds.complete_session = AsyncMock()
        mock_ds.close = AsyncMock()

        mock_executor = AsyncMock()
        mock_executor.execute = AsyncMock(return_value=mock_outcome)
        mock_exec_cls.return_value = mock_executor

        selector = OneShotSelector()
        orch = await Orchestrator.bootstrap(
            cfg=cfg,
            repo_root=tmp_path,
            selector=selector,
            state_provider=provider,
        )
        # desktop-mr1i: dispatch reads `_manager.worktrees.main_repo`. The
        # patched AgentManager leaves `.worktrees` as a MagicMock whose
        # `main_repo` looks truthy to `_is_git_work_tree`, sending dispatch
        # down the async allocate path. Wire the AgentManager mock to the
        # real WorktreeManager bootstrap built (`orch._worktrees`) — its
        # `main_repo` is the tmp_path which `_is_git_work_tree` correctly
        # reports as not-a-work-tree, short-circuiting to TrunkAllocation.
        orch._manager.worktrees = orch._worktrees

        async with orch:
            await orch.run_until_idle()

    # Order: state_update (with current_play set), play_started, then after play:
    # play_completed, state_update (post-completion)
    assert "state_update" in events
    play_started_idx = next(i for i, e in enumerate(events) if e.startswith("play_started:"))
    state_update_idx = events.index("state_update")
    play_completed_idx = events.index("play_completed")

    assert state_update_idx < play_started_idx < play_completed_idx, f"Event order wrong: {events}"

    # There should be a second state_update after play_completed
    post_state_updates = [i for i, e in enumerate(events) if e == "state_update"]
    assert len(post_state_updates) >= 2, f"Expected 2+ state_updates, got: {events}"
    assert post_state_updates[-1] > play_completed_idx, (
        "Last state_update should be after play_completed"
    )
