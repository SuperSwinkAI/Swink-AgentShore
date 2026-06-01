"""desktop-3fiu: per-play-type timeout overrides + classified timeout event.

Pins:
- ``RuntimeConfig.effective_play_timeout`` returns the per-play override
  when present, ``agent_timeout`` otherwise.
- ``AgentManager.dispatch`` resolves the effective timeout per play type
  and passes it through to ``dispatch_cli``.
- On timeout, ``AgentManager`` emits ``agent_dispatch_timeout_classified``
  with structured ``play_type`` / ``tier`` / ``elapsed_seconds`` fields so
  the histogram is queryable without re-parsing the freeform ``error``
  message on ``agent_dispatch_timed_out``.

Tracks the 4-timeout-per-95-min pattern from session 2b8729bf where the
fleet kept hitting the global 1800s ceiling on ``issue_pickup`` and
``unblock_pr`` plays. Per-play headroom + a classified event together
give us the data to size the actual play timeout distribution instead of
guessing.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from agentshore.agents.manager import AgentManager
from agentshore.config import AgentConfig, RuntimeConfig
from agentshore.data.store import DataStore, SessionRecord
from agentshore.errors import AgentTimeout
from agentshore.state import AgentType

if TYPE_CHECKING:
    from pathlib import Path

SESSION_ID = "test-session-timeout"


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> DataStore:
    s = DataStore(tmp_path / "agentshore.db")
    await s.initialize()
    await s.create_session(
        SessionRecord(
            session_id=SESSION_ID,
            project_path=str(tmp_path),
            started_at="2026-05-20T00:00:00+00:00",
        )
    )
    yield s
    await s.close()


def _make_manager(
    store: DataStore,
    tmp_path: Path,
    *,
    play_timeouts: dict[str, int] | None = None,
    agent_timeout: int = 1800,
    mock_binary: str | None = None,
) -> AgentManager:
    agents: dict[str, AgentConfig] = {}
    if mock_binary:
        # No per-agent timeout — we want play_timeouts to win.
        agents["codex"] = AgentConfig(enabled=True, binary=mock_binary)
    cfg = RuntimeConfig(
        agents=agents,
        agent_timeout=agent_timeout,
        play_timeouts=play_timeouts or {},
    )
    return AgentManager(
        session_id=SESSION_ID,
        store=store,
        cfg=cfg,
        working_dir=tmp_path,
        python_executable=sys.executable,
    )


# ---------------------------------------------------------------------------
# Per-play timeout resolution
# ---------------------------------------------------------------------------


def test_effective_play_timeout_falls_back_to_global() -> None:
    cfg = RuntimeConfig(agent_timeout=1800)
    assert cfg.effective_play_timeout("issue_pickup") == 1800
    assert cfg.effective_play_timeout(None) == 1800
    assert cfg.effective_play_timeout("merge_pr") == 1800


def test_effective_play_timeout_per_play_override_wins() -> None:
    cfg = RuntimeConfig(
        agent_timeout=1800,
        play_timeouts={"issue_pickup": 3600, "unblock_pr": 5400},
    )
    assert cfg.effective_play_timeout("issue_pickup") == 3600
    assert cfg.effective_play_timeout("unblock_pr") == 5400
    # Plays not in the map keep the global fallback.
    assert cfg.effective_play_timeout("merge_pr") == 1800
    assert cfg.effective_play_timeout(None) == 1800


def test_play_timeouts_frozen_after_init() -> None:
    """The frozen dataclass invariant still holds — the mapping is immutable."""
    cfg = RuntimeConfig(play_timeouts={"issue_pickup": 3600})
    with pytest.raises(TypeError):
        cfg.play_timeouts["unblock_pr"] = 5400  # type: ignore[index]


# ---------------------------------------------------------------------------
# Manager dispatch threads play_type to dispatch_cli
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_uses_play_timeout_when_set(
    store: DataStore, tmp_path: Path, mock_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``manager.dispatch(play_type="issue_pickup")`` passes the override
    through to ``dispatch_cli`` via ``default_timeout``."""
    mgr = _make_manager(
        store,
        tmp_path,
        mock_binary=str(mock_agent_path),
        play_timeouts={"issue_pickup": 3600},
        agent_timeout=1800,
    )
    handle = await mgr.instantiate(AgentType.CODEX)
    monkeypatch.setattr("agentshore.agents.manager.resolve_identity_env", lambda *_a, **_k: {})
    monkeypatch.setattr("agentshore.agents.manager.verify_identity_repo_access", lambda *_a: None)

    captured: dict[str, object] = {}

    async def _fake_dispatch_cli(*_args: object, **kwargs: object) -> object:
        captured["default_timeout"] = kwargs.get("default_timeout")
        from agentshore.agents.handle import AgentInvocationResult

        return AgentInvocationResult(
            raw_output='{"result":"ok"}',
            tokens_in=10,
            tokens_out=10,
            dollar_cost=0.01,
            duration_ms=10,
            exit_code=0,
        )

    monkeypatch.setattr("agentshore.agents.manager.dispatch_cli", _fake_dispatch_cli)

    await mgr.dispatch(handle.agent_id, "prompt", play_type="issue_pickup")
    assert captured["default_timeout"] == 3600


