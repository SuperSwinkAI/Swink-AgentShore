"""Tests for rl/policy.py — ActorCritic architecture, masking, save/load."""

from __future__ import annotations

from pathlib import Path

import pytest
import torch

from agentshore.rl.action_space import (
    ACTION_SPACE_VERSION,
    NUM_ACTIONS,
)
from agentshore.rl.config_head import POLICY_VERSION
from agentshore.rl.observation import OBSERVATION_DIM, OBSERVATION_VERSION
from agentshore.rl.policy import ActorCritic, IncompatibleCheckpointError


def _obs() -> torch.Tensor:
    return torch.zeros(OBSERVATION_DIM)


def _mask_all_true() -> torch.Tensor:
    return torch.ones(NUM_ACTIONS, dtype=torch.bool)


def _mask_single(idx: int) -> torch.Tensor:
    m = torch.zeros(NUM_ACTIONS, dtype=torch.bool)
    m[idx] = True
    return m


# ---------------------------------------------------------------------------
# Architecture
# ---------------------------------------------------------------------------


def test_param_count_under_100k():
    m = ActorCritic()
    total = sum(p.numel() for p in m.parameters())
    assert total < 100_000


def test_forward_output_shapes():
    m = ActorCritic()
    obs = torch.zeros(1, OBSERVATION_DIM)
    logits, value = m(obs)
    assert logits.shape == (1, NUM_ACTIONS)
    assert value.shape == (1, 1)


def test_forward_batch_shape():
    m = ActorCritic()
    obs = torch.zeros(8, OBSERVATION_DIM)
    logits, value = m(obs)
    assert logits.shape == (8, NUM_ACTIONS)
    assert value.shape == (8, 1)


# ---------------------------------------------------------------------------
# act() — masking and sampling
# ---------------------------------------------------------------------------


def test_act_returns_valid_action():
    m = ActorCritic()
    action, log_prob, val = m.act(_obs(), _mask_all_true())
    assert 0 <= action < NUM_ACTIONS
    assert isinstance(log_prob, float)
    assert isinstance(val, float)


def test_act_obeys_mask_single_action():
    m = ActorCritic()
    for allowed_idx in [0, 5, 18]:
        action, _, _ = m.act(_obs(), _mask_single(allowed_idx))
        assert action == allowed_idx


def test_act_greedy_reproducible():
    m = ActorCritic()
    obs = torch.rand(OBSERVATION_DIM)
    a1, lp1, v1 = m.act(obs, _mask_all_true(), greedy=True)
    a2, lp2, v2 = m.act(obs, _mask_all_true(), greedy=True)
    assert a1 == a2
    assert lp1 == pytest.approx(lp2, abs=1e-6)
    assert v1 == pytest.approx(v2, abs=1e-6)


def test_act_all_masked_raises():
    m = ActorCritic()
    empty_mask = torch.zeros(NUM_ACTIONS, dtype=torch.bool)
    with pytest.raises(RuntimeError):
        m.act(_obs(), empty_mask)


# ---------------------------------------------------------------------------
# evaluate() — used by PPOUpdater
# ---------------------------------------------------------------------------


def test_evaluate_shapes():
    m = ActorCritic()
    batch = 4
    obs = torch.zeros(batch, OBSERVATION_DIM)
    actions = torch.zeros(batch, dtype=torch.long)
    mask = torch.ones(batch, NUM_ACTIONS, dtype=torch.bool)
    log_probs, values, entropy = m.evaluate(obs, actions, mask)
    assert log_probs.shape == (batch,)
    assert values.shape == (batch,)
    assert entropy.shape == (batch,)


def test_evaluate_entropy_non_negative():
    m = ActorCritic()
    batch = 4
    obs = torch.zeros(batch, OBSERVATION_DIM)
    actions = torch.zeros(batch, dtype=torch.long)
    mask = torch.ones(batch, NUM_ACTIONS, dtype=torch.bool)
    _, _, entropy = m.evaluate(obs, actions, mask)
    assert (entropy >= 0).all()


# ---------------------------------------------------------------------------
# value()
# ---------------------------------------------------------------------------


def test_value_returns_float():
    m = ActorCritic()
    v = m.value(_obs())
    assert isinstance(v, float)


# ---------------------------------------------------------------------------
# save / load round-trip
# ---------------------------------------------------------------------------


def test_save_load_roundtrip(tmp_path: Path):
    m = ActorCritic()
    # Randomize weights to check they survive round-trip
    with torch.no_grad():
        for p in m.parameters():
            p.normal_()
    path = tmp_path / "policy.pt"
    m.save(path)
    assert path.exists()

    loaded = ActorCritic.load(path)
    obs = torch.zeros(1, OBSERVATION_DIM)
    with torch.no_grad():
        logits_orig, _ = m(obs)
        logits_loaded, _ = loaded(obs)
    torch.testing.assert_close(logits_orig, logits_loaded)


def test_save_is_atomic(tmp_path: Path):
    m = ActorCritic()
    path = tmp_path / "policy.pt"
    m.save(path)
    # No partial .tmp files should remain
    tmps = list(tmp_path.glob("*.tmp"))
    assert tmps == []


