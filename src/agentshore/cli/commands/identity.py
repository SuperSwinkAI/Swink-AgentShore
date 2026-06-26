"""``agentshore identity`` subcommand."""

from __future__ import annotations

from pathlib import Path

import click

from agentshore.cli.identity_helpers import (
    _agent_keys_from_yaml,
    _existing_identities_from_yaml,
    _identity_defaults_from_yaml,
    _identity_repo_name_with_owner,
)


@click.command()
@click.option(
    "--project",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    show_default=True,
    help="Target project directory",
)
@click.option(
    "--reconfigure",
    is_flag=True,
    help="Re-run the identity wizard against an existing agentshore.yaml (no DB reset).",
)
def identity(project: str, reconfigure: bool) -> None:
    """Print or reconfigure the per-agent GitHub identity bindings.

    Default mode is a read-only diagnostic — useful to verify token wiring
    before ``agentshore start``. Pass ``--reconfigure`` to re-run the
    interactive wizard against an existing project; new bindings are
    merged into ``agentshore.yaml`` and the SQLite database is left untouched.
    """
    project_path = Path(project).resolve()
    cfg_path = project_path / "agentshore.yaml"
    if not cfg_path.exists():
        click.echo(f"No agentshore.yaml at {cfg_path}.", err=True)
        raise SystemExit(1)

    if reconfigure:
        from agentshore.availability import refresh as refresh_availability
        from agentshore.identity_wizard import run_identity_wizard

        agent_keys = _agent_keys_from_yaml(cfg_path)
        if not agent_keys:
            click.echo("No CLI agents in agentshore.yaml; nothing to bind.", err=True)
            return
        refresh_availability()
        defaults = _identity_defaults_from_yaml(cfg_path)
        existing = _existing_identities_from_yaml(cfg_path)
        run_identity_wizard(
            cfg_path,
            agent_keys,
            force_run=True,
            defaults=defaults,
            existing_identities=existing,
            repo_name_with_owner=_identity_repo_name_with_owner(project_path),
        )
        return

    from agentshore.agents.identity import (
        bad_identity_rows,
        report_identities,
        report_identity_repo_access,
    )
    from agentshore.config import load_config
    from agentshore.identity_wizard import echo_identity_report, echo_repo_access_report

    cfg = load_config(cfg_path)
    rows = report_identities(cfg)
    echo_identity_report(rows)
    if bad_identity_rows(rows):
        raise SystemExit(1)

    repo_access_rows = report_identity_repo_access(cfg, project_path)
    if repo_access_rows:
        click.echo()
        echo_repo_access_report(repo_access_rows)
    if any(not row.ok for row in repo_access_rows):
        raise SystemExit(1)
