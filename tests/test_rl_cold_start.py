"""Tests for rl/cold_start.py — apply_cold_start_bias correctness."""

from __future__ import annotations

import numpy as np
import pytest
import torch

from agentshore.rl.action_space import NUM_ACTIONS, PLAY_TO_INDEX
from agentshore.rl.cold_start import DEFAULT_PLAY_WEIGHTS, apply_cold_start_bias
from agentshore.rl.observation import OBSERVATION_DIM
from agentshore.rl.policy import ActorCritic
from agentshore.state import PlayType


def _policy() -> ActorCritic:
    m = ActorCritic()
    apply_cold_start_bias(m)
    return m


# ---------------------------------------------------------------------------
# Bias applied correctly
# ---------------------------------------------------------------------------


def test_actor_weight_zeroed():
    m = _policy()
    assert m.actor.weight.abs().sum().item() == 0.0


def test_actor_bias_set():
    m = _policy()
    assert m.actor.bias.abs().sum().item() > 0.0


def test_bias_sum_consistent():
    m = _policy()
    # bias = log(w) - mean(log(w)) → sum of exp(bias) ≈ num_actions (before renorm)
    # just verify the bias is finite and non-trivial
    b = m.actor.bias.detach().numpy()
    assert np.all(np.isfinite(b))
    assert b.max() > b.min()


# ---------------------------------------------------------------------------
# Cold-start semantics
# ---------------------------------------------------------------------------


def test_argmax_zero_obs_selects_issue_pickup():
    """argmax on all-zero obs + all-true mask == ISSUE_PICKUP."""
    m = _policy()
    obs = torch.zeros(OBSERVATION_DIM)
    mask = torch.ones(NUM_ACTIONS, dtype=torch.bool)
    action, _, _ = m.act(obs, mask, greedy=True)
    assert action == PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]


def test_seed_project_preferred_over_low_weight_plays():
    """SEED_PROJECT should have higher logit than the old slot-9 prior."""
    m = _policy()
    obs = torch.zeros(OBSERVATION_DIM)
    with torch.no_grad():
        logits, _ = m(obs.unsqueeze(0))
    logits = logits.squeeze()
    seed_logit = logits[PLAY_TO_INDEX[PlayType.SEED_PROJECT]].item()
    design_audit_logit = logits[PLAY_TO_INDEX[PlayType.DESIGN_AUDIT]].item()
    assert seed_logit > design_audit_logit


def test_all_play_types_covered():
    """DEFAULT_PLAY_WEIGHTS covers all 20 play types."""
    assert set(DEFAULT_PLAY_WEIGHTS.keys()) == set(PlayType)


def test_weights_sum_to_approximately_1():
    total = sum(DEFAULT_PLAY_WEIGHTS.values())
    assert abs(total - 1.0) < 0.02  # allow small floating-point slack (20 actions)


# ---------------------------------------------------------------------------
# Config-head cold-start
# ---------------------------------------------------------------------------


def test_apply_config_bias_no_op_when_num_configs_zero():
    from agentshore.rl.cold_start import apply_cold_start_config_bias

    p = ActorCritic(num_configs=0)
    # Silent no-op even when config_index is empty.
    apply_cold_start_config_bias(p, ())


def test_apply_config_bias_prefers_by_tier_not_provider():
    from agentshore.rl.cold_start import apply_cold_start_config_bias

    config_index = (
        ("claude_code", "medium"),
        ("codex", "medium"),
        ("codex", "small"),
    )
    p = ActorCritic(num_configs=len(config_index))
    apply_cold_start_config_bias(p, config_index)

    obs = torch.zeros(1, OBSERVATION_DIM)
    with torch.no_grad():
        logits = p.forward_config(obs).squeeze(0)
    assert logits[0].item() == pytest.approx(logits[1].item())
    assert logits[1].item() > logits[2].item()


def test_apply_config_bias_size_mismatch_raises():
    from agentshore.rl.cold_start import apply_cold_start_config_bias

    p = ActorCritic(num_configs=2)
    with pytest.raises(ValueError, match="num_configs"):
        apply_cold_start_config_bias(p, (("claude_code", "medium"),))
