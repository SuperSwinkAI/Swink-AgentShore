"""Cold-start bias for the actor head.

Applies log-renormalized DEFAULT_PLAY_WEIGHTS as the actor bias so that argmax
on an all-zero observation + all-true mask selects ISSUE_PICKUP.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import numpy as np
import torch

from agentshore.rl.action_space import V1_ACTION_ORDER
from agentshore.state import PlayType

if TYPE_CHECKING:
    from agentshore.rl.policy import ActorCritic


# Default weights — renormalized after removing the 4 legacy plays
# (PROJECT_ALIGNMENT_CHECK 0.14, ALIGN_TO_NEW_GOALS 0.0046,
#  CONSOLIDATE_LEARNINGS 0.0194, INTAKE_AND_PLAN 0.0300).
# Remaining raw sum was 0.8061; each entry divided by 0.8061 → sum ≈ 1.0.
DEFAULT_PLAY_WEIGHTS: Final[dict[PlayType, float]] = {
    PlayType.ISSUE_PICKUP: 0.2233,
    PlayType.CODE_REVIEW: 0.1365,
    PlayType.RUN_QA: 0.0992,
    PlayType.MERGE_PR: 0.0992,
    PlayType.REFINE_TASK_BREAKDOWN: 0.0496,
    PlayType.INSTANTIATE_AGENT: 0.0285,
    PlayType.UNBLOCK_PR: 0.0682,
    PlayType.WRITE_IMPLEMENTATION_PLAN: 0.0744,
    PlayType.SYSTEMATIC_DEBUGGING: 0.0434,
    # DESIGN_AUDIT occupies the old slot-9 reserved prior to preserve learned
    # action-space compatibility.
    PlayType.DESIGN_AUDIT: 0.0114,
    PlayType.END_AGENT: 0.0114,
    PlayType.END_SESSION: 0.0057,
    PlayType.CLEANUP: 0.0120,
    PlayType.BROWSER_VERIFICATION: 0.0120,
    PlayType.TAKE_BREAK: 0.0057,
    # Beads-native plays — weights will be tuned by PPO once in production.
    PlayType.GROOM_BACKLOG: 0.0248,
    PlayType.SEED_PROJECT: 0.0186,
    PlayType.CALIBRATE_ALIGNMENT: 0.0186,
    # RECONCILE_STATE — event-driven self-heal play, masked except during
    # wedge conditions. Cold-start weight matches the other low-frequency
    # event-driven plays; PPO learns the real selection prior once the play
    # has fired a few times.
    PlayType.RECONCILE_STATE: 0.0114,
    # PRUNE — infrastructure-debt sweep. Threshold-gated, so the cold-start
    # weight only matters as a numerical anchor; PPO learns the real prior.
    PlayType.PRUNE: 0.0114,
    # Reserved future slots get the same low cold-start weight as the other
    # placeholders so the sum stays within ~1.0. They never get selected (mask
    # gates them off) — the weight only matters as a numerical anchor for the
    # log-renormalization, not as a selection prior.
    PlayType.FUTURE_7: 0.0114,
    PlayType.FUTURE_8: 0.0114,
}


def apply_cold_start_bias(policy: ActorCritic) -> None:
    """Zero the actor weight matrix and set actor bias from log-renormalized DEFAULT_PLAY_WEIGHTS.

    After this call, argmax(policy.actor(zero_trunk), all_true_mask) == PLAY_TO_INDEX[ISSUE_PICKUP].
    Gradients will de-zero the weight matrix over training.
    """
    weights = np.array([DEFAULT_PLAY_WEIGHTS[pt] for pt in V1_ACTION_ORDER], dtype=np.float64)
    weights = weights / weights.sum()
    log_w = np.log(weights)
    bias = log_w - log_w.mean()

    with torch.no_grad():
        policy.actor.weight.zero_()
        policy.actor.bias.copy_(torch.tensor(bias, dtype=torch.float32))


# ---------------------------------------------------------------------------
# Config-head cold-start
# ---------------------------------------------------------------------------

# Tier-only priors for the config head. Agent provider should not affect spawn
# eligibility or cold-start preference; provider availability is handled by the
# config mask, while the prior only expresses expected work shape.
DEFAULT_CONFIG_TIER_WEIGHTS: Final[dict[str, float]] = {
    "medium": 0.50,
    "large": 0.35,
    "small": 0.15,
}


def apply_cold_start_config_bias(
    policy: ActorCritic,
    config_index: tuple[tuple[str, str], ...],
) -> None:
    """Zero the config-head weight matrix and seed bias from DEFAULT_CONFIG_WEIGHTS.

    No-op when ``num_configs == 0`` or when ``config_index`` is empty. Unknown
    (agent_type, model_tier) keys default to a small uniform residual so the
    head can still learn to use them via gradients.
    """
    if policy.num_configs == 0 or not config_index:
        return

    if len(config_index) != policy.num_configs:
        msg = (
            f"config_index size {len(config_index)} does not match policy "
            f"num_configs {policy.num_configs}"
        )
        raise ValueError(msg)

    fallback = 0.001
    raw = np.array(
        [DEFAULT_CONFIG_TIER_WEIGHTS.get(tier, fallback) for _, tier in config_index],
        dtype=np.float64,
    )
    if raw.sum() <= 0.0:
        raw = np.ones_like(raw)
    weights = raw / raw.sum()
    log_w = np.log(weights)
    bias = log_w - log_w.mean()

    # The config head's actual output dim may be max(num_configs, 1) to keep
    # the state-dict stable when num_configs == 0; we only touch the live
    # slots and leave any padding at zero.
    head_dim = policy.config_head.out_features
    bias_full = np.zeros(head_dim, dtype=np.float32)
    bias_full[: len(config_index)] = bias.astype(np.float32)

    with torch.no_grad():
        policy.config_head.weight.zero_()
        policy.config_head.bias.copy_(torch.tensor(bias_full, dtype=torch.float32))
