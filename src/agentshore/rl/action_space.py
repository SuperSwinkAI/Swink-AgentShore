"""V1 action space constants.

The tensor shape and slot order are locked. A reserved slot may be filled in
place without bumping ACTION_SPACE_VERSION so existing learned weights load.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, NamedTuple

from agentshore.agents.model_tiers import MODEL_TIER_PRIORITY, enabled_model_tiers
from agentshore.state import AgentType, PlayType

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentshore.config.models import RuntimeConfig

# Declaration order of PlayType enum IS the canonical V1 action ordering.
V1_ACTION_ORDER: Final[tuple[PlayType, ...]] = tuple(PlayType)
NUM_ACTIONS: Final[int] = 22
ACTION_SPACE_VERSION: Final[int] = 13

# Sanity-checked at import time — guards against accidental enum reordering.
if len(V1_ACTION_ORDER) != NUM_ACTIONS:
    msg = f"V1_ACTION_ORDER has {len(V1_ACTION_ORDER)} entries, expected {NUM_ACTIONS}"
    raise ValueError(msg)

PLAY_TO_INDEX: Final[Mapping[PlayType, int]] = {pt: i for i, pt in enumerate(V1_ACTION_ORDER)}
INDEX_TO_PLAY: Final[Mapping[int, PlayType]] = {i: pt for i, pt in enumerate(V1_ACTION_ORDER)}


# ---------------------------------------------------------------------------
# Config-head action space (second policy head for instantiate_agent)
# ---------------------------------------------------------------------------


class ConfigKey(NamedTuple):
    """A spawnable agent config: (agent_type.value, model_tier)."""

    agent_type: str
    model_tier: str


# Sanity cap on the config head's output dimension. Real deployments use 4–6
# configs (claude_code/codex × small/medium/large) so 32 is plenty of headroom.
MAX_CONFIG_INDEX_SIZE: Final[int] = 32

# Bumped independently of ACTION_SPACE_VERSION when the config head's shape or
# semantics change. Old checkpoints with mismatched policy_version are rejected.
POLICY_VERSION: Final[int] = 5


def build_config_index(cfg: RuntimeConfig) -> tuple[ConfigKey, ...]:
    """Enumerate (agent_type, model_tier) pairs for enabled agents/tiers.

    Order is deterministic: configured agent order outer, MODEL_TIER_PRIORITY inner.
    The same enumeration is used by the resolver, observation features, mask,
    cold-start, and selector so all components see consistent indices.
    """
    pairs: list[ConfigKey] = []
    for agent_key, agent_cfg in cfg.agents.items():
        try:
            agent_type = AgentType(agent_key)
        except ValueError:
            continue
        if not agent_cfg.enabled:
            continue
        tiers = enabled_model_tiers(agent_type, agent_cfg)
        # Iterate in priority order rather than tier-config order to match the
        # resolver's existing behaviour.
        for tier in MODEL_TIER_PRIORITY:
            if tier in tiers:
                pairs.append(ConfigKey(agent_type.value, tier))

    if len(pairs) > MAX_CONFIG_INDEX_SIZE:
        msg = (
            f"build_config_index produced {len(pairs)} configs, "
            f"exceeds MAX_CONFIG_INDEX_SIZE={MAX_CONFIG_INDEX_SIZE}"
        )
        raise ValueError(msg)
    return tuple(pairs)
