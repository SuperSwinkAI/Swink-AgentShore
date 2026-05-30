"""PPO trainer with NaN rollback.

PPOUpdater takes a RolloutBuffer (with pre-computed advantages) and runs
ppo_epochs passes of clipped surrogate + value + entropy loss.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
import torch
import torch.nn as nn
import torch.optim as optim

if TYPE_CHECKING:
    from agentshore.rl.experience import RolloutBuffer
    from agentshore.rl.policy import ActorCritic

_logger = structlog.get_logger(__name__)
type OptimizerStateDict = dict[str, Any]


@dataclass(slots=True)
class UpdateStats:
    policy_loss: float = 0.0
    value_loss: float = 0.0
    entropy: float = 0.0
    approx_kl: float = 0.0
    clip_fraction: float = 0.0
    n_epochs: int = 0
    rolled_back: bool = False
    config_policy_loss: float = 0.0
    config_entropy: float = 0.0
    n_config_updates: int = 0


class PPOUpdater:
    """Runs PPO updates on a filled RolloutBuffer.

    NaN guard: snapshot policy state_dict before each full update; restore on
    non-finite loss or non-finite logits on a probe observation.
    """

    def __init__(
        self,
        policy: ActorCritic,
        *,
        lr: float = 3e-4,
        clip_eps: float = 0.2,
        value_coef: float = 0.5,
        entropy_coef: float = 0.01,
        ppo_epochs: int = 4,
        mini_batch_size: int = 4,
        max_grad_norm: float = 0.5,
        config_policy_coef: float = 1.0,
        config_entropy_coef: float = 0.05,
    ) -> None:
        self._policy = policy
        self._clip_eps = clip_eps
        self._value_coef = value_coef
        self._entropy_coef = entropy_coef
        self._ppo_epochs = ppo_epochs
        self._mini_batch_size = mini_batch_size
        self._max_grad_norm = max_grad_norm
        self._config_policy_coef = config_policy_coef
        self._config_entropy_coef = config_entropy_coef
        self._optimizer = optim.Adam(policy.parameters(), lr=lr)

    def update(self, buffer: RolloutBuffer) -> UpdateStats:
        """Run ppo_epochs of PPO updates and return aggregated stats.

        Returns immediately with rolled_back=True if NaN is detected.
        """
        stats = UpdateStats()
        if len(buffer) == 0 or not buffer.advantages:
            return stats

        if len(buffer) < 2:
            return UpdateStats(
                policy_loss=0.0,
                value_loss=0.0,
                entropy=0.0,
                approx_kl=0.0,
                clip_fraction=0.0,
                rolled_back=False,
            )

        # Snapshot before update — used for NaN rollback
        snapshot = {k: v.clone() for k, v in self._policy.state_dict().items()}

        # Normalize advantages across the whole buffer
        adv_all = torch.tensor(buffer.advantages, dtype=torch.float32)
        adv_mean = adv_all.mean()
        adv_std = adv_all.std() + 1e-8
        # Normalisation stored back into buffer.advantages for minibatch access
        normalized_advantages = (adv_all - adv_mean) / adv_std
        buffer.advantages = normalized_advantages.tolist()

        total_pl = 0.0
        total_vl = 0.0
        total_ent = 0.0
        total_kl = 0.0
        total_cf = 0.0
        total_cpl = 0.0
        total_cent = 0.0
        n_batches = 0
        n_config_batches = 0

        for _ in range(self._ppo_epochs):
            for batch in buffer.iter_minibatches(self._mini_batch_size):
                log_probs, values, entropy = self._policy.evaluate(
                    batch.states, batch.actions, batch.masks
                )

                ratio = torch.exp(log_probs - batch.old_log_probs)
                clip_ratio = torch.clamp(ratio, 1.0 - self._clip_eps, 1.0 + self._clip_eps)
                adv = batch.advantages

                policy_loss = -torch.min(ratio * adv, clip_ratio * adv).mean()
                value_loss = nn.functional.mse_loss(values, batch.returns)
                entropy_loss = -entropy.mean()

                loss = (
                    policy_loss + self._value_coef * value_loss + self._entropy_coef * entropy_loss
                )

                # Add the config-head loss only on rows that actually had a
                # config decision (i.e. play == INSTANTIATE_AGENT). Skip when no
                # such rows are in this minibatch, when the policy has no config
                # head, or when the batch wasn't populated with config tensors.
                config_pl_value = 0.0
                config_ent_value = 0.0
                config_active_any = (
                    self._policy.num_configs > 0
                    and batch.config_active is not None
                    and bool(batch.config_active.any().item())
                )
                if config_active_any:
                    if batch.config_actions is None:
                        raise RuntimeError("config_actions must not be None when config_active_any")
                    if batch.config_old_log_probs is None:
                        raise RuntimeError(
                            "config_old_log_probs must not be None when config_active_any"
                        )
                    if batch.config_masks is None:
                        raise RuntimeError("config_masks must not be None when config_active_any")
                    if batch.config_active is None:
                        raise RuntimeError("config_active must not be None when config_active_any")
                    active = batch.config_active
                    cfg_log_probs, cfg_entropy = self._policy.evaluate_config(
                        batch.states[active],
                        batch.config_actions[active],
                        batch.config_masks[active],
                    )
                    cfg_old_log_probs = batch.config_old_log_probs[active]
                    cfg_adv = batch.advantages[active]

                    cfg_ratio = torch.exp(cfg_log_probs - cfg_old_log_probs)
                    cfg_clip_ratio = torch.clamp(
                        cfg_ratio, 1.0 - self._clip_eps, 1.0 + self._clip_eps
                    )
                    config_policy_loss = -torch.min(
                        cfg_ratio * cfg_adv, cfg_clip_ratio * cfg_adv
                    ).mean()
                    config_entropy_loss = -cfg_entropy.mean()

                    loss = (
                        loss
                        + self._config_policy_coef * config_policy_loss
                        + self._config_entropy_coef * config_entropy_loss
                    )
                    config_pl_value = config_policy_loss.item()
                    config_ent_value = (-config_entropy_loss).item()

                if not math.isfinite(loss.item()):
                    _logger.error("policy_rollback", reason="non_finite_loss", loss=loss.item())
                    self._policy.load_state_dict(snapshot)
                    stats.rolled_back = True
                    return stats

                self._optimizer.zero_grad()
                loss.backward()  # type: ignore[no-untyped-call]
                nn.utils.clip_grad_norm_(self._policy.parameters(), self._max_grad_norm)
                self._optimizer.step()

                with torch.no_grad():
                    kl = (batch.old_log_probs - log_probs).mean().item()
                    cf = ((ratio - 1.0).abs() > self._clip_eps).float().mean().item()

                total_pl += policy_loss.item()
                total_vl += value_loss.item()
                total_ent += (-entropy_loss).item()
                total_kl += kl
                total_cf += cf
                if config_active_any:
                    total_cpl += config_pl_value
                    total_cent += config_ent_value
                    n_config_batches += 1
                n_batches += 1

        if n_batches > 0:
            stats.policy_loss = total_pl / n_batches
            stats.value_loss = total_vl / n_batches
            stats.entropy = total_ent / n_batches
            stats.approx_kl = total_kl / n_batches
            stats.clip_fraction = total_cf / n_batches
            stats.n_epochs = self._ppo_epochs
        if n_config_batches > 0:
            stats.config_policy_loss = total_cpl / n_config_batches
            stats.config_entropy = total_cent / n_config_batches
            stats.n_config_updates = n_config_batches

        _logger.info(
            "ppo_update",
            policy_loss=stats.policy_loss,
            value_loss=stats.value_loss,
            entropy=stats.entropy,
            approx_kl=stats.approx_kl,
            clip_fraction=stats.clip_fraction,
            config_policy_loss=stats.config_policy_loss,
            config_entropy=stats.config_entropy,
            n_config_updates=stats.n_config_updates,
        )
        return stats

    def state_dict(self) -> dict[str, OptimizerStateDict]:
        return {"optimizer": self._optimizer.state_dict()}

    def load_state_dict(self, sd: dict[str, OptimizerStateDict]) -> None:
        self._optimizer.load_state_dict(sd["optimizer"])

    @property
    def entropy_coef(self) -> float:
        """Current PPO entropy coefficient used by policy updates."""
        return self._entropy_coef

    def set_entropy_coef(self, entropy_coef: float) -> None:
        """Update the PPO entropy coefficient for subsequent updates."""
        self._entropy_coef = max(0.0, float(entropy_coef))
