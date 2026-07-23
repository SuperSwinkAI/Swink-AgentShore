"""PPO ActorCritic policy network.

Architecture: OBSERVATION_DIM → 128 → 128 → {NUM_ACTIONS-actor, 1-value, num_configs-config}
Shared trunk, ReLU activations, separate heads. <120K parameters, CPU-only.

The config head is conditional: only sampled when the play head selects
INSTANTIATE_AGENT, only contributes to loss on those steps. See
``ActorCritic.act_config`` and ``PPOUpdater``.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

import structlog
import torch
import torch.nn as nn

from agentshore.rl.action_space import ACTION_SPACE_VERSION, NUM_ACTIONS
from agentshore.rl.config_head import POLICY_VERSION
from agentshore.rl.observation import OBSERVATION_DIM, OBSERVATION_VERSION

_logger = structlog.get_logger(__name__)


def _masked_categorical_act(
    logits: torch.Tensor,
    mask: torch.Tensor,
    *,
    greedy: bool,
    empty_mask_msg: str,
) -> tuple[int, float]:
    """Sample (or greedily pick) one index from ``logits`` restricted to ``mask``.

    Shared by ``ActorCritic.act`` and ``ActorCritic.act_config`` — both mask
    invalid entries to ``-inf``, guard against an all-masked input, then either
    argmax or multinomial-sample and read back the log-prob of the pick.
    """
    masked_logits = logits.clone()
    masked_logits[~mask] = float("-inf")

    if not mask.any():
        raise RuntimeError(empty_mask_msg)

    if greedy:
        idx = int(masked_logits.argmax().item())
    else:
        probs = torch.softmax(masked_logits, dim=0)
        idx = int(torch.multinomial(probs, 1).item())

    log_prob = float(torch.log_softmax(masked_logits, dim=0)[idx].item())
    return idx, log_prob


def _masked_categorical_evaluate(
    logits: torch.Tensor,
    actions: torch.Tensor,
    mask: torch.Tensor,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Batched (log_probs, entropy) for ``actions`` under ``logits`` restricted to ``mask``.

    Shared by ``ActorCritic.evaluate`` and ``ActorCritic.evaluate_config`` —
    same masking-then-softmax strategy, entropy computed only over valid
    (unmasked) actions to avoid ``-inf * 0 = NaN``.
    """
    masked_logits = logits.clone()
    masked_logits[~mask] = float("-inf")

    log_probs_all = torch.log_softmax(masked_logits, dim=-1)
    log_probs = log_probs_all.gather(1, actions.unsqueeze(1)).squeeze(1)

    probs = torch.softmax(masked_logits, dim=-1)
    log_p_safe = torch.where(mask, log_probs_all, torch.zeros_like(log_probs_all))
    entropy = -(probs * log_p_safe).sum(dim=-1)

    return log_probs, entropy


class IncompatibleCheckpointError(ValueError):
    """Raised when a checkpoint's version metadata doesn't match the current build.

    Triggered for any of: ``action_space_version``, ``policy_version``,
    ``observation_version``, or shape mismatches on the actor/config heads.
    """


