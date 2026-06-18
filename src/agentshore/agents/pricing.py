"""Per-agent / per-model pricing, loaded from an external YAML touchpoint.

The canonical default table ships in the wheel at ``agentshore/data/pricing.yaml``
and is overridable by a single global file (``paths.GLOBAL_PRICING_PATH``) that
is deep-merged on top — so operators reprice models by editing one file, no code
change. :func:`load_pricebook` reads bundled + global on every call so a SIGHUP
config reload (which rebuilds :class:`~agentshore.config.RuntimeConfig`) picks up
edits and the next dispatch bills at the new rate.

Resolution at cost time: model id → agent-type default → global default, logging
a one-time warning when a known model id falls past the per-model tier so the gap
is visible and the table gets updated.
"""

from __future__ import annotations

import importlib.resources
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import structlog
import yaml

from agentshore.errors import ConfigError
from agentshore.paths import GLOBAL_PRICING_PATH

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "AgentPricing",
    "PriceBook",
    "PricingQuote",
    "bundled_pricebook",
    "default_quote",
    "load_pricebook",
]

_logger = structlog.get_logger(__name__)

# Optional rate fields that may be omitted per entry (the multipliers fill in).
_OPTIONAL_RATE_FIELDS = ("cost_per_1k_cached_input", "cost_per_1k_cache_write_input")
_REQUIRED_RATE_FIELDS = ("cost_per_1k_input", "cost_per_1k_output")

# Dedup set for fallback warnings; module-level so it survives config reloads
# (we want one warning per distinct gap, not one per dispatch).
_WARNED_FALLBACKS: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class AgentPricing:
    """Token rates + context window for a single pricing entry.

    Optional cost fields are ``None`` when the provider has no such tier;
    :class:`PricingQuote` carries the multipliers used to derive a rate in that case.
    """

    max_context: int
    cost_per_1k_input: float
    cost_per_1k_cached_input: float | None
    cost_per_1k_cache_write_input: float | None
    cost_per_1k_output: float


@dataclass(frozen=True)
class PricingQuote:
    """A resolved :class:`AgentPricing` plus the cache multipliers to apply.

    This is the unit handed to :func:`agentshore.agents.costs.estimate_cost`, so
    the cost computation needs no access to the wider config or price book.
    """

    pricing: AgentPricing
    cache_read_multiplier: float
    cache_write_multiplier: float


@dataclass(frozen=True)
class PriceBook:
    """The full pricing table: per-model entries, agent-type defaults, fallback."""

    models: Mapping[str, AgentPricing]
    agent_defaults: Mapping[str, AgentPricing]
    default: AgentPricing
    cache_read_multiplier: float
    cache_write_multiplier: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "models", MappingProxyType(dict(self.models)))
        object.__setattr__(self, "agent_defaults", MappingProxyType(dict(self.agent_defaults)))

    def resolve(self, agent_type: str | None, model: str | None) -> AgentPricing:
        """Resolve pricing: model id → agent-type default → global default.

        Falling past the per-model tier for a *named* model is logged once per
        ``(agent_type, model)`` so an unpriced model surfaces without crashing
        the play or silently mis-billing.
        """
        if model and model in self.models:
            return self.models[model]
        agent_default = self.agent_defaults.get(agent_type) if agent_type else None
        if model:
            # A model was named but isn't enumerated — surface the gap once.
            self._warn_fallback(agent_type, model, "agent_default" if agent_default else "default")
        if agent_default is not None:
            return agent_default
        return self.default

    def quote(self, agent_type: str | None, model: str | None) -> PricingQuote:
        """Resolve pricing and bundle it with this book's cache multipliers."""
        return PricingQuote(
            pricing=self.resolve(agent_type, model),
            cache_read_multiplier=self.cache_read_multiplier,
            cache_write_multiplier=self.cache_write_multiplier,
        )

    @staticmethod
    def _warn_fallback(agent_type: str | None, model: str, tier: str) -> None:
        key = (agent_type or "?", model)
        if key in _WARNED_FALLBACKS:
            return
        _WARNED_FALLBACKS.add(key)
        _logger.warning(
            "pricing_model_not_listed",
            agent_type=agent_type,
            model=model,
            fell_back_to=tier,
            hint="add this model to pricing.yaml `models:` to bill it precisely",
        )


def default_quote() -> PricingQuote:
    """A quote from the bundled (no-override) global default — last-resort.

    Used only where no resolved quote is threaded in (e.g. ``dispatch_cli``
    called directly in tests). Production dispatch always resolves a real quote
    from the live :class:`PriceBook`.
    """
    return bundled_pricebook().quote(None, None)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_pricebook() -> PriceBook:
    """Build the active :class:`PriceBook` from bundled + global YAML.

    Reads the wheel-bundled ``data/pricing.yaml`` then deep-merges the global
    override file (:data:`agentshore.paths.GLOBAL_PRICING_PATH`) when present.
    Raises :class:`ConfigError` on unreadable / malformed / invalid input.
    """
    data = dict(_read_bundled_data())
    if GLOBAL_PRICING_PATH.exists():
        try:
            text = GLOBAL_PRICING_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"could not read pricing file {GLOBAL_PRICING_PATH}: {exc}") from exc
        try:
            overlay = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {GLOBAL_PRICING_PATH}: {exc}") from exc
        if overlay is not None:
            if not isinstance(overlay, dict):
                raise ConfigError(
                    f"pricing file {GLOBAL_PRICING_PATH} root must be a mapping, "
                    f"got {type(overlay).__name__}"
                )
            data = _merge_pricing(data, overlay)
    return _build_pricebook(data)