@pytest.mark.asyncio
async def test_dispatch_falls_back_to_agent_timeout(
    store: DataStore, tmp_path: Path, mock_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _make_manager(
        store,
        tmp_path,
        mock_binary=str(mock_agent_path),
        play_timeouts={"issue_pickup": 3600},
        agent_timeout=1800,
    )
    handle = await mgr.instantiate(AgentType.CODEX)
    monkeypatch.setattr("agentshore.agents.manager.resolve_identity_env", lambda *_a, **_k: {})
    monkeypatch.setattr("agentshore.agents.manager.verify_identity_repo_access", lambda *_a: None)

    captured: dict[str, object] = {}

    async def _fake_dispatch_cli(*_args: object, **kwargs: object) -> object:
        captured["default_timeout"] = kwargs.get("default_timeout")
        from agentshore.agents.handle import AgentInvocationResult

        return AgentInvocationResult(
            raw_output='{"result":"ok"}',
            tokens_in=10,
            tokens_out=10,
            dollar_cost=0.01,
            duration_ms=10,
            exit_code=0,
        )

    monkeypatch.setattr("agentshore.agents.manager.dispatch_cli", _fake_dispatch_cli)

    # merge_pr has no override → falls back to agent_timeout (1800).
    await mgr.dispatch(handle.agent_id, "prompt", play_type="merge_pr")
    assert captured["default_timeout"] == 1800


# ---------------------------------------------------------------------------
# Classified timeout event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_emits_classified_event(
    store: DataStore,
    tmp_path: Path,
    mock_agent_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When AgentTimeout fires, the manager emits BOTH events:
    - agent_dispatch_timed_out (the historical signal)
    - agent_dispatch_timeout_classified with play_type / tier / elapsed_seconds
    """
    mgr = _make_manager(
        store,
        tmp_path,
        mock_binary=str(mock_agent_path),
        play_timeouts={"issue_pickup": 3600},
    )
    handle = await mgr.instantiate(AgentType.CODEX, model_tier="medium")
    monkeypatch.setattr("agentshore.agents.manager.resolve_identity_env", lambda *_a, **_k: {})
    monkeypatch.setattr("agentshore.agents.manager.verify_identity_repo_access", lambda *_a: None)

    async def _raise_timeout(*_args: object, **_kwargs: object) -> object:
        raise AgentTimeout("simulated timeout")

    monkeypatch.setattr("agentshore.agents.manager.dispatch_cli", _raise_timeout)

    captured: list[tuple[str, dict[str, object]]] = []

    def _capture(event: str, **kwargs: object) -> None:
        captured.append((event, kwargs))

    monkeypatch.setattr("agentshore.agents.manager._logger.warning", _capture)

    with pytest.raises(AgentTimeout):
        await mgr.dispatch(handle.agent_id, "prompt", play_type="issue_pickup")

    events = {event: kwargs for event, kwargs in captured}
    assert "agent_dispatch_timed_out" in events
    assert "agent_dispatch_timeout_classified" in events

    classified = events["agent_dispatch_timeout_classified"]
    assert classified["play_type"] == "issue_pickup"
    assert classified["tier"] == "medium"
    assert classified["agent_type"] == "codex"
    assert classified["effective_timeout"] == 3600
    elapsed = classified["elapsed_seconds"]
    assert isinstance(elapsed, float)
    assert elapsed >= 0.0
    assert classified["error_class"] == "timeout_transient"
    assert classified["timeout_count"] == 1


@pytest.mark.asyncio
async def test_timeout_classified_event_omits_play_type_when_unknown(
    store: DataStore,
    tmp_path: Path,
    mock_agent_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifecycle / test dispatch without a play_type still emits the classified
    event — the field is ``None`` rather than missing, so downstream filters
    can identify the case."""
    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CODEX)
    monkeypatch.setattr("agentshore.agents.manager.resolve_identity_env", lambda *_a, **_k: {})
    monkeypatch.setattr("agentshore.agents.manager.verify_identity_repo_access", lambda *_a: None)

    async def _raise_timeout(*_args: object, **_kwargs: object) -> object:
        raise AgentTimeout("no play type")

    monkeypatch.setattr("agentshore.agents.manager.dispatch_cli", _raise_timeout)

    captured: list[tuple[str, dict[str, object]]] = []

    def _capture(event: str, **kwargs: object) -> None:
        captured.append((event, kwargs))

    monkeypatch.setattr("agentshore.agents.manager._logger.warning", _capture)

    with pytest.raises(AgentTimeout):
        await mgr.dispatch(handle.agent_id, "prompt")

    classified = next(
        kwargs for event, kwargs in captured if event == "agent_dispatch_timeout_classified"
    )
    assert classified["play_type"] is None
    assert classified["effective_timeout"] == 1800
