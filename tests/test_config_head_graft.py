"""Config-head graft for head-stripped / other-roster checkpoints (review L5)."""

from __future__ import annotations

import asyncio
import importlib.resources
import unittest.mock as mock
from pathlib import Path

import torch

from agentshore.rl.checkpoint_store import (
    canonical_lock_filename,
    canonical_weights_filename,
    graft_config_head,
    write_global_canonical_blocking,
)
from agentshore.rl.cold_start import apply_cold_start_config_bias
from agentshore.rl.policy import ActorCritic
from agentshore.rl.selector_lifecycle import reload_shared_weights

_INDEX = (("claude", "large"), ("codex", "medium"), ("grok", "small"))


def _seed_path() -> Path:
    return Path(str(importlib.resources.files("agentshore.data") / "bootstrap_policy.pt"))


def _seed() -> ActorCritic:
    return ActorCritic.load(_seed_path())


def test_graft_keeps_play_and_value_outputs_and_sizes_head() -> None:
    seed = _seed()
    assert seed.num_configs == 0
    obs = torch.randn(16, seed.trunk[0].in_features)
    with torch.no_grad():
        logits_before, value_before = seed(obs)

    grafted = graft_config_head(seed, _INDEX)

    assert grafted.num_configs == len(_INDEX)
    assert grafted.config_head.out_features == len(_INDEX)
    with torch.no_grad():
        logits_after, value_after = grafted(obs)
    assert torch.equal(logits_before, logits_after)
    assert torch.equal(value_before, value_after)

    expected = ActorCritic(num_configs=len(_INDEX))
    apply_cold_start_config_bias(expected, _INDEX)
    assert torch.equal(grafted.config_head.bias, expected.config_head.bias)
    assert torch.count_nonzero(grafted.config_head.weight) == 0


def test_graft_is_noop_when_widths_match_or_no_roster() -> None:
    policy = ActorCritic(num_configs=len(_INDEX))
    assert graft_config_head(policy, _INDEX) is policy
    seed = _seed()
    assert graft_config_head(seed, ()) is seed


def test_reload_from_headless_canonical_takes_trunk_keeps_head(tmp_path: Path) -> None:
    seed = _seed()
    seed.save(tmp_path / canonical_weights_filename())
    live = graft_config_head(ActorCritic(), _INDEX)  # random trunk, grafted head
    head_before = live.config_head.bias.clone()

    outcome = reload_shared_weights(live, tmp_path)

    assert not outcome.rejected
    assert outcome.reload_base is not None
    assert torch.equal(live.trunk[0].weight, seed.trunk[0].weight)
    assert torch.equal(live.actor.weight, seed.actor.weight)
    assert torch.equal(live.config_head.bias, head_before)

    # The shared canonical stays head-stripped: the config head is roster-specific.
    canonical = tmp_path / canonical_weights_filename()
    write_global_canonical_blocking(
        live, outcome.reload_base, canonical, tmp_path / canonical_lock_filename()
    )
    assert ActorCritic.load(canonical).num_configs == 0


def test_selector_load_from_seed_grafts_head() -> None:
    from agentshore.config import PPOConfig
    from agentshore.rl.selector import PPOSelector

    cfg = mock.MagicMock()
    cfg.learning_rate = 3e-4
    cfg.entropy_coef = 0.01
    cfg.ppo = PPOConfig()
    sel = asyncio.run(
        PPOSelector.load(
            weights_path=_seed_path(),
            resolver=mock.MagicMock(),
            registry=mock.MagicMock(),
            metrics=mock.MagicMock(),
            cfg=cfg,
            config_index=_INDEX,
        )
    )
    assert sel._policy.num_configs == len(_INDEX)


def test_projects_with_different_rosters_keep_sharing(tmp_path: Path) -> None:
    """Project A's save must not lock project B (other roster width) out of the canonical."""
    canonical = tmp_path / canonical_weights_filename()
    lock = tmp_path / canonical_lock_filename()
    _seed().save(canonical)

    a = graft_config_head(_seed(), _INDEX)
    a_out = reload_shared_weights(a, tmp_path)
    with torch.no_grad():
        a.actor.bias += 1.0
    write_global_canonical_blocking(a, a_out.reload_base, canonical, lock)

    b = graft_config_head(_seed(), _INDEX[:2])
    b_head = b.config_head.weight.clone()
    b_out = reload_shared_weights(b, tmp_path)
    assert not b_out.rejected
    assert torch.equal(b.actor.bias, ActorCritic.load(canonical).actor.bias)
    assert torch.equal(b.config_head.weight, b_head)
    with torch.no_grad():
        b.actor.bias += 1.0
    write_global_canonical_blocking(b, b_out.reload_base, canonical, lock)

    # Both deltas landed (A's +1 then B's +1) and the canonical stayed head-stripped.
    merged = ActorCritic.load(canonical)
    assert merged.num_configs == 0
    assert torch.allclose(merged.actor.bias, _seed().actor.bias + 2.0)
