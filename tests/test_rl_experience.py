"""Tests for rl/experience.py — RolloutBuffer, GAE, minibatches."""

from __future__ import annotations

import numpy as np
import pytest

from agentshore.rl.action_space import NUM_ACTIONS
from agentshore.rl.experience import RolloutBuffer, Step
from agentshore.rl.observation import OBSERVATION_DIM


def _step(
    reward: float = 0.0,
    done: bool = False,
    value: float = 0.0,
    log_prob: float = -1.0,
) -> Step:
    return Step(
        state=np.zeros(OBSERVATION_DIM, dtype=np.float32),
        action=0,
        reward=reward,
        next_state=np.zeros(OBSERVATION_DIM, dtype=np.float32),
        done=done,
        log_prob=log_prob,
        value=value,
        mask=np.ones(NUM_ACTIONS, dtype=bool),
    )


# ---------------------------------------------------------------------------
# Buffer basics
# ---------------------------------------------------------------------------


def test_buffer_len():
    buf = RolloutBuffer()
    for _ in range(5):
        buf.add(_step())
    assert len(buf) == 5


def test_buffer_clear():
    buf = RolloutBuffer()
    buf.add(_step())
    buf.compute_advantages(0.0)
    buf.clear()
    assert len(buf) == 0
    assert buf.advantages == []
    assert buf.returns == []


# ---------------------------------------------------------------------------
# GAE recursion — hand-computed example
# ---------------------------------------------------------------------------


def test_gae_single_step_not_done():
    """A single non-terminal step.

    r=1, V(s)=0, V(s')=0.5 (bootstrap), gamma=1, lambda=1
    delta = r + gamma*V(s') - V(s) = 1 + 0.5 - 0 = 1.5
    A = delta (only one step) = 1.5
    R = A + V(s) = 1.5
    """
    buf = RolloutBuffer()
    buf.add(_step(reward=1.0, value=0.0))
    buf.compute_advantages(last_value=0.5, gamma=1.0, gae_lambda=1.0)
    assert buf.advantages[0] == pytest.approx(1.5, abs=1e-5)
    assert buf.returns[0] == pytest.approx(1.5, abs=1e-5)


def test_gae_done_step_zeroes_bootstrap():
    """Terminal step: next_value is zeroed regardless of last_value."""
    buf = RolloutBuffer()
    buf.add(_step(reward=1.0, value=0.0, done=True))
    buf.compute_advantages(last_value=100.0, gamma=1.0, gae_lambda=1.0)
    # delta = 1 + 0 (zeroed by done) - 0 = 1.0
    assert buf.advantages[0] == pytest.approx(1.0, abs=1e-5)


def test_gae_two_steps():
    """Two non-terminal steps — check recursion.

    Steps (t=0, t=1), last_value=0.
    t=1: delta_1 = r_1 + gamma*0 - V_1 = 2 - 0 = 2; A_1 = 2
    t=0: delta_0 = 1 + 0.9*0 - 0 = 1; A_0 = 1 + gamma*lam*A_1 = 1 + 0.9*1*2 = 2.8
    Returns: R_1 = A_1 + V_1 = 2, R_0 = A_0 + V_0 = 2.8
    """
    buf = RolloutBuffer()
    buf.add(_step(reward=1.0, value=0.0))  # t=0
    buf.add(_step(reward=2.0, value=0.0))  # t=1
    buf.compute_advantages(last_value=0.0, gamma=0.9, gae_lambda=1.0)
    assert buf.advantages[1] == pytest.approx(2.0, abs=1e-5)
    assert buf.advantages[0] == pytest.approx(1 + 0.9 * 2.0, abs=1e-5)
    assert buf.returns[1] == pytest.approx(2.0, abs=1e-5)
    assert buf.returns[0] == pytest.approx(1 + 0.9 * 2.0, abs=1e-5)


def test_gae_bootstrap_used_when_not_done():
    """last_value > 0 should contribute when last step is not done."""
    buf = RolloutBuffer()
    buf.add(_step(reward=0.0, value=0.0, done=False))
    # With large last_value the advantage should be positive
    buf.compute_advantages(last_value=5.0, gamma=1.0, gae_lambda=1.0)
    assert buf.advantages[0] > 0.0


