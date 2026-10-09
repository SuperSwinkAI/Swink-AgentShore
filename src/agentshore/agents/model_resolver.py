"""Live model availability: resolve configured/default models against what the
installed CLI actually offers right now.

Model names rot — CLIs retire models between releases (agy dropped
``Gemini 3.5 Flash (High)`` in 1.3, codex hid ``gpt-5.4`` from ChatGPT
accounts). Configured and default models are therefore *preferences*: each
agent type's live list is discovered once per process through the free CLI
probes in :mod:`model_discovery`, and a preference that is no longer offered
is swapped for the closest available model instead of dispatching a dead name
that would permanently error the agent.

Resolution is cache-only and synchronous so every consumer of
:func:`model_tiers.effective_model_tier_config` (spawn, eligibility mask,
resolver, reports) sees the same answer. Discovery runs via
:func:`ensure_discovered` (async, on agent spawn) and is re-run after a
dispatch reports INVALID_MODEL (:func:`mark_model_invalid`). With nothing
discovered (Claude Code has no free probe; a probe failed) a preference passes
through unchanged unless a dispatch proved it invalid.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from agentshore.agents.model_discovery import DiscoveryResult
    from agentshore.state import AgentType

_logger = structlog.get_logger(__name__)

# Canonical effort ladder used to pick the nearest supported effort.
_EFFORT_LADDER: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)

_VERSION_RE = re.compile(r"\d+(?:[.\-]\d+)*")


@dataclass
class _Live:
    models: tuple[str, ...] = ()
    default: str | None = None
    efforts: dict[str, tuple[str, ...]] = field(default_factory=dict)


# ponytail: process-global cache, refreshed only on INVALID_MODEL; add a TTL if
# a long-lived sidecar needs to notice newly *added* models without a restart.
_live: dict[AgentType, _Live] = {}
_invalid: dict[AgentType, set[str]] = {}
_stale: set[AgentType] = set()
_locks: dict[AgentType, asyncio.Lock] = {}
# Resolution runs every tick (eligibility mask); log each substitution once.
_announced: set[tuple[str, str, str | None, str | None]] = set()


def _announce(
    event: str, agent_type: AgentType, configured: str | None, resolved: str | None
) -> None:
    key = (event, agent_type.value, configured, resolved)
    if key in _announced:
        return
    _announced.add(key)
    _logger.info(event, agent_type=agent_type.value, configured=configured, resolved=resolved)


def record_discovery(agent_type: AgentType, result: DiscoveryResult) -> None:
    """Cache a successful discovery result; ignore non-ok ones."""
    if result.status != "ok" or not result.models:
        return
    _live[agent_type] = _Live(result.models, result.default, dict(result.efforts))
    _stale.discard(agent_type)


async def ensure_discovered(agent_type: AgentType) -> None:
    """Run the free model probe for *agent_type* once (again after invalidation).

    Never raises: a failed or unsupported probe just leaves preferences
    unresolved. The probe is blocking, so it runs in a worker thread.
    """
    from agentshore.agents.model_discovery import free_discovery_func

    if agent_type in _live and agent_type not in _stale:
        return
    func = free_discovery_func(agent_type.value)
    if func is None:
        return
    lock = _locks.setdefault(agent_type, asyncio.Lock())
    async with lock:
        if agent_type in _live and agent_type not in _stale:
            return
        try:
            result = await asyncio.to_thread(func)
        except Exception as exc:  # discovery is best-effort
            _logger.warning("model_discovery_failed", agent_type=agent_type.value, error=str(exc))
            _stale.discard(agent_type)
            return
        if result.status != "ok":
            _logger.warning(
                "model_discovery_unavailable",
                agent_type=agent_type.value,
                status=result.status,
                detail=result.detail,
            )
            # Keep any previous list; stop re-probing until the next invalidation.
            _stale.discard(agent_type)
            return
        record_discovery(agent_type, result)
        _write_through(agent_type, result.models)


def _write_through(agent_type: AgentType, models: tuple[str, ...]) -> None:
    """Persist the live list to the global catalog so the wizard/desktop see it."""
    from agentshore.agents.model_catalog import write_model_catalog_override

    try:
        write_model_catalog_override({agent_type.value: list(models)})
    except Exception as exc:  # a read-only home dir must not break dispatch
        _logger.debug("model_catalog_write_through_failed", error=str(exc))


def mark_model_invalid(agent_type: AgentType, model: str | None) -> None:
    """Record that *model* was rejected by the CLI and schedule a re-probe."""
    if not model:
        return
    _invalid.setdefault(agent_type, set()).add(model)
    _stale.add(agent_type)
    _logger.warning("model_marked_invalid", agent_type=agent_type.value, model=model)


def _family(name: str) -> str:
    return _VERSION_RE.sub("#", name.lower())


def _version(name: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", name))


def _family_match(name: str, candidates: tuple[str, ...]) -> str | None:
    """Newest candidate in the same family as *name* (version digits ignored),
    e.g. ``Gemini 3.5 Flash (High)`` -> ``Gemini 3.8 Flash (High)``."""
    fam = _family(name)
    same = [c for c in candidates if _family(c) == fam]
    return max(same, key=_version) if same else None


def resolve_model(
    agent_type: AgentType, model: str | None, fallback: str | None = None
) -> str | None:
    """Return the model to actually dispatch for preference *model*.

    *fallback* is the tier's built-in default, tried when *model* itself is
    gone. Order: model → fallback → same-family newest of either → the CLI's
    own default → the first offered model.
    """
    if not model:
        return model
    bad = _invalid.get(agent_type, set())
    live = _live.get(agent_type)
    prefs = [p for p in (model, fallback) if p]
    if live is None:
        # Nothing discovered: trust the preference unless a dispatch rejected it.
        chosen = next((p for p in prefs if p not in bad), model)
    else:
        usable = tuple(m for m in live.models if m not in bad)
        chosen = (
            next((p for p in prefs if p in usable), None)
            or next((m for p in prefs if (m := _family_match(p, usable))), None)
            or (live.default if live.default in usable else None)
            or (usable[0] if usable else model)
        )
    if chosen != model:
        _announce("model_substituted", agent_type, model, chosen)
    return chosen


def resolve_effort(
    agent_type: AgentType,
    model: str | None,
    effort: str | None,
    vocabulary: tuple[str, ...],
) -> str | None:
    """Clamp *effort* to the nearest level the model (or CLI) supports.

    Per-model levels come from discovery when the CLI reports them (codex);
    otherwise the static per-CLI *vocabulary* applies.
    """
    if not effort:
        return effort
    live = _live.get(agent_type)
    supported = (live.efforts.get(model or "") if live else None) or vocabulary
    if not supported or effort in supported:
        return effort
    if effort not in _EFFORT_LADDER:
        return supported[0]
    want = _EFFORT_LADDER.index(effort)
    ranked = [s for s in supported if s in _EFFORT_LADDER]
    if not ranked:
        return effort
    chosen = min(
        ranked, key=lambda s: (abs(_EFFORT_LADDER.index(s) - want), -_EFFORT_LADDER.index(s))
    )
    _announce("reasoning_effort_clamped", agent_type, effort, chosen)
    return chosen


def reset() -> None:
    """Drop all cached availability (tests)."""
    _live.clear()
    _invalid.clear()
    _stale.clear()
    _locks.clear()
    _announced.clear()
