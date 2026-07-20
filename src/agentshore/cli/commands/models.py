"""``agentshore models`` command group."""

from __future__ import annotations

from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from agentshore.agents.model_refresh import HarnessRefreshOutcome


@click.group()
def models() -> None:
    """Inspect or refresh the per-harness model catalog."""


@models.command()
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would change without writing the override file.",
)
@click.option(
    "--include-claude-code",
    is_flag=True,
    help=(
        "Also refresh Claude Code's model list via a live agent dispatch "
        "(spends real API tokens, roughly $0.30-0.50; prompts for confirmation "
        "unless --yes)."
    ),
)
@click.option("--yes", "-y", is_flag=True, help="Skip the Claude Code cost confirmation prompt.")
@click.option(
    "--tier",
    default=None,
    help="Model tier for the Claude Code discovery dispatch (default: haiku).",
)
@click.option(
    "--max-budget-usd",
    default=None,
    type=float,
    help="Hard spend cap for the Claude Code discovery dispatch (default: 0.75).",
)
def refresh(
    dry_run: bool,
    include_claude_code: bool,
    yes: bool,
    tier: str | None,
    max_budget_usd: float | None,
) -> None:
    """Probe each harness's CLI for its current models and update the catalog.

    Codex, Grok, and Antigravity are probed for free via their own CLI
    subcommands — no API key, no token spend. Claude Code has no such
    surface: refreshing it requires --include-claude-code, which dispatches
    a real, paid LLM agent call and asks for confirmation first (unless
    --yes is passed).
    """
    from agentshore.agents.model_discovery_llm import DEFAULT_MAX_BUDGET_USD, DEFAULT_MODEL_TIER
    from agentshore.agents.model_refresh import refresh_model_catalog

    resolved_tier = tier or DEFAULT_MODEL_TIER
    resolved_budget = DEFAULT_MAX_BUDGET_USD if max_budget_usd is None else max_budget_usd

    if include_claude_code and not yes:
        click.echo(
            "Refreshing Claude Code's model list dispatches a live agent call "
            f"(tier: {resolved_tier}) that spends real API tokens. Measured cost "
            "for a successful run is roughly $0.30-0.50; this run is hard-capped "
            f"at ${resolved_budget:g}."
        )
        if not click.confirm("Continue?", default=False):
            click.echo("Skipping Claude Code; refreshing the other harnesses only.")
            include_claude_code = False

    summary = refresh_model_catalog(
        include_claude_code=include_claude_code,
        claude_code_tier=resolved_tier,
        claude_code_max_budget_usd=resolved_budget,
        dry_run=dry_run,
    )

    for agent_key, outcome in summary.harnesses.items():
        _echo_outcome(agent_key, outcome)

    if summary.unpriced_models:
        click.echo()
        click.echo("New models with no pricing.yaml row (will bill at the agent-default rate):")
        for agent_key, model in summary.unpriced_models:
            click.echo(f"  - {agent_key}: {model}")

    if summary.total_cost_usd:
        click.echo(f"\nTotal spend this refresh: ${summary.total_cost_usd:.4f}")

    if dry_run:
        click.echo("\n(dry run — nothing written)")
    elif summary.any_changes:
        from agentshore.paths import GLOBAL_MODELS_PATH

        click.echo(f"\nWrote {GLOBAL_MODELS_PATH}")
    else:
        click.echo("\nNo changes.")


def _echo_outcome(agent_key: str, outcome: HarnessRefreshOutcome) -> None:
    if outcome.status == "ok":
        if outcome.added or outcome.removed:
            parts = []
            if outcome.added:
                parts.append(f"+{', '.join(outcome.added)}")
            if outcome.removed:
                parts.append(f"-{', '.join(outcome.removed)}")
            click.echo(f"{agent_key}: {' '.join(parts)}")
        else:
            click.echo(f"{agent_key}: no change ({len(outcome.models)} models)")
    elif outcome.status == "skipped":
        click.echo(f"{agent_key}: skipped ({outcome.detail})")
    elif outcome.status == "unavailable":
        click.echo(f"{agent_key}: CLI not found on PATH")
    else:
        click.echo(f"{agent_key}: {outcome.status} — {outcome.detail}")