# ---------------------------------------------------------------------------
# Minibatches
# ---------------------------------------------------------------------------


def test_minibatch_total_count():
    buf = RolloutBuffer()
    n = 8
    for _ in range(n):
        buf.add(_step())
    buf.compute_advantages(0.0)

    total = sum(len(b.states) for b in buf.iter_minibatches(batch_size=3))
    assert total == n


def test_minibatch_shapes():
    buf = RolloutBuffer()
    obs_dim = OBSERVATION_DIM
    for _ in range(4):
        buf.add(_step())
    buf.compute_advantages(0.0)

    batches = list(buf.iter_minibatches(batch_size=4, shuffle=False))
    assert len(batches) == 1
    b = batches[0]
    assert b.states.shape == (4, obs_dim)
    assert b.actions.shape == (4,)
    assert b.old_log_probs.shape == (4,)
    assert b.advantages.shape == (4,)
    assert b.returns.shape == (4,)
    assert b.masks.shape == (4, NUM_ACTIONS)


def test_minibatch_no_advantages_yields_nothing():
    buf = RolloutBuffer()
    buf.add(_step())
    # compute_advantages NOT called
    batches = list(buf.iter_minibatches(batch_size=1))
    assert batches == []


def test_minibatch_shuffled_preserves_count():
    buf = RolloutBuffer()
    for _ in range(7):
        buf.add(_step())
    buf.compute_advantages(0.0)

    # Run several times to exercise shuffle paths
    for _ in range(3):
        total = sum(len(b.states) for b in buf.iter_minibatches(batch_size=3, shuffle=True))
        assert total == 7


# ---------------------------------------------------------------------------
# Config-head fields on Step / Batch
# ---------------------------------------------------------------------------


def _config_step(*, config_action: int | None, num_configs: int = 4) -> Step:
    s = _step()
    if config_action is not None:
        s.config_action = config_action
        s.config_log_prob = -0.5
        s.config_mask = np.ones(num_configs, dtype=bool)
    return s


def test_step_default_config_fields_are_none():
    s = _step()
    assert s.config_action is None
    assert s.config_log_prob is None
    assert s.config_mask is None


def test_minibatch_no_config_actions_yields_none_tensors():
    buf = RolloutBuffer()
    for _ in range(4):
        buf.add(_step())
    buf.compute_advantages(0.0)
    for batch in buf.iter_minibatches(batch_size=4, shuffle=False):
        assert batch.config_actions is None
        assert batch.config_old_log_probs is None
        assert batch.config_masks is None
        assert batch.config_active is None


def test_minibatch_mixed_config_active_populates_tensors():
    buf = RolloutBuffer()
    buf.add(_config_step(config_action=2, num_configs=4))
    buf.add(_step())  # inactive row
    buf.add(_config_step(config_action=0, num_configs=4))
    buf.add(_step())
    buf.compute_advantages(0.0)
    batches = list(buf.iter_minibatches(batch_size=4, shuffle=False))
    assert len(batches) == 1
    batch = batches[0]

    assert batch.config_actions is not None
    assert batch.config_active is not None
    assert batch.config_masks is not None
    assert batch.config_old_log_probs is not None
    # Two active rows; two inactive.
    assert int(batch.config_active.sum().item()) == 2
    # Inactive rows get an all-True mask (so log_softmax stays finite),
    # but the loss is masked off via config_active.
    assert batch.config_masks.shape == (4, 4)
    assert batch.config_actions.shape == (4,)


def test_minibatch_raises_when_action_set_but_mask_missing():
    buf = RolloutBuffer()
    s = _step()
    s.config_action = 1
    s.config_log_prob = -0.3
    # Intentionally do not set config_mask.
    buf.add(s)
    buf.compute_advantages(0.0)
    with pytest.raises(ValueError, match="config_mask"):
        list(buf.iter_minibatches(batch_size=1, shuffle=False))
