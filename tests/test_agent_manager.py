"""Tests for AgentManager — lifecycle, dispatch, circuit breaker, authorship cache."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from agentshore.agents.handle import AgentInvocationResult
from agentshore.agents.manager import _PLACEHOLDER_COLD_START_COST, AgentManager
from agentshore.config import AgentConfig, GitHubIdentity, RuntimeConfig
from agentshore.data.store import DataStore, SessionRecord
from agentshore.errors import (
    AgentAuthError,
    AgentTimeout,
    ErrorClass,
    PlayTimeoutError,
    PreconditionFailed,
)
from agentshore.result_parser import parse_skill_result
from agentshore.state import AgentStatus, AgentType, PlayType

if TYPE_CHECKING:
    from pathlib import Path

SESSION_ID = "test-session-1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> DataStore:
    s = DataStore(tmp_path / "agentshore.db")
    await s.initialize()
    await s.create_session(
        SessionRecord(
            session_id=SESSION_ID,
            project_path=str(tmp_path),
            started_at="2026-04-27T00:00:00+00:00",
        )
    )
    yield s
    await s.close()


def _make_manager(
    store: DataStore,
    tmp_path: Path,
    *,
    mock_binary: str | None = None,
) -> AgentManager:
    agents: dict[str, AgentConfig] = {}
    if mock_binary:
        for key in ("codex", "claude_code"):
            agents[key] = AgentConfig(enabled=True, binary=mock_binary, timeout=10)  # type: ignore[assignment]
    return AgentManager(
        session_id=SESSION_ID,
        store=store,
        cfg=RuntimeConfig(agents=agents),
        working_dir=tmp_path,
        python_executable=sys.executable,
    )


# ---------------------------------------------------------------------------
# instantiate
# ---------------------------------------------------------------------------


async def test_instantiate_registers_agent_record(
    store: DataStore, tmp_path: Path, mock_agent_path: Path
) -> None:
    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CODEX)

    assert handle.agent_id in mgr.handles
    assert handle.status == AgentStatus.IDLE
    assert handle.agent_type == AgentType.CODEX

    # Check persisted record
    agent_id = handle.agent_id
    async with store._conn.execute(
        "SELECT agent_id, session_id, agent_type FROM agents WHERE agent_id = ?",
        (agent_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert row[0] == agent_id
    assert row[1] == SESSION_ID
    assert row[2] == AgentType.CODEX.value


async def test_instantiate_creates_circuit_breaker(
    store: DataStore, tmp_path: Path, mock_agent_path: Path
) -> None:
    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CODEX)
    assert handle.agent_id in mgr._circuit_breakers


async def test_instantiate_records_model_tier_metadata(
    store: DataStore, tmp_path: Path, mock_agent_path: Path
) -> None:
    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CODEX, model_tier="small")

    assert handle.model_tier == "small"
    assert handle.model == "gpt-5.4-mini"
    assert handle.reasoning_effort == "low"
    assert handle.display_name.startswith("Codex/gpt-5.4-mini:")


async def test_instantiate_marks_agent_auth_error_when_repo_preflight_fails(
    store: DataStore,
    tmp_path: Path,
    mock_agent_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents = {
        "codex": AgentConfig(
            enabled=True,
            binary=str(mock_agent_path),
            timeout=5,
            identity="bot",
        )
    }
    mgr = AgentManager(
        session_id=SESSION_ID,
        store=store,
        cfg=RuntimeConfig(agents=agents),
        working_dir=tmp_path,
        python_executable=sys.executable,
    )
    monkeypatch.setattr(
        "agentshore.agents.manager.resolved_github_login_for_agent",
        lambda *_args, **_kwargs: "bot",
    )
    monkeypatch.setattr(
        "agentshore.agents.manager.resolve_identity_env",
        lambda *_args, **_kwargs: {"GH_TOKEN": "ghp_test", "GITHUB_TOKEN": "ghp_test"},
    )
    monkeypatch.setattr(
        "agentshore.agents.manager.verify_identity_repo_access",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AgentAuthError("repo denied")),
    )

    handle = await mgr.instantiate(AgentType.CODEX)

    assert handle.status == AgentStatus.ERROR
    assert handle.last_error_class == "auth"
    # #zeke auth-hang: a preflight AUTH also records the agent TYPE for session
    # suppression so the state-builder mixin can mask all codex dispatch.
    assert mgr.last_auth_failed_types == {"codex"}


async def test_mark_agent_error_auth_records_type_for_session_suppression(
    store: DataStore, tmp_path: Path, mock_agent_path: Path
) -> None:
    """An AUTH mark_agent_error records the agent type; a non-AUTH mark doesn't."""
    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CODEX)

    await mgr.mark_agent_error(handle.agent_id, ErrorClass.AUTH, reason="backend token expired")
    assert handle.last_error_class == ErrorClass.AUTH
    assert mgr.last_auth_failed_types == {"codex"}


