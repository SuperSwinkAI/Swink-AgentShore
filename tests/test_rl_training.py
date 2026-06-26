"""Tests for rl/training.py — PPOUpdater loss reduction, NaN rollback."""

from __future__ import annotations

import numpy as np
import torch

from agentshore.rl.action_space import NUM_ACTIONS
from agentshore.rl.experience import RolloutBuffer, Step
from agentshore.rl.observation import OBSERVATION_DIM
from agentshore.rl.policy import ActorCritic
from agentshore.rl.training import PPOUpdater


def _step(reward: float = 1.0) -> Step:
    return Step(
        state=np.random.rand(OBSERVATION_DIM).astype(np.float32),
        action=0,
        reward=reward,
        next_state=np.random.rand(OBSERVATION_DIM).astype(np.float32),
        done=False,
        log_prob=-2.0,
        value=0.0,
        mask=np.ones(NUM_ACTIONS, dtype=bool),
    )


def _filled_buffer(n: int = 8, reward: float = 1.0) -> RolloutBuffer:
    buf = RolloutBuffer()
    for _ in range(n):
        buf.add(_step(reward=reward))
    buf.compute_advantages(0.0)
    return buf


# ---------------------------------------------------------------------------
# Basic update
# ---------------------------------------------------------------------------


def test_update_returns_stats():
    policy = ActorCritic()
    updater = PPOUpdater(policy, ppo_epochs=1, mini_batch_size=4)
    buf = _filled_buffer(8)
    stats = updater.update(buf)
    assert stats.n_epochs == 1
    assert not stats.rolled_back


def test_update_finite_stats():
    policy = ActorCritic()
    updater = PPOUpdater(policy, ppo_epochs=2, mini_batch_size=4)
    buf = _filled_buffer(8)
    stats = updater.update(buf)
    import math

    assert math.isfinite(stats.policy_loss)
    assert math.isfinite(stats.value_loss)
    assert math.isfinite(stats.entropy)


def test_update_empty_buffer_returns_fast():
    policy = ActorCritic()
    updater = PPOUpdater(policy)
    buf = RolloutBuffer()
    stats = updater.update(buf)
    assert stats.n_epochs == 0
    assert not stats.rolled_back


# ---------------------------------------------------------------------------
# NaN rollback
# ---------------------------------------------------------------------------


def test_nan_reward_triggers_rollback():
    policy = ActorCritic()
    snapshot = {k: v.clone() for k, v in policy.state_dict().items()}
    updater = PPOUpdater(policy, ppo_epochs=1, mini_batch_size=2)

    buf = RolloutBuffer()
    for _ in range(4):
        buf.add(
            Step(
                state=np.zeros(OBSERVATION_DIM, dtype=np.float32),
                action=0,
                reward=float("nan"),  # NaN reward → NaN returns → NaN loss
                next_state=np.zeros(OBSERVATION_DIM, dtype=np.float32),
                done=False,
                log_prob=-1.0,
                value=0.0,
                mask=np.ones(NUM_ACTIONS, dtype=bool),
            )
        )
    buf.compute_advantages(0.0)

    stats = updater.update(buf)
    assert stats.rolled_back

    # Weights should be restored
    for k in snapshot:
        torch.testing.assert_close(policy.state_dict()[k], snapshot[k])


# ---------------------------------------------------------------------------
# Gradient clipping enforced
# ---------------------------------------------------------------------------


def test_grad_clip_does_not_explode():
    """After update, weight norms should be finite and reasonable."""
    policy = ActorCritic()
    updater = PPOUpdater(policy, ppo_epochs=2, mini_batch_size=4, max_grad_norm=0.5)
    buf = _filled_buffer(8)
    updater.update(buf)
    for p in policy.parameters():
        assert p.data.isfinite().all()


# ---------------------------------------------------------------------------
# Config head loss path
# ---------------------------------------------------------------------------


def _config_step(*, num_configs: int, config_action: int) -> Step:
    s = _step()
    s.config_action = config_action
    s.config_log_prob = -1.0
    s.config_mask = np.ones(num_configs, dtype=bool)
    return s


def test_update_skips_config_loss_when_no_config_actions():
    """No config_action steps → n_config_updates stays 0, weights still update."""
    policy = ActorCritic(num_configs=4)
    updater = PPOUpdater(policy, ppo_epochs=1, mini_batch_size=4)
    buf = _filled_buffer(8)
    stats = updater.update(buf)
    assert stats.n_config_updates == 0
    assert stats.config_policy_loss == 0.0


def test_update_runs_config_loss_when_steps_have_config_actions():
    policy = ActorCritic(num_configs=4)
    updater = PPOUpdater(policy, ppo_epochs=1, mini_batch_size=4)
    buf = RolloutBuffer()
    # Mix of config-active and inactive steps
    for i in range(4):
        buf.add(_config_step(num_configs=4, config_action=i % 4))
    for _ in range(4):
        buf.add(_step())
    buf.compute_advantages(0.0)

    before = policy.config_head.bias.detach().clone()
    stats = updater.update(buf)
    after = policy.config_head.bias.detach().clone()

    assert stats.n_config_updates >= 1
    # The config-head bias should have moved (gradient flowed through it).
    assert not torch.equal(before, after)


def test_update_without_config_head_skips_config_path():
    """num_configs=0 means we never touch the config head, even if rows had it."""
    policy = ActorCritic(num_configs=0)
    updater = PPOUpdater(policy, ppo_epochs=1, mini_batch_size=4)
    buf = _filled_buffer(8)
    stats = updater.update(buf)
    assert stats.n_config_updates == 0


def test_state_dict_round_trip_restores_optimizer_state():
    policy = ActorCritic()
    updater = PPOUpdater(policy)
    state = updater.state_dict()
    updater.load_state_dict(state)
    loaded_state = updater.state_dict()
    assert loaded_state.keys() == state.keys()


def test_load_state_dict_requires_optimizer_key():
    policy = ActorCritic()
    updater = PPOUpdater(policy)
    try:
        updater.load_state_dict({})
    except KeyError as exc:
        assert exc.args == ("optimizer",)
    else:
        raise AssertionError("expected KeyError for missing optimizer state")
