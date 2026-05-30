"""Experience rollout buffer with GAE-λ advantage estimation.

Step  — one SARS transition with PPO metadata.
RolloutBuffer — collects Steps; computes advantages on flush.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import torch

if TYPE_CHECKING:
    from collections.abc import Iterator

    from numpy.typing import NDArray

    type FloatArray = NDArray[np.float32]
    type BoolArray = NDArray[np.bool_]


@dataclass(slots=True)
class Step:
    state: FloatArray  # float32 (OBSERVATION_DIM,)
    action: int
    reward: float
    next_state: FloatArray  # float32 (OBSERVATION_DIM,)
    done: bool
    log_prob: float  # old log prob at selection time
    value: float  # value estimate at selection time
    mask: BoolArray  # bool (NUM_ACTIONS,)
    # Config-head metadata. Set on steps where the play head selected
    # INSTANTIATE_AGENT — None otherwise. The config-head loss is masked off
    # for rows with config_action is None.
    config_action: int | None = None
    config_log_prob: float | None = None
    config_mask: BoolArray | None = None  # bool (num_configs,) when set


@dataclass(slots=True)
class Batch:
    states: torch.Tensor  # (batch, obs_dim)
    actions: torch.Tensor  # (batch,) int64
    old_log_probs: torch.Tensor  # (batch,) float32
    advantages: torch.Tensor  # (batch,) float32
    returns: torch.Tensor  # (batch,) float32
    masks: torch.Tensor  # (batch, num_actions) bool
    # Config-head batch tensors. These are None when no row in the minibatch
    # had a config decision; PPOUpdater skips the config loss in that case.
    config_actions: torch.Tensor | None = None  # (batch,) int64 — 0 for inactive rows
    config_old_log_probs: torch.Tensor | None = None  # (batch,) float32
    config_masks: torch.Tensor | None = None  # (batch, num_configs) bool
    config_active: torch.Tensor | None = None  # (batch,) bool


class RolloutBuffer:
    """Stores Steps and computes GAE advantages on demand.

    Usage::

        buf = RolloutBuffer(capacity=256)
        buf.add(step)
        ...
        buf.compute_advantages(last_value, gamma=0.99, gae_lambda=0.95)
        for batch in buf.iter_minibatches(batch_size=4):
            ...
        buf.clear()
    """

    def __init__(self, capacity: int = 256) -> None:
        self._capacity = capacity
        self._steps: list[Step] = []
        self.advantages: list[float] = []
        self.returns: list[float] = []

    def add(self, step: Step) -> None:
        self._steps.append(step)

    def __len__(self) -> int:
        return len(self._steps)

    def compute_advantages(
        self,
        last_value: float,
        *,
        gamma: float = 0.99,
        gae_lambda: float = 0.95,
    ) -> None:
        """Run GAE-λ backward pass and populate self.advantages / self.returns.

        last_value: V(s_T) for bootstrap (0.0 if terminal).
        """
        n = len(self._steps)
        advantages = [0.0] * n
        returns = [0.0] * n

        last_gae = 0.0
        next_value = last_value
        for t in reversed(range(n)):
            s = self._steps[t]
            next_non_term = 0.0 if s.done else 1.0
            delta = s.reward + gamma * next_value * next_non_term - s.value
            last_gae = delta + gamma * gae_lambda * next_non_term * last_gae
            advantages[t] = last_gae
            returns[t] = last_gae + s.value
            next_value = s.value

        self.advantages = advantages
        self.returns = returns

    def iter_minibatches(
        self,
        batch_size: int,
        *,
        shuffle: bool = True,
    ) -> Iterator[Batch]:
        """Yield Batch objects of size batch_size (last batch may be smaller).

        Config-head fields are only populated when at least one step in the
        minibatch has ``config_action`` set; otherwise they are left as None
        and PPOUpdater skips the config-policy loss term for that batch.
        """
        if not self.advantages:
            return

        triples = list(zip(self._steps, self.advantages, self.returns, strict=True))
        if shuffle:
            random.shuffle(triples)

        for chunk in itertools.batched(triples, batch_size):
            steps, adv, ret = zip(*chunk, strict=True)

            config_active_list = [s.config_action is not None for s in steps]
            if any(config_active_list):
                # Determine num_configs from the first row that supplied a mask.
                ref_mask = next(
                    (s.config_mask for s in steps if s.config_mask is not None),
                    None,
                )
                if ref_mask is None:
                    msg = "Step has config_action but no config_mask"
                    raise ValueError(msg)
                num_configs = int(ref_mask.shape[0])
                config_actions_arr = np.zeros(len(steps), dtype=np.int64)
                config_log_probs_arr = np.zeros(len(steps), dtype=np.float32)
                config_masks_arr = np.zeros((len(steps), num_configs), dtype=np.bool_)
                for i, s in enumerate(steps):
                    if s.config_action is None:
                        # Inactive row: stash a permissive all-True mask so
                        # log_softmax doesn't divide by zero. The loss is
                        # masked off via config_active.
                        config_masks_arr[i, :] = True
                        continue
                    config_actions_arr[i] = s.config_action
                    config_log_probs_arr[i] = s.config_log_prob or 0.0
                    if s.config_mask is None:
                        msg = "Step has config_action but no config_mask"
                        raise ValueError(msg)
                    config_masks_arr[i, :] = s.config_mask
                config_actions_t: torch.Tensor | None = torch.tensor(
                    config_actions_arr, dtype=torch.long
                )
                config_old_log_probs_t: torch.Tensor | None = torch.tensor(
                    config_log_probs_arr, dtype=torch.float32
                )
                config_masks_t: torch.Tensor | None = torch.tensor(
                    config_masks_arr, dtype=torch.bool
                )
                config_active_t: torch.Tensor | None = torch.tensor(
                    config_active_list, dtype=torch.bool
                )
            else:
                config_actions_t = None
                config_old_log_probs_t = None
                config_masks_t = None
                config_active_t = None

            yield Batch(
                states=torch.tensor(np.stack([s.state for s in steps]), dtype=torch.float32),
                actions=torch.tensor([s.action for s in steps], dtype=torch.long),
                old_log_probs=torch.tensor([s.log_prob for s in steps], dtype=torch.float32),
                advantages=torch.tensor(adv, dtype=torch.float32),
                returns=torch.tensor(ret, dtype=torch.float32),
                masks=torch.tensor(np.stack([s.mask for s in steps]), dtype=torch.bool),
                config_actions=config_actions_t,
                config_old_log_probs=config_old_log_probs_t,
                config_masks=config_masks_t,
                config_active=config_active_t,
            )

    def clear(self) -> None:
        self._steps = []
        self.advantages = []
        self.returns = []
