"""Canonical per-agent pricing/cost defaults.

This is the single source of truth for the default pricing numbers that feed
reward + budget accounting. The values were previously duplicated across the
config parser, the CLI template generator, and the embedded default YAML; they
had drifted. Every consumer now reads from :data:`AGENT_PRICING` here.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["AGENT_PRICING", "AgentPricing", "pricing_yaml_lines"]


@dataclass(frozen=True)
class AgentPricing:
    """Default pricing for a single agent type.

    Optional cost fields are ``None`` when the provider has no such tier (e.g.
    Gemini has no cached-input price). ``None`` fields are omitted from emitted
    YAML.
    """

    max_context: int
    cost_per_1k_input: float
    cost_per_1k_cached_input: float | None
    cost_per_1k_cache_write_input: float | None
    cost_per_1k_output: float


AGENT_PRICING: dict[str, AgentPricing] = {
    "claude_code": AgentPricing(
        max_context=200_000,
        cost_per_1k_input=0.003,
        cost_per_1k_cached_input=0.0003,
        cost_per_1k_cache_write_input=0.00375,
        cost_per_1k_output=0.015,
    ),
    "codex": AgentPricing(
        max_context=400_000,
        cost_per_1k_input=0.00175,
        cost_per_1k_cached_input=0.000175,
        cost_per_1k_cache_write_input=None,
        cost_per_1k_output=0.014,
    ),
    "gemini": AgentPricing(
        max_context=1_000_000,
        cost_per_1k_input=0.0005,
        cost_per_1k_cached_input=None,
        cost_per_1k_cache_write_input=None,
        cost_per_1k_output=0.003,
    ),
    "grok": AgentPricing(
        max_context=256_000,
        cost_per_1k_input=0.001,
        cost_per_1k_cached_input=0.0002,
        cost_per_1k_cache_write_input=None,
        cost_per_1k_output=0.002,
    ),
}

# Field render order for YAML emission. Optional fields are omitted when None.
_FIELD_ORDER: tuple[str, ...] = (
    "max_context",
    "cost_per_1k_input",
    "cost_per_1k_cached_input",
    "cost_per_1k_cache_write_input",
    "cost_per_1k_output",
)


def pricing_yaml_lines(agent_key: str, indent: str = "    ") -> list[str]:
    """Render the pricing YAML lines for *agent_key*.

    Emits one ``key: value`` line per non-None field, in canonical order,
    prefixed with *indent*. Returns an empty list for unknown agents.
    """
    pricing = AGENT_PRICING.get(agent_key)
    if pricing is None:
        return []
    lines: list[str] = []
    for field_name in _FIELD_ORDER:
        value = getattr(pricing, field_name)
        if value is None:
            continue
        lines.append(f"{indent}{field_name}: {value}")
    return lines
