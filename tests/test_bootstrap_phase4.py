"""Phase 4A: bootstrap timing, NullStateProvider, setup_logging wiring."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.config import RuntimeConfig
from agentshore.state import AgentStatus, NullStateProvider, OrchestratorState


@pytest.mark.asyncio
async def test_null_state_provider_accepts_all_hooks() -> None:
    provider = NullStateProvider()
    state = OrchestratorState(
        session_id="test",
        session_state=__import__(
            "agentshore.state", fromlist=["SessionState"]
        ).SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
    )
    from agentshore.plays.base import PlayParams
    from agentshore.state import PlayType

    await provider.on_state_update(state)
    await provider.on_play_started(PlayType.ISSUE_PICKUP, PlayParams())
    await provider.on_play_completed(MagicMock())
    await provider.on_agent_changed("agent-1", AgentStatus.IDLE)
    await provider.on_feedback_requested("budget_exhaustion")
    await provider.on_session_paused("loop_detected")


def test_null_state_provider_is_state_provider() -> None:
    from agentshore.state import StateProvider

    provider = NullStateProvider()
    assert isinstance(provider, StateProvider)


@pytest.mark.asyncio
async def test_bootstrap_default_state_provider_is_null(tmp_path: Path) -> None:
    cfg = RuntimeConfig()

    with (
        patch("agentshore.core.phases.DataStore") as mock_ds_cls,
        patch("agentshore.core.phases.AgentManager"),
        patch("agentshore.core.phases.PlayExecutor"),
        patch("agentshore.core.phases.build_default_registry", return_value=MagicMock()),
        patch("agentshore.core.phases.ParameterResolver"),
        patch("agentshore.skills.install_skills"),
        patch("agentshore.core.phases._phase_session_start_worktree_sweep", new_callable=AsyncMock),
        patch("agentshore.core.orchestrator.setup_logging"),
    ):
        mock_ds = AsyncMock()
        mock_ds_cls.return_value = mock_ds

        from agentshore.core import Orchestrator

        orch = await Orchestrator.bootstrap(cfg=cfg, repo_root=tmp_path)

    from agentshore.state import NullStateProvider

    assert isinstance(orch._runtime.state_provider, NullStateProvider)


@pytest.mark.asyncio
async def test_bootstrap_calls_setup_logging(tmp_path: Path) -> None:
    cfg = RuntimeConfig()
    log_calls: list[dict[str, Any]] = []

    def capture_setup_logging(**kwargs: Any) -> None:
        log_calls.append(kwargs)

    with (
        patch("agentshore.core.phases.DataStore") as mock_ds_cls,
        patch("agentshore.core.phases.AgentManager"),
        patch("agentshore.core.phases.PlayExecutor"),
        patch("agentshore.core.phases.build_default_registry", return_value=MagicMock()),
        patch("agentshore.core.phases.ParameterResolver"),
        patch("agentshore.skills.install_skills"),
        patch("agentshore.core.phases._phase_session_start_worktree_sweep", new_callable=AsyncMock),
        patch("agentshore.core.orchestrator.setup_logging", side_effect=capture_setup_logging),
    ):
        mock_ds = AsyncMock()
        mock_ds_cls.return_value = mock_ds

        from agentshore.core import Orchestrator

        await Orchestrator.bootstrap(cfg=cfg, repo_root=tmp_path, session_id="test-sid")

    assert len(log_calls) == 1
    assert log_calls[0]["level"] == cfg.logging.level
    assert log_calls[0]["session_id"] == "test-sid"


@pytest.mark.asyncio
async def test_bootstrap_logs_each_step_with_timing(
    tmp_path: Path, capsys: Any, caplog: Any
) -> None:
    """bootstrap_step INFO logs are emitted for each major step."""
    import logging

    cfg = RuntimeConfig()

    with (
        caplog.at_level(logging.DEBUG),
        patch("agentshore.core.phases.DataStore") as mock_ds_cls,
        patch("agentshore.core.phases.AgentManager"),
        patch("agentshore.core.phases.PlayExecutor"),
        patch("agentshore.core.phases.build_default_registry", return_value=MagicMock()),
        patch("agentshore.core.phases.ParameterResolver"),
        patch("agentshore.skills.install_skills"),
        patch("agentshore.core.phases._phase_session_start_worktree_sweep", new_callable=AsyncMock),
        patch("agentshore.core.orchestrator.setup_logging"),
    ):
        mock_ds = AsyncMock()
        mock_ds_cls.return_value = mock_ds

        from agentshore.core import Orchestrator

        await Orchestrator.bootstrap(cfg=cfg, repo_root=tmp_path)

    captured = capsys.readouterr()
    all_output = captured.out + captured.err + " ".join(r.getMessage() for r in caplog.records)

    expected_steps = {
        "init_datastore",
        "init_manager",
        "init_executor",
        "init_metrics",
    }
    for step in expected_steps:
        assert step in all_output, f"Missing step: {step}"


# bootstrap_phase IPC events (desktop-zmw)


@pytest.mark.asyncio
async def test_step_fires_publisher_on_started_and_completed() -> None:
    """Inside a bootstrap publisher context, _step emits started + completed."""
    from agentshore.core.helpers import _bootstrap_phase_publisher, _step

    events: list[tuple[str, str, float]] = []

    async def publisher(phase: str, status: str, elapsed_ms: float) -> None:
        events.append((phase, status, elapsed_ms))

    token = _bootstrap_phase_publisher.set(publisher)
    try:
        async with _step("some_phase"):
            pass
    finally:
        _bootstrap_phase_publisher.reset(token)

    assert [(e[0], e[1]) for e in events] == [
        ("some_phase", "started"),
        ("some_phase", "completed"),
    ]
    assert events[0][2] == 0.0
    assert events[1][2] >= 0.0


@pytest.mark.asyncio
async def test_step_is_silent_without_publisher() -> None:
    """Without a publisher set, _step does no extra work — calling it must not crash."""
    from agentshore.core.helpers import _bootstrap_phase_publisher, _step

    assert _bootstrap_phase_publisher.get() is None
    async with _step("silent_phase"):
        pass


@pytest.mark.asyncio
async def test_step_swallows_publisher_failures() -> None:
    """A broken publisher (e.g. dashboard disconnected) must not break bootstrap."""
    from agentshore.core.helpers import _bootstrap_phase_publisher, _step

    async def broken_publisher(phase: str, status: str, elapsed_ms: float) -> None:
        raise RuntimeError("dashboard went away")

    token = _bootstrap_phase_publisher.set(broken_publisher)
    try:
        async with _step("noisy_phase"):
            pass
    finally:
        _bootstrap_phase_publisher.reset(token)


@pytest.mark.asyncio
async def test_bootstrap_forwards_phases_to_state_provider(tmp_path: Path) -> None:
    """Orchestrator.bootstrap should call provider.on_bootstrap_phase for each step."""
    phase_events: list[tuple[str, str, float]] = []

    class RecordingProvider(NullStateProvider):
        async def on_bootstrap_phase(self, phase: str, status: str, elapsed_ms: float) -> None:
            phase_events.append((phase, status, elapsed_ms))

    cfg = RuntimeConfig()
    provider = RecordingProvider()

    with (
        patch("agentshore.core.phases.DataStore") as mock_ds_cls,
        patch("agentshore.core.phases.AgentManager"),
        patch("agentshore.core.phases.PlayExecutor"),
        patch("agentshore.core.phases.build_default_registry", return_value=MagicMock()),
        patch("agentshore.core.phases.ParameterResolver"),
        patch("agentshore.skills.install_skills"),
        patch("agentshore.core.phases._phase_session_start_worktree_sweep", new_callable=AsyncMock),
        patch("agentshore.core.orchestrator.setup_logging"),
    ):
        mock_ds = AsyncMock()
        mock_ds_cls.return_value = mock_ds

        from agentshore.core import Orchestrator

        await Orchestrator.bootstrap(cfg=cfg, repo_root=tmp_path, state_provider=provider)

    phases_started = [p for p, s, _ in phase_events if s == "started"]
    phases_completed = [p for p, s, _ in phase_events if s == "completed"]
    for required in ("init_datastore", "init_executor", "init_metrics"):
        assert required in phases_started, f"missing started for {required}"
        assert required in phases_completed, f"missing completed for {required}"

    # Final synthetic "ready/completed" event dismisses the dashboard loading modal.
    assert ("ready", "completed", 0.0) in phase_events
