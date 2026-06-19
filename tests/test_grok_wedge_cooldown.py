"""Tests for #202 bounded-backoff Grok launch-wedge suppression.

A Grok first-byte launch wedge used to permanently disable the agent type for
the session via the grow-only ``last_auth_failed_types`` set. These tests pin the
new behavior: a wedge records a DECAYING cooldown that expires after
``_GROK_WEDGE_COOLDOWN_TICKS`` ticks (type re-eligible), while a genuine
``ErrorClass.AUTH`` failure stays permanent.
"""

from __future__ import annotations

from types import SimpleNamespace

from agentshore.agents.manager import _GROK_WEDGE_COOLDOWN_TICKS, _is_grok_launch_wedge_timeout
from agentshore.core.mixins.state import StateBuilder
from agentshore.core.session_runtime import SessionRuntime
from agentshore.errors import ErrorClass
from agentshore.plays.base import PlayParams
from agentshore.plays.candidates import (
    PlayCandidate,
    _candidate_wedge_cooldown_type,
)
from agentshore.state import AgentType, PlayType

# ---------------------------------------------------------------------------
# Wedge detection (manager helper)
# ---------------------------------------------------------------------------


def _grok_handle() -> SimpleNamespace:
    return SimpleNamespace(
        agent_type=AgentType.GROK,
        last_error_class=ErrorClass.TIMEOUT_STREAM_IDLE,
    )


def test_grok_launch_wedge_detected() -> None:
    handle = _grok_handle()
    exc = Exception("grok agent never produced first byte (launch wedge after 120s)")
    assert _is_grok_launch_wedge_timeout(handle, exc) is True


def test_non_grok_timeout_is_not_a_wedge() -> None:
    handle = SimpleNamespace(
        agent_type=AgentType.CODEX,
        last_error_class=ErrorClass.TIMEOUT_STREAM_IDLE,
    )
    exc = Exception("never produced first byte launch wedge")
    assert _is_grok_launch_wedge_timeout(handle, exc) is False


# ---------------------------------------------------------------------------
# Cooldown drain/decay (state builder)
# ---------------------------------------------------------------------------


def _fake_builder(manager: object, runtime: SessionRuntime) -> StateBuilder:
    builder = StateBuilder.__new__(StateBuilder)
    builder._manager = manager  # type: ignore[attr-defined]
    builder._runtime = runtime  # type: ignore[attr-defined]
    builder._session_id = "s1"  # type: ignore[attr-defined]
    return builder


def test_wedge_records_cooldown_that_expires_after_n_ticks() -> None:
    runtime = SessionRuntime()
    runtime.last_play_id = 100
    manager = SimpleNamespace(wedge_cooldown_types={"grok"})
    builder = _fake_builder(manager, runtime)

    # First drain at tick 100: seeds an active cooldown expiring at 100 + N.
    active = builder._drain_wedge_cooldowns()
    assert active == frozenset({"grok"})
    assert runtime.wedge_cooldown_until["grok"] == 100 + _GROK_WEDGE_COOLDOWN_TICKS

    # Mid-cooldown: still active, not re-seeded to a later expiry.
    runtime.last_play_id = 100 + _GROK_WEDGE_COOLDOWN_TICKS - 1
    active_mid = builder._drain_wedge_cooldowns()
    assert active_mid == frozenset({"grok"})
    assert runtime.wedge_cooldown_until["grok"] == 100 + _GROK_WEDGE_COOLDOWN_TICKS

    # At expiry tick: cooldown drops, type becomes re-eligible.
    runtime.last_play_id = 100 + _GROK_WEDGE_COOLDOWN_TICKS
    active_after = builder._drain_wedge_cooldowns()
    assert active_after == frozenset()
    assert "grok" not in runtime.wedge_cooldown_until


def test_wedge_cooldown_is_not_permanent_auth_suppression() -> None:
    """A wedge must NEVER feed the permanent auth-suppression set."""
    runtime = SessionRuntime()
    runtime.last_play_id = 0
    manager = SimpleNamespace(wedge_cooldown_types={"grok"})
    builder = _fake_builder(manager, runtime)
    builder._drain_wedge_cooldowns()
    # Permanent set is untouched by the wedge path.
    assert runtime.auth_suppressed_agent_types == set()


def test_drain_tolerates_manager_without_attribute() -> None:
    runtime = SessionRuntime()
    builder = _fake_builder(SimpleNamespace(), runtime)
    assert builder._drain_wedge_cooldowns() == frozenset()


def test_drain_seeds_cooldown_for_agy_stream_hang_cluster() -> None:
    """#233: an agy stream-hang cluster feeds the SAME decaying cooldown set as a
    Grok launch wedge (with its own reason tag), so it auto-recovers identically."""
    runtime = SessionRuntime()
    runtime.last_play_id = 5
    manager = SimpleNamespace(
        wedge_cooldown_types={"antigravity"},
        wedge_cooldown_reasons={"antigravity": "stream_hang_cluster"},
    )
    builder = _fake_builder(manager, runtime)

    active = builder._drain_wedge_cooldowns()
    assert active == frozenset({"antigravity"})
    assert runtime.wedge_cooldown_until["antigravity"] == 5 + _GROK_WEDGE_COOLDOWN_TICKS
    # Decaying cooldown only — never the permanent auth-suppression set.
    assert runtime.auth_suppressed_agent_types == set()


# ---------------------------------------------------------------------------
# Candidate masking (decaying)
# ---------------------------------------------------------------------------


def _candidate(play_type: PlayType, params: PlayParams) -> PlayCandidate:
    return PlayCandidate(
        play_type=play_type,
        params=params,
        resource_keys=(),
        source="test",
        sort_key=(0,),
    )


def test_wedge_cooldown_masks_matching_type_only() -> None:
    cooldown = frozenset({"grok"})
    agent_id_to_type = {"grok-1": "grok", "claude-1": "claude"}

    grok_instantiate = _candidate(PlayType.INSTANTIATE_AGENT, PlayParams(target_agent_type="grok"))
    assert _candidate_wedge_cooldown_type(grok_instantiate, cooldown, agent_id_to_type) == "grok"

    claude_instantiate = _candidate(
        PlayType.INSTANTIATE_AGENT, PlayParams(target_agent_type="claude")
    )
    assert _candidate_wedge_cooldown_type(claude_instantiate, cooldown, agent_id_to_type) is None


def test_wedge_cooldown_empty_set_masks_nothing() -> None:
    grok_instantiate = _candidate(PlayType.INSTANTIATE_AGENT, PlayParams(target_agent_type="grok"))
    assert _candidate_wedge_cooldown_type(grok_instantiate, frozenset(), {}) is None
