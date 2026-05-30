"""RL engine exports with lazy loading.

Keeping this module lazy prevents importing torch during cold-start paths that
only need lightweight submodules (for example, sidecar startup checks).
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "ActorCritic",
    "apply_cold_start_bias",
    "compute_reward",
    "encode_observation",
    "NUM_ACTIONS",
    "OBSERVATION_DIM",
    "PLAY_TO_INDEX",
    "PPOSelector",
    "PPOUpdater",
    "ReplayLoader",
    "RewardBreakdown",
    "RewardSignals",
    "RolloutBuffer",
    "Step",
    "UpdateStats",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    "NUM_ACTIONS": ("agentshore.rl.action_space", "NUM_ACTIONS"),
    "PLAY_TO_INDEX": ("agentshore.rl.action_space", "PLAY_TO_INDEX"),
    "apply_cold_start_bias": ("agentshore.rl.cold_start", "apply_cold_start_bias"),
    "RolloutBuffer": ("agentshore.rl.experience", "RolloutBuffer"),
    "Step": ("agentshore.rl.experience", "Step"),
    "OBSERVATION_DIM": ("agentshore.rl.observation", "OBSERVATION_DIM"),
    "encode_observation": ("agentshore.rl.observation", "encode_observation"),
    "ActorCritic": ("agentshore.rl.policy", "ActorCritic"),
    "ReplayLoader": ("agentshore.rl.replay", "ReplayLoader"),
    "RewardBreakdown": ("agentshore.rl.reward", "RewardBreakdown"),
    "RewardSignals": ("agentshore.rl.reward", "RewardSignals"),
    "compute_reward": ("agentshore.rl.reward", "compute_reward"),
    "PPOSelector": ("agentshore.rl.selector", "PPOSelector"),
    "PPOUpdater": ("agentshore.rl.training", "PPOUpdater"),
    "UpdateStats": ("agentshore.rl.training", "UpdateStats"),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    module_name, attr_name = target
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
