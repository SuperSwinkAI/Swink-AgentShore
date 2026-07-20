"""Single-source-of-truth orchestrator behind ``agentshore models refresh``.

Ties together the two discovery mechanisms (model_discovery.py's free CLI
probes, model_discovery_llm.py's opt-in paid Claude Code dispatch), diffs the
result against the current catalog, cross-checks new models against the
pricebook, and writes successful harnesses through to the global override
file. Both the CLI command (cli/commands/models.py) and the desktop RPC
handler (sidecar/rpc/handlers/agents.py) call :func:`refresh_model_catalog`
directly so there is exactly one place that decides what "a refresh" means.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from agentshore.agents import model_discovery, model_discovery_llm
from agentshore.agents.model_catalog import load_model_catalog, write_model_catalog_override
from agentshore.agents.pricing import load_pricebook

HarnessStatus = Literal["ok", "unavailable", "timeout", "error", "budget_exceeded", "skipped"]


@dataclass(frozen=True)
class HarnessRefreshOutcome:
    """Per-harness result of one refresh round.

    ``models`` is the harness's list after this round — the freshly
    discovered list on ``ok``, or the pre-refresh list unchanged on any
    other status (nothing gets written for a harness that didn't succeed).
    """

    agent_key: str
    status: HarnessStatus
    models: tuple[str, ...]
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    detail: str = ""
    cost_usd: float = 0.0


@dataclass(frozen=True)
class ModelRefreshSummary:
    harnesses: dict[str, HarnessRefreshOutcome] = field(default_factory=dict)
    unpriced_models: tuple[tuple[str, str], ...] = ()
    total_cost_usd: float = 0.0
    dry_run: bool = False

    @property
    def any_changes(self) -> bool:
        return any(o.added or o.removed for o in self.harnesses.values())

    def to_jsonable(self) -> dict[str, object]:
        """Plain-dict projection for the RPC layer / desktop JSON transport."""
        return {
            "harnesses": {
                key: {
                    "status": outcome.status,
                    "models": list(outcome.models),
                    "added": list(outcome.added),
                    "removed": list(outcome.removed),
                    "detail": outcome.detail,
                    "cost_usd": outcome.cost_usd,
                }
                for key, outcome in self.harnesses.items()
            },
            "unpriced_models": [list(pair) for pair in self.unpriced_models],
            "total_cost_usd": self.total_cost_usd,
            "dry_run": self.dry_run,
            "any_changes": self.any_changes,
        }


def _diff(
    before: tuple[str, ...], after: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    before_set, after_set = set(before), set(after)
    added = tuple(m for m in after if m not in before_set)
    removed = tuple(m for m in before if m not in after_set)
    return added, removed


def _unpriced(agent_key: str, models: tuple[str, ...], priced: object) -> list[tuple[str, str]]:
    return [(agent_key, model) for model in models if model not in priced]  # type: ignore[operator]


def refresh_model_catalog(
    *,
    include_claude_code: bool = False,
    claude_code_tier: str = model_discovery_llm.DEFAULT_MODEL_TIER,
    claude_code_max_budget_usd: float = model_discovery_llm.DEFAULT_MAX_BUDGET_USD,
    dry_run: bool = False,
    timeout: float = model_discovery.DEFAULT_DISCOVERY_TIMEOUT_S,
    claude_code_timeout: float = model_discovery_llm.DEFAULT_DISCOVERY_TIMEOUT_S,
) -> ModelRefreshSummary:
    """Probe every harness's current models and (unless dry_run) write the diff.

    Codex/Grok/Antigravity are always probed (free). Claude Code is probed
    only when *include_claude_code* is True — that path spends real API
    tokens (see model_discovery_llm.py) and callers MUST have already gotten
    explicit user consent before setting this flag; refresh_model_catalog
    performs no consent gate of its own.
    """
    before = load_model_catalog()
    book = load_pricebook()

    outcomes: dict[str, HarnessRefreshOutcome] = {}
    free_results = model_discovery.discover_all(timeout=timeout)
    for agent_key, result in free_results.items():
        prior = tuple(before.get(agent_key, ()))
        if result.status == "ok":
            added, removed = _diff(prior, result.models)
            outcomes[agent_key] = HarnessRefreshOutcome(
                agent_key, "ok", result.models, added, removed, result.detail
            )
        else:
            outcomes[agent_key] = HarnessRefreshOutcome(
                agent_key, result.status, prior, detail=result.detail
            )

    if include_claude_code:
        llm_result = model_discovery_llm.discover_claude_code_models_via_agent(
            tier=claude_code_tier,
            max_budget_usd=claude_code_max_budget_usd,
            timeout=claude_code_timeout,
        )
        prior = tuple(before.get("claude_code", ()))
        if llm_result.status == "ok":
            added, removed = _diff(prior, llm_result.models)
            outcomes["claude_code"] = HarnessRefreshOutcome(
                "claude_code",
                "ok",
                llm_result.models,
                added,
                removed,
                llm_result.detail,
                llm_result.cost_usd,
            )
        else:
            outcomes["claude_code"] = HarnessRefreshOutcome(
                "claude_code",
                llm_result.status,
                prior,
                detail=llm_result.detail,
                cost_usd=llm_result.cost_usd,
            )
    else:
        outcomes["claude_code"] = HarnessRefreshOutcome(
            "claude_code",
            "skipped",
            tuple(before.get("claude_code", ())),
            detail="not requested (spends API tokens; opt in explicitly)",
        )

    unpriced: list[tuple[str, str]] = []
    for outcome in outcomes.values():
        if outcome.status == "ok":
            unpriced.extend(_unpriced(outcome.agent_key, outcome.added, book.models))

    total_cost = sum(o.cost_usd for o in outcomes.values())

    if not dry_run:
        updates = {
            outcome.agent_key: list(outcome.models)
            for outcome in outcomes.values()
            if outcome.status == "ok"
        }
        if updates:
            write_model_catalog_override(updates)

    return ModelRefreshSummary(
        harnesses=outcomes,
        unpriced_models=tuple(unpriced),
        total_cost_usd=total_cost,
        dry_run=dry_run,
    )