async def test_mark_agent_error_non_auth_does_not_suppress_type(
    store: DataStore, tmp_path: Path, mock_agent_path: Path
) -> None:
    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CODEX)

    await mgr.mark_agent_error(handle.agent_id, ErrorClass.TIMEOUT, reason="slow")
    assert mgr.last_auth_failed_types == set()


# ---------------------------------------------------------------------------
# dispatch — end-to-end via mock agent
# ---------------------------------------------------------------------------


async def test_dispatch_success_updates_handle_and_store(
    store: DataStore, tmp_path: Path, mock_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full e2e: instantiate → dispatch → parse → verify counters.

    Uses stream_json format (Claude Code) so that token/cost metadata is present.
    """
    monkeypatch.setenv("MOCK_AGENT_FORMAT", "stream_json")
    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CLAUDE_CODE)
    agent_id = handle.agent_id
    # A prior stream-idle timeout left the storm counter elevated; a successful
    # dispatch must reset it so a one-off stall doesn't accumulate toward the
    # bench limit (#161).
    handle.consecutive_timeouts = 3

    result = await mgr.dispatch(agent_id, "do the work")

    assert result.exit_code == 0
    assert result.tokens_in == 500
    assert result.tokens_out == 200
    assert handle.consecutive_timeouts == 0

    # Parse the raw output using the shared result parser
    sr = parse_skill_result(result.raw_output)
    assert sr.success is True
    assert len(sr.artifacts) == 1
    assert sr.artifacts[0]["number"] == 42  # type: ignore[index]

    # Handle counters updated
    assert handle.total_tokens == 700  # 500 in + 200 out
    assert handle.total_cost > 0.0
    assert handle.status == AgentStatus.IDLE

    # DataStore stats updated
    cursor = await store._conn.execute(
        "SELECT tasks_completed, tasks_failed FROM agents WHERE agent_id = ?",
        (agent_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == 1
    assert row[1] == 0


async def test_dispatch_failure_increments_failed_tasks(
    store: DataStore, tmp_path: Path, mock_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_AGENT_MODE", "failure")
    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CODEX)
    agent_id = handle.agent_id

    result = await mgr.dispatch(agent_id, "do the work")
    # failure mode exits 0 — result is returned, not raised
    sr = parse_skill_result(result.raw_output)
    assert sr.success is False

    cursor = await store._conn.execute(
        "SELECT tasks_completed, tasks_failed FROM agents WHERE agent_id = ?",
        (agent_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == 1  # dispatch returned normally (exit 0)
    assert row[1] == 0


async def test_dispatch_nonzero_exit_increments_failed_and_sets_error_status(
    store: DataStore, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A script that exits 1 → AgentProcessError; handle becomes ERROR."""
    script = tmp_path_factory.mktemp("scripts") / "exit1.py"
    script.write_text("import sys; sys.exit(1)\n", encoding="utf-8")

    agents = {"codex": AgentConfig(enabled=True, binary=str(script), timeout=5)}
    mgr = AgentManager(
        session_id=SESSION_ID,
        store=store,
        cfg=RuntimeConfig(agents=agents),
        working_dir=tmp_path,
        python_executable=sys.executable,
    )
    handle = await mgr.instantiate(AgentType.CODEX)
    agent_id = handle.agent_id

    from agentshore.errors import AgentProcessError

    with pytest.raises(AgentProcessError):
        await mgr.dispatch(agent_id, "prompt")

    assert handle.status == AgentStatus.ERROR
    cursor = await store._conn.execute(
        "SELECT tasks_failed FROM agents WHERE agent_id = ?", (agent_id,)
    )
    row = await cursor.fetchone()
    assert row[0] == 1


async def test_dispatch_emits_subprocess_callbacks(
    store: DataStore,
    tmp_path: Path,
    mock_agent_path: Path,
) -> None:
    spawned: list[tuple[str, AgentType, int]] = []
    exited: list[tuple[str, AgentType, int, int | None]] = []

    async def on_spawned(agent_id: str, agent_type: AgentType, pid: int) -> None:
        spawned.append((agent_id, agent_type, pid))

    async def on_exited(
        agent_id: str, agent_type: AgentType, pid: int, exit_code: int | None
    ) -> None:
        exited.append((agent_id, agent_type, pid, exit_code))

    mgr = AgentManager(
        session_id=SESSION_ID,
        store=store,
        cfg=RuntimeConfig(
            agents={"codex": AgentConfig(enabled=True, binary=str(mock_agent_path), timeout=10)}
        ),
        working_dir=tmp_path,
        python_executable=sys.executable,
        on_subprocess_spawned=on_spawned,
        on_subprocess_exited=on_exited,
    )
    handle = await mgr.instantiate(AgentType.CODEX)
    await mgr.dispatch(handle.agent_id, "do the work")

    assert len(spawned) == 1
    assert len(exited) == 1
    assert spawned[0][0] == handle.agent_id
    assert spawned[0][1] == AgentType.CODEX
    assert exited[0][0] == handle.agent_id
    assert exited[0][1] == AgentType.CODEX


async def test_instantiate_missing_configured_identity_token_marks_auth_error(
    store: DataStore,
    tmp_path: Path,
    mock_agent_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The identity overlay is resolved once at instantiate(); a missing token
    # fails that one-time preflight (no per-dispatch re-resolution) and leaves
    # the handle ERROR/auth without registering it in the manager.
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    agents = {
        "codex": AgentConfig(
            enabled=True,
            binary=str(mock_agent_path),
            timeout=5,
            identity="bot",
        )
    }
    cfg = RuntimeConfig(
        agents=agents,
        identities={
            "bot": GitHubIdentity(
                git_user_name="Bot",
                git_user_email="bot@example.com",
                gh_token_env="BOT_TOKEN",
            )
        },
    )
    mgr = AgentManager(
        session_id=SESSION_ID,
        store=store,
        cfg=cfg,
        working_dir=tmp_path,
        python_executable=sys.executable,
    )
    handle = await mgr.instantiate(AgentType.CODEX)

    assert handle.status == AgentStatus.ERROR
    assert handle.last_error_class == "auth"
    # Half-constructed agent must not be registered (H5).
    assert handle.agent_id not in mgr.handles
    assert handle.agent_id not in mgr.circuit_breakers


async def test_instantiate_repo_preflight_failure_does_not_register_agent(
    store: DataStore,
    tmp_path: Path,
    mock_agent_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents = {
        "codex": AgentConfig(
            enabled=True,
            binary=str(mock_agent_path),
            timeout=5,
            identity="bot",
        )
    }
    mgr = AgentManager(
        session_id=SESSION_ID,
        store=store,
        cfg=RuntimeConfig(agents=agents),
        working_dir=tmp_path,
        python_executable=sys.executable,
    )
    monkeypatch.setattr(
        "agentshore.agents.manager.resolved_github_login_for_agent",
        lambda *_args, **_kwargs: "bot",
    )
    monkeypatch.setattr(
        "agentshore.agents.manager.resolve_identity_env",
        lambda *_args, **_kwargs: {"GH_TOKEN": "ghp_test", "GITHUB_TOKEN": "ghp_test"},
    )
    calls = 0

    def repo_preflight(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise AgentAuthError("repo denied")

    monkeypatch.setattr("agentshore.agents.manager.verify_identity_repo_access", repo_preflight)
    dispatch_cli = AsyncMock()
    monkeypatch.setattr("agentshore.agents.manager.dispatch_cli", dispatch_cli)

    handle = await mgr.instantiate(AgentType.CODEX)

    # Repo access is verified exactly once at instantiate(), not per dispatch (C2).
    assert calls == 1
    assert handle.status == AgentStatus.ERROR
    assert handle.last_error_class == "auth"
    # H5: preflight failure leaves no live, dispatchable handle behind.
    assert handle.agent_id not in mgr.handles
    assert handle.agent_id not in mgr.circuit_breakers
    with pytest.raises(PreconditionFailed):
        await mgr.dispatch(handle.agent_id, "prompt")
    assert dispatch_cli.await_count == 0


@pytest.mark.parametrize(
    ("error_class", "message"),
    [
        ("timeout_stream_idle", "idle"),
        ("timeout_wallclock", "wallclock"),
    ],
)
async def test_dispatch_preserves_play_timeout_error_class(
    store: DataStore,
    tmp_path: Path,
    mock_agent_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_class: str,
    message: str,
) -> None:
    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CODEX)
    monkeypatch.setattr(
        "agentshore.agents.manager.resolve_identity_env", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        "agentshore.agents.manager.verify_identity_repo_access", lambda *_args: None
    )
    dispatch_cli = AsyncMock(side_effect=PlayTimeoutError(message, error_class=error_class))
    monkeypatch.setattr("agentshore.agents.manager.dispatch_cli", dispatch_cli)

    with pytest.raises(PlayTimeoutError):
        await mgr.dispatch(handle.agent_id, "prompt")

    assert handle.status == AgentStatus.IDLE
    assert handle.last_error_class == error_class
    assert handle.timeout_count == 1


async def test_dispatch_timeout_is_transient_and_keeps_agent_idle(
    store: DataStore, tmp_path: Path, mock_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CODEX)

    async def _raise_timeout(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AgentTimeout("timed out")

    monkeypatch.setattr("agentshore.agents.manager.dispatch_cli", _raise_timeout)
    with pytest.raises(AgentTimeout):
        await mgr.dispatch(handle.agent_id, "prompt")

    assert handle.status == AgentStatus.IDLE
    assert handle.last_error_class == "timeout_transient"
    assert handle.timeout_count == 1
    assert handle.consecutive_timeouts == 1


async def test_dispatch_stamps_busy_watchdog_deadline(
    store: DataStore, tmp_path: Path, mock_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``dispatch`` stamps a future busy-watchdog deadline before going BUSY.

    The HealthMonitor reads ``dispatch_deadline_monotonic`` to reap an agent
    whose dispatch hung in BUSY (session a3202694). Verify it is set, in the
    future, and observable from inside the dispatch (i.e. while the agent is
    BUSY), not just after completion.
    """
    import time as _time

    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CODEX)
    assert handle.dispatch_deadline_monotonic is None  # nothing stamped yet

    captured: dict[str, float | None] = {}

    async def _capture(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["deadline"] = handle.dispatch_deadline_monotonic
        captured["status_is_busy"] = handle.status == AgentStatus.BUSY
        raise AgentTimeout("timed out")

    monkeypatch.setattr("agentshore.agents.manager.dispatch_cli", _capture)
    before = _time.monotonic()
    with pytest.raises(AgentTimeout):
        await mgr.dispatch(handle.agent_id, "prompt")

    assert captured["status_is_busy"] is True
    assert captured["deadline"] is not None
    # Deadline is the effective timeout + margin into the future, never the past.
    assert captured["deadline"] > before


async def test_grok_first_byte_launch_wedge_records_cooldown_not_permanent_suppression(
    store: DataStore, tmp_path: Path, mock_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = AgentManager(
        session_id=SESSION_ID,
        store=store,
        cfg=RuntimeConfig(
            agents={"grok": AgentConfig(enabled=True, binary=str(mock_agent_path), timeout=10)}
        ),
        working_dir=tmp_path,
        python_executable=sys.executable,
    )
    handle = await mgr.instantiate(AgentType.GROK)
    dispatch_cli = AsyncMock(
        side_effect=PlayTimeoutError(
            "agent 'grok-1' (grok/medium, prompt_bytes=10054) never produced first byte "
            "within 120s (launch wedge)",
            error_class=ErrorClass.TIMEOUT_STREAM_IDLE,
        )
    )
    monkeypatch.setattr("agentshore.agents.manager.dispatch_cli", dispatch_cli)

    with pytest.raises(PlayTimeoutError):
        await mgr.dispatch(handle.agent_id, "prompt")

    assert handle.status == AgentStatus.IDLE
    assert handle.last_error_class == ErrorClass.TIMEOUT_STREAM_IDLE
    assert handle.timeout_count == 1
    assert handle.consecutive_timeouts == 1
    # #202: a launch wedge records a DECAYING cooldown, NOT the permanent
    # auth-suppression set — the type must auto-recover after the cooldown.
    assert mgr.wedge_cooldown_types == {"grok"}
    assert mgr.last_auth_failed_types == set()


async def test_grok_stream_idle_timeout_without_launch_wedge_does_not_suppress_type(
    store: DataStore, tmp_path: Path, mock_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = AgentManager(
        session_id=SESSION_ID,
        store=store,
        cfg=RuntimeConfig(
            agents={"grok": AgentConfig(enabled=True, binary=str(mock_agent_path), timeout=10)}
        ),
        working_dir=tmp_path,
        python_executable=sys.executable,
    )
    handle = await mgr.instantiate(AgentType.GROK)
    dispatch_cli = AsyncMock(
        side_effect=PlayTimeoutError(
            "agent 'grok-1' (grok/medium) stopped producing stdout for 120s",
            error_class=ErrorClass.TIMEOUT_STREAM_IDLE,
        )
    )
    monkeypatch.setattr("agentshore.agents.manager.dispatch_cli", dispatch_cli)

    with pytest.raises(PlayTimeoutError):
        await mgr.dispatch(handle.agent_id, "prompt")

    assert handle.last_error_class == ErrorClass.TIMEOUT_STREAM_IDLE
    assert mgr.last_auth_failed_types == set()
    # A plain stream-idle timeout (no launch-wedge markers) is not a wedge.
    assert mgr.wedge_cooldown_types == set()
    # #233: but it does start a per-type stream-hang streak (one is not a cluster).
    assert mgr._type_stream_hang_streak["grok"] == 1


async def test_antigravity_stream_hang_cluster_trips_decaying_cooldown(
    store: DataStore, tmp_path: Path, mock_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#233: a cluster of agy zero-stdout 1800s hangs benches the TYPE into the same
    decaying wedge cooldown (NOT permanent auth suppression, and without lowering the
    load-bearing 1800s first-byte deadline)."""
    from agentshore.agents.manager import _STREAM_HANG_CLUSTER_LIMIT
    from agentshore.config import CircuitBreakerConfig

    mgr = AgentManager(
        session_id=SESSION_ID,
        store=store,
        cfg=RuntimeConfig(
            agents={
                "antigravity": AgentConfig(enabled=True, binary=str(mock_agent_path), timeout=10)
            },
            # Decouple from the circuit breaker so consecutive timeouts don't trip it.
            circuit_breaker=CircuitBreakerConfig(failures=_STREAM_HANG_CLUSTER_LIMIT + 5),
        ),
        working_dir=tmp_path,
        python_executable=sys.executable,
    )
    handle = await mgr.instantiate(AgentType.ANTIGRAVITY)
    atype = AgentType.ANTIGRAVITY.value
    dispatch_cli = AsyncMock(
        side_effect=PlayTimeoutError(
            f"agent {handle.agent_id!r} (antigravity/medium) never produced any stdout for 1800s",
            error_class=ErrorClass.TIMEOUT_STREAM_IDLE,
        )
    )
    monkeypatch.setattr("agentshore.agents.manager.dispatch_cli", dispatch_cli)

    # Below the cluster limit: the streak builds but the type is not yet benched.
    for _ in range(_STREAM_HANG_CLUSTER_LIMIT - 1):
        with pytest.raises(PlayTimeoutError):
            await mgr.dispatch(handle.agent_id, "prompt")
    assert mgr.wedge_cooldown_types == set()
    assert mgr._type_stream_hang_streak[atype] == _STREAM_HANG_CLUSTER_LIMIT - 1

    # The cluster-limit-th hang benches the whole agy type.
    with pytest.raises(PlayTimeoutError):
        await mgr.dispatch(handle.agent_id, "prompt")
    assert mgr.wedge_cooldown_types == {atype}
    assert mgr.wedge_cooldown_reasons[atype] == "stream_hang_cluster"
    # Decaying cooldown, NOT the permanent auth-suppression set.
    assert mgr.last_auth_failed_types == set()
    # Streak resets after tripping so the cooldown re-arms cleanly on recurrence.
    assert mgr._type_stream_hang_streak[atype] == 0


async def test_successful_dispatch_resets_type_stream_hang_streak(
    store: DataStore, tmp_path: Path, mock_agent_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#233: any productive dispatch of a type clears its stream-hang streak so a
    recovered backend isn't benched on a stale count."""
    monkeypatch.setenv("MOCK_AGENT_FORMAT", "stream_json")
    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CLAUDE_CODE)
    mgr._type_stream_hang_streak[handle.agent_type.value] = 2

    await mgr.dispatch(handle.agent_id, "do the work")

    assert mgr._type_stream_hang_streak[handle.agent_type.value] == 0


# ---------------------------------------------------------------------------
# Circuit breaker integration
# ---------------------------------------------------------------------------


async def test_dispatch_blocked_when_circuit_breaker_open(
    store: DataStore, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Three consecutive failures should open the breaker; next dispatch raises."""
    script = tmp_path_factory.mktemp("scripts") / "exit1.py"
    script.write_text("import sys; sys.exit(1)\n", encoding="utf-8")

    agents = {"codex": AgentConfig(enabled=True, binary=str(script), timeout=5)}
    mgr = AgentManager(
        session_id=SESSION_ID,
        store=store,
        cfg=RuntimeConfig(agents=agents),
        working_dir=tmp_path,
        python_executable=sys.executable,
    )
    handle = await mgr.instantiate(AgentType.CODEX)
    agent_id = handle.agent_id

    from agentshore.errors import AgentProcessError

    # Three failures to open the breaker (default threshold = 3)
    for _ in range(3):
        with pytest.raises(AgentProcessError):
            await mgr.dispatch(agent_id, "prompt")
        handle.transition_to(AgentStatus.IDLE)  # reset so dispatch doesn't short-circuit

    # Circuit is now open — next call blocked immediately
    with pytest.raises(PreconditionFailed, match="Circuit breaker OPEN"):
        await mgr.dispatch(agent_id, "prompt")


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


async def test_clear_removes_handle_and_sets_terminated_at(
    store: DataStore, tmp_path: Path, mock_agent_path: Path
) -> None:
    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CODEX)
    agent_id = handle.agent_id

    await mgr.clear(agent_id)

    assert agent_id not in mgr.handles

    cursor = await store._conn.execute(
        "SELECT terminated_at FROM agents WHERE agent_id = ?", (agent_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] is not None  # terminated_at set


async def test_clear_unknown_agent_raises(store: DataStore, tmp_path: Path) -> None:
    mgr = _make_manager(store, tmp_path)
    with pytest.raises(PreconditionFailed, match="Unknown agent_id"):
        await mgr.clear("does-not-exist")


async def test_clear_refuses_to_kill_agent_with_active_play(
    store: DataStore, tmp_path: Path, mock_agent_path: Path
) -> None:
    """clear() without force=True must not kill an agent with an active in-flight play.

    This is the #93 regression guard: reconcile_state was calling clear() on agents
    that were mid-play.  The guard prevents any non-teardown caller from doing that.
    """
    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CODEX)
    agent_id = handle.agent_id

    # Simulate the agent being mid-play (as dispatch() would set it).
    handle.start_play(
        play_type=AgentType.CODEX,  # type: ignore[arg-type]  # value only matters for message
        play_id=42,
        started_at="2026-06-09T00:00:00+00:00",
        issue_number=7,
        pr_number=None,
        branch="feature/foo",
    )
    assert handle.current_play_id == 42

    with pytest.raises(PreconditionFailed, match="active in-flight play"):
        await mgr.clear(agent_id)

    # Agent must still be alive.
    assert agent_id in mgr.handles


async def test_clear_force_bypasses_active_play_guard(
    store: DataStore, tmp_path: Path, mock_agent_path: Path
) -> None:
    """clear(force=True) must succeed even when the agent has an active play.

    This preserves the session-teardown path (drain) which cancels asyncio tasks
    first and then needs to hard-clear every agent regardless of play state.
    """
    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CODEX)
    agent_id = handle.agent_id

    handle.start_play(
        play_type=AgentType.CODEX,  # type: ignore[arg-type]
        play_id=99,
        started_at="2026-06-09T00:00:00+00:00",
        issue_number=None,
        pr_number=None,
        branch=None,
    )
    assert handle.current_play_id == 99

    # Must not raise.
    await mgr.clear(agent_id, force=True)

    assert agent_id not in mgr.handles


async def test_clear_allows_agent_whose_inflight_play_is_end_agent(
    store: DataStore, tmp_path: Path, mock_agent_path: Path
) -> None:
    """An agent whose in-flight play is END_AGENT is clearable without force.

    #154 regression guard: the executor marks the retirement target with the
    end_agent play's own marker before the play body runs, so that marker must
    never block the retirement it belongs to — even if a caller forgets
    force=True.
    """
    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CODEX)
    agent_id = handle.agent_id

    # Simulate the executor's _mark_agent_current_play for an end_agent play.
    handle.start_play(
        play_type=PlayType.END_AGENT,
        play_id=7,
        started_at="2026-06-10T00:00:00+00:00",
        issue_number=None,
        pr_number=None,
        branch=None,
    )
    assert handle.current_play_id == 7

    # Must not raise despite force defaulting to False.
    await mgr.clear(agent_id)

    assert agent_id not in mgr.handles


async def test_active_play_agent_ids_reflects_live_state(
    store: DataStore, tmp_path: Path, mock_agent_path: Path
) -> None:
    """active_play_agent_ids() returns only agents with a non-None current_play_id."""
    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle_a = await mgr.instantiate(AgentType.CODEX)
    handle_b = await mgr.instantiate(AgentType.CODEX)

    # Initially no active plays.
    assert mgr.active_play_agent_ids() == frozenset()

    # Give handle_a an active play.
    handle_a.start_play(
        play_type=AgentType.CODEX,  # type: ignore[arg-type]
        play_id=1,
        started_at="2026-06-09T00:00:00+00:00",
        issue_number=None,
        pr_number=None,
        branch=None,
    )

    active = mgr.active_play_agent_ids()
    assert handle_a.agent_id in active
    assert handle_b.agent_id not in active

    # Give handle_b one too.
    handle_b.start_play(
        play_type=AgentType.CODEX,  # type: ignore[arg-type]
        play_id=2,
        started_at="2026-06-09T00:00:00+00:00",
        issue_number=None,
        pr_number=None,
        branch=None,
    )
    active = mgr.active_play_agent_ids()
    assert handle_a.agent_id in active
    assert handle_b.agent_id in active

    # Clear handle_a's play.
    handle_a.clear_play()
    active = mgr.active_play_agent_ids()
    assert handle_a.agent_id not in active
    assert handle_b.agent_id in active


async def test_clear_handles_concurrent_process_null(
    store: DataStore, tmp_path: Path, mock_agent_path: Path
) -> None:
    """Regression — a concurrent dispatch_cli finally-block can null
    handle.process between the first guard and the second .returncode read.
    The clear() path must not raise `'NoneType' object has no attribute
    'returncode'`. Symptom from a prior session
    (`agent_clear_failed × 2`)."""
    from unittest.mock import AsyncMock, MagicMock

    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CODEX)

    # Simulate a process whose handle.process gets nulled while we're awaiting
    # its .wait() — i.e. mid-clear, a concurrent dispatch finished and reset
    # the handle. The local-binding fix in manager.py must ride through
    # without dereferencing handle.process again.
    fake_proc = MagicMock()
    fake_proc.returncode = None
    fake_proc.terminate = MagicMock()
    fake_proc.kill = MagicMock()

    async def _wait_then_null() -> int:
        # Pretend the racing dispatch finishes and clears the handle.
        handle.process = None
        # Pretend the process exited cleanly during the wait.
        fake_proc.returncode = 0
        return 0

    fake_proc.wait = AsyncMock(side_effect=_wait_then_null)
    handle.process = fake_proc

    # Must not raise.
    await mgr.clear(handle.agent_id)

    assert handle.agent_id not in mgr.handles


# ---------------------------------------------------------------------------
# Authorship cache
# ---------------------------------------------------------------------------


async def test_record_branch_exposure_records_branch_exposure(
    store: DataStore, tmp_path: Path
) -> None:
    mgr = _make_manager(store, tmp_path)
    mgr.record_branch_exposure(branch="feature/foo", agent_id="agent-a")

    assert mgr.branch_exposure["feature/foo"] == "agent-a"


async def test_record_branch_commit_updates_branch_exposure(
    store: DataStore, tmp_path: Path
) -> None:
    mgr = _make_manager(store, tmp_path)
    mgr.record_branch_commit(branch="feature/bar", agent_id="agent-b", sha="abc123")

    assert mgr.branch_exposure["feature/bar"] == "agent-b"


async def test_record_branch_exposure_last_writer_wins(store: DataStore, tmp_path: Path) -> None:
    mgr = _make_manager(store, tmp_path)
    mgr.record_branch_exposure("branch-a", "agent-1")
    mgr.record_branch_exposure("branch-a", "agent-2")

    assert mgr.branch_exposure["branch-a"] == "agent-2"


# ---------------------------------------------------------------------------
# Error recovery
# ---------------------------------------------------------------------------


async def test_attempt_recovery_transitions_error_to_idle(
    store: DataStore, tmp_path: Path, mock_agent_path: Path
) -> None:
    """An ERROR agent whose breaker is HALF_OPEN should recover to IDLE."""
    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CODEX)
    agent_id = handle.agent_id

    # Put agent into ERROR state
    handle.transition_to(AgentStatus.ERROR)

    # Trip the breaker and let the cooldown elapse so it becomes HALF_OPEN.
    cb = mgr.circuit_breakers[agent_id]
    cb.record_failure()
    # Force HALF_OPEN by recording success (resets to CLOSED, which allows dispatch)
    cb.record_success()

    result = await mgr.attempt_recovery(agent_id)

    assert result is True
    assert handle.status == AgentStatus.IDLE


async def test_attempt_recovery_fails_when_breaker_open(
    store: DataStore, tmp_path: Path, mock_agent_path: Path
) -> None:
    """An ERROR agent whose breaker is OPEN should not recover."""
    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CODEX)
    agent_id = handle.agent_id

    handle.transition_to(AgentStatus.ERROR)

    # Trip the breaker so it's OPEN (default threshold = 3)
    cb = mgr.circuit_breakers[agent_id]
    for _ in range(3):
        cb.record_failure()
    assert cb.is_open

    result = await mgr.attempt_recovery(agent_id)

    assert result is False
    assert handle.status == AgentStatus.ERROR


async def test_attempt_recovery_does_not_clear_auth_quarantine(
    store: DataStore, tmp_path: Path, mock_agent_path: Path
) -> None:
    mgr = _make_manager(store, tmp_path, mock_binary=str(mock_agent_path))
    handle = await mgr.instantiate(AgentType.CODEX)
    handle.transition_to(AgentStatus.ERROR)
    handle.last_error_class = ErrorClass.AUTH

    result = await mgr.attempt_recovery(handle.agent_id)

    assert result is False
    assert handle.status == AgentStatus.ERROR
    assert handle.last_error_class == "auth"


# ---------------------------------------------------------------------------
# placeholder cost for no-usage agents (grok / antigravity)
# ---------------------------------------------------------------------------


def _invocation(dollar_cost: float) -> AgentInvocationResult:
    return AgentInvocationResult(
        raw_output="ok",
        tokens_in=0,
        tokens_out=0,
        dollar_cost=dollar_cost,
        duration_ms=1,
        exit_code=0,
    )


def test_placeholder_cost_uses_cold_start_before_any_measured_play(
    store: DataStore, tmp_path: Path
) -> None:
    mgr = _make_manager(store, tmp_path)
    # No measured play yet → no-usage dispatch ($0) is re-billed the cold-start cost.
    out = mgr._apply_placeholder_cost(_invocation(0.0), agent_type=AgentType.ANTIGRAVITY)
    assert out.dollar_cost == pytest.approx(_PLACEHOLDER_COLD_START_COST)


def test_placeholder_cost_is_running_mean_of_measured_plays(
    store: DataStore, tmp_path: Path
) -> None:
    mgr = _make_manager(store, tmp_path)
    # Two measured (usage-reporting) dispatches pass through unchanged and seed the mean.
    a = mgr._apply_placeholder_cost(_invocation(1.00), agent_type=AgentType.CLAUDE_CODE)
    b = mgr._apply_placeholder_cost(_invocation(3.00), agent_type=AgentType.CODEX)
    assert a.dollar_cost == pytest.approx(1.00)
    assert b.dollar_cost == pytest.approx(3.00)
    # A no-usage grok dispatch is billed the mean of the two measured plays (= 2.00).
    grok = mgr._apply_placeholder_cost(_invocation(0.0), agent_type=AgentType.GROK)
    assert grok.dollar_cost == pytest.approx(2.00)
    # Placeholder plays must NOT pollute the measured mean: another no-usage play
    # still sees 2.00, not a drifted value.
    grok2 = mgr._apply_placeholder_cost(_invocation(0.0), agent_type=AgentType.GROK)
    assert grok2.dollar_cost == pytest.approx(2.00)
