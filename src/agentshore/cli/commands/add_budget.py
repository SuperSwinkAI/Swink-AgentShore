"""``agentshore add-budget`` subcommand.

Additively tops up a running session's budget: ``--budget`` adds dollars to the
spend cap and ``--time`` extends the wall-clock cap. Semantics are ADDITIVE
(top up / extend), never absolute — the deltas are added to the live caps. At
least one of ``--budget`` / ``--time`` is required. The actual cap arithmetic
and bounds validation live in the orchestrator; this command is the CLI
transport over the NDJSON line-IPC channel.
"""

from __future__ import annotations

from pathlib import Path

import click

from agentshore.budget import parse_duration_delta


@click.command(name="add-budget")
@click.option(
    "--project",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    help="Project root directory",
)
@click.option(
    "--budget",
    "budget",
    type=float,
    default=None,
    help="Dollars to ADD to the spend cap (e.g. 25). Additive, not absolute.",
)
@click.option(
    "--time",
    "time",
    type=str,
    default=None,
    help="Time to ADD to the wall-clock cap (e.g. '2h', '30m', or minutes). Additive.",
)
def add_budget(project: str, budget: float | None, time: str | None) -> None:
    """Top up the budget of a running AgentShore session.

    ``--budget`` adds dollars and ``--time`` extends the wall-clock cap; the
    deltas are added to the live caps (never replace them). Supply at least one.
    """
    from agentshore.session_path import is_session_running, request_add_budget

    if budget is None and time is None:
        raise click.UsageError("Provide at least one of --budget or --time.")

    delta_usd: float | None = None
    if budget is not None:
        if budget <= 0:
            raise click.BadParameter(
                "--budget must be a positive dollar amount.", param_hint="--budget"
            )
        delta_usd = float(budget)

    delta_minutes: int | None = None
    if time is not None:
        try:
            delta_minutes = parse_duration_delta(time)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--time") from exc

    project_path = Path(project).resolve()

    if not is_session_running(project_path):
        click.echo("No running AgentShore session found for this project.")
        raise SystemExit(0)

    result = request_add_budget(
        project_path,
        delta_usd=delta_usd,
        delta_minutes=delta_minutes,
    )

    if result == "no_session":
        click.echo("No running AgentShore session found for this project.")
        raise SystemExit(0)
    if result in ("error", "timeout"):
        click.echo("Error: Failed to add budget over IPC.", err=True)
        raise SystemExit(1)
    if isinstance(result, str) and result.startswith("rejected:"):
        msg = result[len("rejected:") :]
        click.echo(f"Error: Budget update rejected: {msg}", err=True)
        raise SystemExit(1)

    parts: list[str] = []
    if delta_usd is not None:
        parts.append(f"+${delta_usd:.2f}")
    if delta_minutes is not None:
        parts.append(f"+{delta_minutes}m")
    click.echo(f"Budget topped up ({', '.join(parts)}).")

    if isinstance(result, dict) and result:
        _echo_applied_caps(result)


def _echo_applied_caps(applied: dict[str, object]) -> None:
    """Echo the resulting caps + remaining from the live state snapshot."""
    if applied.get("enabled"):
        total = applied.get("total")
        remaining = applied.get("remaining")
        if isinstance(total, (int, float)):
            line = f"  Dollar cap: ${float(total):.2f}"
            if isinstance(remaining, (int, float)):
                line += f" (${float(remaining):.2f} remaining)"
            click.echo(line)
    if applied.get("time_enabled"):
        total_min = applied.get("time_total_minutes")
        remaining_min = applied.get("time_remaining_minutes")
        if isinstance(total_min, (int, float)):
            line = f"  Time cap: {int(round(float(total_min)))} min"
            if isinstance(remaining_min, (int, float)):
                line += f" ({int(round(float(remaining_min)))} min remaining)"
            click.echo(line)
