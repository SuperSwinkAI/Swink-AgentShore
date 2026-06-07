"""Tests for rl/action_space.py — V1 ordering invariants."""

from __future__ import annotations

import pytest

from agentshore.config.models import (
    AgentConfig,
    ModelTierConfig,
    RuntimeConfig,
)
from agentshore.rl.action_space import (
    ACTION_SPACE_VERSION,
    INDEX_TO_PLAY,
    MAX_CONFIG_INDEX_SIZE,
    NUM_ACTIONS,
    PLAY_TO_INDEX,
    POLICY_VERSION,
    V1_ACTION_ORDER,
    build_config_index,
)
from agentshore.state import PlayType


def test_v1_action_order_length():
    assert len(V1_ACTION_ORDER) == NUM_ACTIONS


def test_num_actions_is_22():
    assert NUM_ACTIONS == 22


def test_v1_action_order_no_duplicates():
    assert len(set(V1_ACTION_ORDER)) == len(V1_ACTION_ORDER)


def test_v1_action_order_contains_all_play_types():
    assert set(V1_ACTION_ORDER) == set(PlayType)


def test_v1_action_order_matches_enum_declaration_order():
    assert tuple(PlayType) == V1_ACTION_ORDER


def test_play_to_index_roundtrip():
    for i, pt in enumerate(V1_ACTION_ORDER):
        assert PLAY_TO_INDEX[pt] == i


def test_index_to_play_roundtrip():
    for i, pt in enumerate(V1_ACTION_ORDER):
        assert INDEX_TO_PLAY[i] == pt


def test_play_to_index_and_index_to_play_are_inverses():
    for pt in PlayType:
        assert INDEX_TO_PLAY[PLAY_TO_INDEX[pt]] == pt


def test_action_space_version_is_13():
    assert ACTION_SPACE_VERSION == 13


def test_idle_tick_and_recover_are_not_in_action_order():
    """desktop-rni0: IDLE_TICK and RECOVER demoted out of the action head.

    Slot 11 was originally a reserved (FUTURE_5) headroom slot after that
    demotion, and was filled in place by RECONCILE_STATE per AgentShore #593.
    """
    play_values = {pt.value for pt in V1_ACTION_ORDER}
    assert "idle_tick" not in play_values
    assert "recover" not in play_values
    assert "reconcile_state" in play_values
    assert "prune" in play_values
    assert not hasattr(PlayType, "IDLE_TICK")
    assert not hasattr(PlayType, "RECOVER")
    assert not hasattr(PlayType, "FUTURE_5")


def test_reserved_future_slots_keep_stable_indices():
    """Slot 11 hosts RECONCILE_STATE, slot 19 hosts PRUNE; FUTURE_4@14, FUTURE_7/8 reserved."""
    assert PLAY_TO_INDEX[PlayType.RECONCILE_STATE] == 11
    assert PLAY_TO_INDEX[PlayType.PRUNE] == 19
    assert PLAY_TO_INDEX[PlayType.FUTURE_4] == 14
    assert PLAY_TO_INDEX[PlayType.FUTURE_7] == 20
    assert PLAY_TO_INDEX[PlayType.FUTURE_8] == 21


def test_instantiate_agent_is_index_0():
    assert PLAY_TO_INDEX[PlayType.INSTANTIATE_AGENT] == 0


def test_filled_slots_keep_stable_indices():
    assert PLAY_TO_INDEX[PlayType.UNBLOCK_PR] == 1
    assert PLAY_TO_INDEX[PlayType.WRITE_IMPLEMENTATION_PLAN] == 2
    assert PLAY_TO_INDEX[PlayType.SYSTEMATIC_DEBUGGING] == 8
    assert PLAY_TO_INDEX[PlayType.DESIGN_AUDIT] == 9
    assert PLAY_TO_INDEX[PlayType.CLEANUP] == 13


def test_take_break_index():
    assert PLAY_TO_INDEX[PlayType.TAKE_BREAK] == 15


def test_calibrate_alignment_index():
    assert PLAY_TO_INDEX[PlayType.CALIBRATE_ALIGNMENT] == 18


def test_prune_is_index_19():
    assert PLAY_TO_INDEX[PlayType.PRUNE] == 19
    assert PLAY_TO_INDEX[PlayType.FUTURE_8] == 21


# ---------------------------------------------------------------------------
# Config-head index (POLICY_VERSION-gated)
# ---------------------------------------------------------------------------


def test_policy_version_default_is_4():
    assert POLICY_VERSION == 5


def test_max_config_index_size_default():
    assert MAX_CONFIG_INDEX_SIZE == 32


def _agent_cfg(*, enabled: bool = True, tiers: tuple[str, ...] = ()) -> AgentConfig:
    model_tiers = (
        {tier: ModelTierConfig(model="m", enabled=True) for tier in tiers} if tiers else {}
    )
    return AgentConfig(enabled=enabled, model_tiers=model_tiers)


def test_build_config_index_uses_config_order_and_tier_order():
    cfg = RuntimeConfig(
        agents={
            "codex": _agent_cfg(tiers=("medium",)),
            "claude_code": _agent_cfg(tiers=("small", "medium")),
            "gemini": _agent_cfg(tiers=("medium", "large")),
            "grok": _agent_cfg(tiers=("small", "medium", "large")),
        }
    )
    index = build_config_index(cfg)

    # Agent type follows config order; tier order follows MODEL_TIER_PRIORITY.
    assert index == (
        ("codex", "medium"),
        ("claude_code", "medium"),
        ("claude_code", "small"),
        ("gemini", "medium"),
        ("gemini", "large"),
        ("grok", "medium"),
        ("grok", "small"),
        ("grok", "large"),
    )


def test_build_config_index_skips_disabled_agents():
    cfg = RuntimeConfig(
        agents={
            "claude_code": _agent_cfg(enabled=False, tiers=("medium",)),
            "codex": _agent_cfg(tiers=("medium",)),
        }
    )
    index = build_config_index(cfg)
    assert index == (("codex", "medium"),)


def test_build_config_index_empty_when_no_agents():
    cfg = RuntimeConfig(agents={})
    assert build_config_index(cfg) == ()


def test_build_config_index_raises_when_over_cap(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "agentshore.rl.action_space.MAX_CONFIG_INDEX_SIZE",
        1,
    )
    cfg = RuntimeConfig(
        agents={
            "claude_code": _agent_cfg(tiers=("small", "medium")),
        }
    )
    with pytest.raises(ValueError, match="MAX_CONFIG_INDEX_SIZE"):
        build_config_index(cfg)
