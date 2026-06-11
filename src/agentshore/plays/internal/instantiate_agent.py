"""InstantiateAgentPlay — spawn a new coding agent."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import aiosqlite

from agentshore.agents.model_tiers import (
    DEFAULT_MODEL_TIER,
    MODEL_TIER_ORDER,
    effective_model_tier_config,
    enabled_model_tiers,
)
from agentshore.config import AgentConfig
from agentshore.rl.mask_reason import MaskClassification, MaskReason, MaskSource
from agentshore.state import AgentStatus, AgentType, PlayOutcome, PlayType

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentshore.plays.base import PlayExecutionContext, PlayParams
    from agentshore.state import OrchestratorState

_MIN_PLAY_COST = 0.05

# Approximate dollar cost of bringing a new agent online — system prompt, tool
# manifest, project context, and skills priming. Tier-scaled because larger
# models bill more per priming token.
_TIER_SPAWN_COST: dict[str, float] = {
    "small": 0.02,
    "medium": 0.05,
}
_DEFAULT_SPAWN_COST = 0.05


def _spawn_cost(model_tier: str | None) -> float:
    return _TIER_SPAWN_COST.get(model_tier or DEFAULT_MODEL_TIER, _DEFAULT_SPAWN_COST)


def _first_enabled_config_for_tier(
    agents: Mapping[str, AgentConfig],
    preferred_tier: str,
) -> tuple[AgentType, str] | None:
    """Return the first enabled config matching *preferred_tier*, else the first enabled config."""
    fallback: tuple[AgentType, str] | None = None
    for agent_key, agent_cfg in agents.items():
        try:
            agent_type = AgentType(agent_key)
        except ValueError:
            continue
        if not agent_cfg.enabled:
            continue
        tiers = enabled_model_tiers(agent_type, agent_cfg)
        if not tiers:
            continue
        if fallback is None:
            fallback = (agent_type, tiers[0])
        if preferred_tier in tiers:
            return (agent_type, preferred_tier)
    return fallback


class InstantiateAgentPlay:
    """Spawn a new agent of the requested type.

    Per-tier spawn caps come from the ``max`` field on each ``ModelTierConfig``
    in ``agentshore.yaml``. Defaults apply when no tier config is present.
    """

    play_type = PlayType.INSTANTIATE_AGENT
    skill_name = None
    capability = None

    def preconditions(self, state: OrchestratorState) -> list[MaskReason]:
        issues: list[MaskReason] = []
        # Defer fleet *expansion* until the bootstrap first-play has completed,
        # but never block the very first spawn. With zero active agents no play
        # can run, so INSTANTIATE_AGENT must stay valid for the PPO to open an
        # empty fleet from cold (open-start boot, or mid-session fleet wipeout).
        # The gate only applies once at least one agent is already active: in
        # seed mode the first play is SEED_PROJECT/CLEANUP; in open-start any
        # non-INSTANTIATE_AGENT play is evidence the first PPO cycle progressed.
        active_agents = any(a.status in (AgentStatus.IDLE, AgentStatus.BUSY) for a in state.agents)
        has_first_play = any(
            pt != PlayType.INSTANTIATE_AGENT for pt in state.plays_since_last_play_type
        )
        if active_agents and not has_first_play:
            issues.append(
                MaskReason(
                    text="waiting for bootstrap first-play to complete before expanding the fleet",
                    classification=MaskClassification.INDEFINITE_WAIT,
                    source=MaskSource.PRECONDITION,
                )
            )
        if (
            state.budget is not None
            and state.budget.enabled
            and state.budget.remaining < _MIN_PLAY_COST
        ):
            issues.append(
                MaskReason(
                    text=f"budget too low ({state.budget.remaining:.2f} < {_MIN_PLAY_COST})",
                    classification=MaskClassification.INDEFINITE_WAIT,
                    source=MaskSource.PRECONDITION,
                )
            )
        # Count a dispatched-but-not-yet-completed instantiate toward the
        # per-tier cap. An in-flight instantiate means its slot is already
        # committed; hold the next one until it lands to prevent overshoot.
        in_flight_instantiate = sum(
            1 for pt in state.in_flight_plays if pt == PlayType.INSTANTIATE_AGENT
        )
        if in_flight_instantiate > 0:
            issues.append(
                MaskReason(
                    text=(
                        f"instantiate dispatch in flight ({in_flight_instantiate}) — "
                        "hold until it lands to prevent per-tier overshoot"
                    ),
                    classification=MaskClassification.INDEFINITE_WAIT,
                    source=MaskSource.SPAWN,
                )
            )
        return issues

    def estimated_cost(self, state: OrchestratorState) -> float:
        return _DEFAULT_SPAWN_COST

    async def execute(
        self,
        state: OrchestratorState,
        params: PlayParams,
        *,
        ctx: PlayExecutionContext,
    ) -> PlayOutcome:
        target_model_tier = params.target_model_tier or DEFAULT_MODEL_TIER
        if target_model_tier not in MODEL_TIER_ORDER:
            return PlayOutcome.failed(self.play_type, f"unknown model tier: {target_model_tier!r}")

        if params.target_agent_type is None:
            resolved = _first_enabled_config_for_tier(ctx.cfg.agents, target_model_tier)
            if resolved is None:
                return PlayOutcome.failed(self.play_type, "no enabled agent config available")
            agent_type, target_model_tier = resolved
        else:
            target_type_str = params.target_agent_type
            try:
                agent_type = AgentType(target_type_str)
            except ValueError:
                return PlayOutcome.failed(
                    self.play_type,
                    f"unknown agent type: {target_type_str!r}",
                )

        try:
            agent_cfg = ctx.cfg.agents.get(agent_type.value, AgentConfig())
        except AttributeError:
            agent_cfg = AgentConfig()
        tiers = enabled_model_tiers(agent_type, agent_cfg)
        if target_model_tier not in tiers:
            return PlayOutcome.failed(
                self.play_type,
                f"model tier {target_model_tier!r} is not enabled for {agent_type.value}",
            )

        live_agents = [a for a in state.agents if a.status.value not in ("error", "terminated")]

        config_count = sum(
            1
            for a in live_agents
            if a.agent_type == agent_type
            and (a.model_tier or DEFAULT_MODEL_TIER) == target_model_tier
        )
        tier_max = effective_model_tier_config(agent_type, agent_cfg, target_model_tier).max
        if config_count >= tier_max:
            return PlayOutcome.failed(
                self.play_type,
                f"{agent_type.value} {target_model_tier} at per-tier max "
                f"({config_count}/{tier_max})",
            )
        if any(
            a.status.value == "idle"
            and a.agent_type == agent_type
            and (a.model_tier or DEFAULT_MODEL_TIER) == target_model_tier
            for a in live_agents
        ):
            return PlayOutcome.failed(
                self.play_type,
                f"{agent_type.value} {target_model_tier} already has an idle agent available",
            )

        try:
            handle = await ctx.manager.instantiate(agent_type, model_tier=target_model_tier)
        except (aiosqlite.Error, sqlite3.Error, RuntimeError, OSError) as exc:
            return PlayOutcome.failed(self.play_type, str(exc))

        return PlayOutcome(
            play_type=self.play_type,
            agent_id=handle.agent_id,
            success=True,
            partial=False,
            duration_seconds=0.0,
            token_cost=0,
            dollar_cost=_spawn_cost(target_model_tier),
            artifacts=[
                {
                    "type": "agent",
                    "agent_id": handle.agent_id,
                    "agent_type": agent_type.value,
                    "model": handle.model,
                    "model_tier": target_model_tier,
                    "reasoning_effort": handle.reasoning_effort,
                }
            ],
            alignment_delta=0.0,
        )