def test_load_mismatched_version_raises(tmp_path: Path):
    m = ActorCritic()
    path = tmp_path / "bad.pt"
    import torch as _torch

    # Write a payload with wrong version
    _torch.save(
        {
            "state_dict": m.state_dict(),
            "action_space_version": 999,
            "policy_version": POLICY_VERSION,
            "observation_version": OBSERVATION_VERSION,
            "obs_dim": OBSERVATION_DIM,
            "num_actions": NUM_ACTIONS,
            "num_configs": 0,
        },
        path,
    )
    with pytest.raises(IncompatibleCheckpointError):
        ActorCritic.load(path)


def test_load_mismatched_policy_version_raises(tmp_path: Path):
    m = ActorCritic()
    path = tmp_path / "bad_policy.pt"
    import torch as _torch

    _torch.save(
        {
            "state_dict": m.state_dict(),
            "action_space_version": ACTION_SPACE_VERSION,
            "policy_version": POLICY_VERSION + 99,
            "observation_version": OBSERVATION_VERSION,
            "obs_dim": OBSERVATION_DIM,
            "num_actions": NUM_ACTIONS,
            "num_configs": 0,
        },
        path,
    )
    with pytest.raises(IncompatibleCheckpointError, match="policy_version"):
        ActorCritic.load(path)


def test_load_mismatched_observation_version_raises(tmp_path: Path):
    m = ActorCritic()
    path = tmp_path / "bad_obs.pt"
    import torch as _torch

    _torch.save(
        {
            "state_dict": m.state_dict(),
            "action_space_version": ACTION_SPACE_VERSION,
            "policy_version": POLICY_VERSION,
            "observation_version": OBSERVATION_VERSION + 99,
            "obs_dim": OBSERVATION_DIM,
            "num_actions": NUM_ACTIONS,
            "num_configs": 0,
        },
        path,
    )
    with pytest.raises(IncompatibleCheckpointError, match="observation_version"):
        ActorCritic.load(path)


# ---------------------------------------------------------------------------
# Config head
# ---------------------------------------------------------------------------


def test_config_head_default_num_configs_is_zero():
    m = ActorCritic()
    assert m.num_configs == 0


def test_act_config_raises_when_no_configs():
    m = ActorCritic(num_configs=0)
    mask = torch.ones(1, dtype=torch.bool)
    with pytest.raises(RuntimeError, match="num_configs == 0"):
        m.act_config(_obs(), mask)


def test_act_config_obeys_mask():
    m = ActorCritic(num_configs=4)
    for allowed_idx in [0, 2, 3]:
        mask = torch.zeros(4, dtype=torch.bool)
        mask[allowed_idx] = True
        idx, _ = m.act_config(_obs(), mask)
        assert idx == allowed_idx


def test_act_config_all_masked_raises():
    m = ActorCritic(num_configs=3)
    mask = torch.zeros(3, dtype=torch.bool)
    with pytest.raises(RuntimeError, match="cannot select"):
        m.act_config(_obs(), mask)


def test_act_config_greedy_reproducible():
    m = ActorCritic(num_configs=5)
    mask = torch.ones(5, dtype=torch.bool)
    obs = torch.rand(OBSERVATION_DIM)
    idx1, lp1 = m.act_config(obs, mask, greedy=True)
    idx2, lp2 = m.act_config(obs, mask, greedy=True)
    assert idx1 == idx2
    assert lp1 == pytest.approx(lp2, abs=1e-6)


def test_evaluate_config_shapes():
    m = ActorCritic(num_configs=4)
    batch = 6
    obs = torch.zeros(batch, OBSERVATION_DIM)
    actions = torch.zeros(batch, dtype=torch.long)
    mask = torch.ones(batch, 4, dtype=torch.bool)
    log_probs, entropy = m.evaluate_config(obs, actions, mask)
    assert log_probs.shape == (batch,)
    assert entropy.shape == (batch,)
    assert (entropy >= 0).all()


def test_save_load_preserves_num_configs(tmp_path: Path):
    m = ActorCritic(num_configs=4)
    path = tmp_path / "policy.pt"
    m.save(path)
    loaded = ActorCritic.load(path)
    assert loaded.num_configs == 4
    # Config-head logits should match across save/load.
    obs = torch.zeros(1, OBSERVATION_DIM)
    with torch.no_grad():
        torch.testing.assert_close(m.forward_config(obs), loaded.forward_config(obs))


def test_save_load_preserves_zero_num_configs(tmp_path: Path):
    m = ActorCritic(num_configs=0)
    path = tmp_path / "policy.pt"
    m.save(path)
    loaded = ActorCritic.load(path)
    assert loaded.num_configs == 0


def test_param_count_with_small_config_head_under_120k():
    m = ActorCritic(num_configs=8)
    total = sum(p.numel() for p in m.parameters())
    assert total < 120_000


def test_forward_repeated_is_finite():
    """Repeated forward passes produce finite outputs."""
    m = ActorCritic()
    obs = torch.zeros(1, OBSERVATION_DIM)
    with torch.no_grad():
        for _ in range(10):
            logits, value = m(obs)
            assert torch.isfinite(logits).all()
            assert torch.isfinite(value).all()