class ActorCritic(nn.Module):
    """Shared-trunk MLP with actor, value, and config heads.

    Input:  (batch, OBSERVATION_DIM) float32
    Actor:  (batch, NUM_ACTIONS) logits — which play to take
    Value:  (batch, 1) scalar
    Config: (batch, num_configs) logits — which (agent_type, model_tier) to spawn
            when the chosen play is INSTANTIATE_AGENT. ``num_configs`` may be 0
            if no agents are configured; a stub head is created so save/load
            stays uniform.
    """

    def __init__(
        self,
        obs_dim: int = OBSERVATION_DIM,
        num_actions: int = NUM_ACTIONS,
        hidden: int = 128,
        *,
        num_configs: int = 0,
    ) -> None:
        super().__init__()
        if num_configs < 0:
            msg = f"num_configs must be >= 0, got {num_configs}"
            raise ValueError(msg)
        self.trunk = nn.Sequential(
            nn.Linear(obs_dim, hidden),
            nn.ReLU(),
            nn.Linear(hidden, hidden),
            nn.ReLU(),
        )
        self.actor = nn.Linear(hidden, num_actions)
        self.value_head = nn.Linear(hidden, 1)
        # Always create the config head so the state dict has a stable key set.
        # When num_configs == 0, the head is a degenerate Linear with output
        # dim 1 that callers must not invoke (mask would be all-zero anyway).
        self._num_configs = num_configs
        self.config_head = nn.Linear(hidden, max(num_configs, 1))

    @property
    def num_configs(self) -> int:
        return self._num_configs

    def forward(self, obs: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """Return (logits, value)."""
        h = self.trunk(obs)
        return self.actor(h), self.value_head(h)

    def forward_config(self, obs: torch.Tensor) -> torch.Tensor:
        """Return raw config-head logits for (batch, num_configs)."""
        h = self.trunk(obs)
        out: torch.Tensor = self.config_head(h)
        return out

    def act(
        self,
        obs: torch.Tensor,
        mask: torch.Tensor,
        *,
        greedy: bool = False,
    ) -> tuple[int, float, float]:
        """Select an action.

        Returns (action_idx, log_prob, value_estimate).
        Mask is a boolean tensor: True = allowed.
        Raises RuntimeError if all actions are masked.
        """
        with torch.no_grad():
            logits, v = self.forward(obs.unsqueeze(0))
            logits = logits.squeeze(0)
            v = v.squeeze()

            action, log_prob = _masked_categorical_act(
                logits,
                mask,
                greedy=greedy,
                empty_mask_msg="All actions are masked — cannot select action",
            )
            value = float(v.item())

        return action, log_prob, value

    def evaluate(
        self,
        obs: torch.Tensor,
        actions: torch.Tensor,
        mask: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Used by PPOUpdater: return (log_probs, values, entropy) for a batch.

        obs:     (batch, obs_dim)
        actions: (batch,) int64
        mask:    (batch, num_actions) bool
        """
        logits, values = self.forward(obs)
        log_probs, entropy = _masked_categorical_evaluate(logits, actions, mask)
        return log_probs, values.squeeze(-1), entropy

    def value(self, obs: torch.Tensor) -> float:
        """Return scalar value estimate for GAE bootstrap."""
        with torch.no_grad():
            _, v = self.forward(obs.unsqueeze(0))
        return float(v.squeeze().item())

    def act_config(
        self,
        obs: torch.Tensor,
        mask: torch.Tensor,
        *,
        greedy: bool = False,
    ) -> tuple[int, float]:
        """Sample a config index for instantiate_agent.

        Returns ``(config_idx, log_prob)``. ``mask`` has length equal to the
        head's logical output dimension (``num_configs``); pass an all-False
        mask only after ``compute_action_mask`` has already excluded
        ``INSTANTIATE_AGENT`` — calling this with no eligible config is a bug.
        """
        if self._num_configs == 0:
            msg = "act_config called on a policy with num_configs == 0"
            raise RuntimeError(msg)
        with torch.no_grad():
            logits = self.forward_config(obs.unsqueeze(0)).squeeze(0)
            # Trim padding (the head may be wider than num_configs by 1).
            logits = logits[: self._num_configs]
            idx, log_prob = _masked_categorical_act(
                logits,
                mask,
                greedy=greedy,
                empty_mask_msg="All config slots are masked — cannot select config",
            )

        return idx, log_prob

    def evaluate_config(
        self,
        obs: torch.Tensor,
        config_actions: torch.Tensor,
        masks: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Used by PPOUpdater on minibatch rows where the config head fired.

        obs:             (batch, obs_dim)
        config_actions:  (batch,) int64
        masks:           (batch, num_configs) bool

        Returns ``(log_probs, entropy)``. Same masking strategy as ``evaluate``.
        """
        if self._num_configs == 0:
            msg = "evaluate_config called on a policy with num_configs == 0"
            raise RuntimeError(msg)
        logits = self.forward_config(obs)[:, : self._num_configs]
        return _masked_categorical_evaluate(logits, config_actions, masks)

    def save(self, path: Path) -> None:
        """Atomically write weights + metadata to *path*."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "state_dict": self.state_dict(),
            "action_space_version": ACTION_SPACE_VERSION,
            "policy_version": POLICY_VERSION,
            "observation_version": OBSERVATION_VERSION,
            "obs_dim": OBSERVATION_DIM,
            "num_actions": NUM_ACTIONS,
            "num_configs": self._num_configs,
        }
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            os.close(fd)
            torch.save(payload, tmp)
            os.replace(tmp, path)
        except (OSError, RuntimeError) as exc:
            _logger.error("policy_save_failed", path=str(path), error=str(exc))
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    @classmethod
    def load(cls, path: Path) -> ActorCritic:
        """Load weights from *path*.

        Raises ``IncompatibleCheckpointError`` if any of action_space_version,
        policy_version, or observation_version disagree with the current build.
        Hard-reset by design — the config head is fresh for each POLICY_VERSION
        bump.

        ``num_configs`` is *not* gated: it is read from the checkpoint to size
        the config head, so a checkpoint trained on one agent roster loads with
        that roster's head width. Callers that mix rosters (e.g. the shipped
        warm-start seed) must reconcile the width themselves — the seed ships
        with ``num_configs == 0`` for exactly this reason.
        """
        payload = torch.load(Path(path), map_location="cpu", weights_only=True)
        saved_action_ver = payload.get("action_space_version")
        if saved_action_ver != ACTION_SPACE_VERSION:
            raise IncompatibleCheckpointError(
                f"Checkpoint action_space_version={saved_action_ver!r} "
                f"!= current {ACTION_SPACE_VERSION!r}"
            )
        saved_policy_ver = payload.get("policy_version")
        if saved_policy_ver != POLICY_VERSION:
            raise IncompatibleCheckpointError(
                f"Checkpoint policy_version={saved_policy_ver!r} != current {POLICY_VERSION!r}"
            )
        saved_obs_ver = payload.get("observation_version")
        if saved_obs_ver is not None and saved_obs_ver != OBSERVATION_VERSION:
            raise IncompatibleCheckpointError(
                f"Checkpoint observation_version={saved_obs_ver!r} "
                f"!= current {OBSERVATION_VERSION!r}"
            )
        obs_dim = payload.get("obs_dim", OBSERVATION_DIM)
        num_actions = payload.get("num_actions", NUM_ACTIONS)
        num_configs = int(payload.get("num_configs", 0))
        model = cls(obs_dim=obs_dim, num_actions=num_actions, num_configs=num_configs)
        model.load_state_dict(payload["state_dict"])
        model.eval()
        return model