@lru_cache(maxsize=1)
def bundled_pricebook() -> PriceBook:
    """The bundled-only price book (no global override), cached.

    Safe to cache: the wheel resource never changes at runtime. The global
    override path deliberately does NOT go through here so SIGHUP reloads see
    fresh edits via :func:`load_pricebook`. Used as the deterministic default
    for a bare ``RuntimeConfig()`` (tests / programmatic construction) so it
    never depends on whether a developer has a global pricing file.
    """
    return _build_pricebook(dict(_read_bundled_data()))


def _read_bundled_data() -> Mapping[str, Any]:
    ref = importlib.resources.files("agentshore.data").joinpath("pricing.yaml")
    raw = yaml.safe_load(ref.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("bundled pricing.yaml is malformed (root is not a mapping)")
    return raw


def _merge_pricing(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge an override over the bundled default.

    ``models`` and ``agent_defaults`` merge per key (so an override may list only
    the entries it changes); scalars and ``default`` replace wholesale.
    """
    merged = dict(base)
    for key, value in overlay.items():
        if key in ("models", "agent_defaults") and isinstance(value, dict):
            existing = dict(merged.get(key, {}) or {})
            existing.update(value)
            merged[key] = existing
        else:
            merged[key] = value
    return merged


def _build_pricebook(data: Mapping[str, Any]) -> PriceBook:
    read_mult = _coerce_multiplier(data.get("cache_read_multiplier", 0.1), "cache_read_multiplier")
    write_mult = _coerce_multiplier(
        data.get("cache_write_multiplier", 1.25), "cache_write_multiplier"
    )
    default_raw = data.get("default")
    if not isinstance(default_raw, dict):
        raise ConfigError("pricing.yaml must define a `default:` mapping")
    default = _coerce_pricing(default_raw, ctx="default")

    models = {
        str(name): _coerce_pricing(entry, ctx=f"models.{name}")
        for name, entry in _as_mapping(data.get("models"), "models").items()
    }
    agent_defaults = {
        str(name): _coerce_pricing(entry, ctx=f"agent_defaults.{name}")
        for name, entry in _as_mapping(data.get("agent_defaults"), "agent_defaults").items()
    }
    return PriceBook(
        models=models,
        agent_defaults=agent_defaults,
        default=default,
        cache_read_multiplier=read_mult,
        cache_write_multiplier=write_mult,
    )


def _as_mapping(value: object, ctx: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"pricing.{ctx} must be a mapping, got {type(value).__name__}")
    return value


def _coerce_multiplier(value: object, ctx: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"pricing.{ctx} must be a positive number, got {value!r}")
    return float(value)


def _coerce_pricing(entry: object, *, ctx: str) -> AgentPricing:
    if not isinstance(entry, dict):
        raise ConfigError(f"pricing.{ctx} must be a mapping, got {type(entry).__name__}")
    max_context = entry.get("max_context")
    if not isinstance(max_context, int) or isinstance(max_context, bool) or max_context <= 0:
        raise ConfigError(f"pricing.{ctx}.max_context must be a positive int, got {max_context!r}")
    rates: dict[str, float | None] = {}
    for field in _REQUIRED_RATE_FIELDS:
        rates[field] = _coerce_rate(entry.get(field), ctx=f"{ctx}.{field}", required=True)
    for field in _OPTIONAL_RATE_FIELDS:
        rates[field] = _coerce_rate(entry.get(field), ctx=f"{ctx}.{field}", required=False)
    # Sanity, not a hard error: output usually costs more than input. Warn so a
    # transposed edit is noticed without rejecting a deliberately odd table.
    if (rates["cost_per_1k_output"] or 0) < (rates["cost_per_1k_input"] or 0):
        _logger.warning(
            "pricing_output_cheaper_than_input",
            entry=ctx,
            cost_per_1k_input=rates["cost_per_1k_input"],
            cost_per_1k_output=rates["cost_per_1k_output"],
        )
    return AgentPricing(
        max_context=int(max_context),
        cost_per_1k_input=rates["cost_per_1k_input"] or 0.0,
        cost_per_1k_cached_input=rates["cost_per_1k_cached_input"],
        cost_per_1k_cache_write_input=rates["cost_per_1k_cache_write_input"],
        cost_per_1k_output=rates["cost_per_1k_output"] or 0.0,
    )


def _coerce_rate(value: object, *, ctx: str, required: bool) -> float | None:
    if value is None:
        if required:
            raise ConfigError(f"pricing.{ctx} is required")
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ConfigError(f"pricing.{ctx} must be a non-negative number, got {value!r}")
    return float(value)
