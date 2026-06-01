"""``agentshore configure`` subcommand.

Helpers go through ``agentshore.cli`` so tests can patch them via
``agentshore.cli._detect_agents`` and friends after the package split.
"""

from __future__ import annotations

from pathlib import Path

import click

from agentshore import cli as _cli_pkg


@click.command()
@click.option(
    "--project",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    show_default=True,
    help="Target project directory",
)
def configure(project: str) -> None:
    """Re-run both wizards (agent tiers + GitHub identities) over the existing config.

    Refreshes ``~/.config/swink/agentshore/availability.yaml`` first so the candidate lists
    reflect the current machine. The agent-tier picker and identity wizard
    both prefill from the existing ``agentshore.yaml``: current ``model_tiers``
    enabled state pre-checked; current per-agent ``identity`` binding
    annotated as ``(current)``.

    Use this when you want to change settings without resetting the database.
    ``agentshore init --force`` has its own merge path for fresh template defaults.
    """
    project_path = Path(project).resolve()
    cfg_path = project_path / "agentshore.yaml"
    if not cfg_path.exists():
        click.echo(f"No agentshore.yaml at {cfg_path}. Run `agentshore init` first.", err=True)
        raise SystemExit(1)

    from agentshore.availability import refresh as refresh_availability
    from agentshore.config import load_config
    from agentshore.identity_wizard import run_identity_wizard

    refresh_availability()

    cfg = load_config(cfg_path)
    detected = _cli_pkg._detect_agents() or list(cfg.agents.keys())
    cfg = _cli_pkg._interactive_agent_select(cfg, detected, cfg_path, force_run=True)

    agent_keys = _cli_pkg._agent_keys_from_yaml(cfg_path)
    if agent_keys:
        defaults = _cli_pkg._identity_defaults_from_yaml(cfg_path)
        existing = _cli_pkg._existing_identities_from_yaml(cfg_path)
        run_identity_wizard(
            cfg_path,
            agent_keys,
            force_run=True,
            defaults=defaults,
            existing_identities=existing,
            repo_name_with_owner=_cli_pkg._identity_repo_name_with_owner(project_path),
        )
