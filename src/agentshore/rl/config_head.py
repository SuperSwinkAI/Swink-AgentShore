"""Config-head action space (second policy head for instantiate_agent).

These symbols govern the policy's *config head* — the (agent_type, model_tier)
selection that backs ``instantiate_agent`` — not the play action space. They are
versioned independently of ``ACTION_SPACE_VERSION`` via ``POLICY_VERSION``: the
config head is hard-reset whenever ``POLICY_VERSION`` changes, so old checkpoints
with a mismatched ``policy_version`` are rejected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, NamedTuple

from agentshore.agents.model_tiers import MODEL_TIER_PRIORITY, enabled_model_tiers
from agentshore.state import AgentType

if TYPE_CHECKING:
    from agentshore.config.models import RuntimeConfig


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
