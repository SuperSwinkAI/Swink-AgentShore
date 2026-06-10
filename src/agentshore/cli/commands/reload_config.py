"""``agentshore reload-config`` subcommand."""

from __future__ import annotations

from pathlib import Path

import click


@click.command("reload-config")
@click.option(
    "--project",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    help="Project root directory",
)
def reload_config_cmd(project: str) -> None:
    """Hot-reload agentshore.yaml for a running session.

    Sends a reload_config IPC command to the orchestrator, which atomically
    re-reads and applies the updated configuration.  This is the cross-platform
    equivalent of ``kill -HUP`` for Windows users where SIGHUP does not exist.
    """
    from agentshore.session_path import is_session_running, request_reload_config

    project_path = Path(project).resolve()

    if not is_session_running(project_path):
        click.echo("No running AgentShore session found for this project.")
        raise SystemExit(0)

    result = request_reload_config(project_path)
    if result == "sent":
        click.echo("Config reload requested.")
    elif result == "fallback_hard":
        click.echo("No IPC endpoint found — is the session running?", err=True)
        raise SystemExit(1)
    elif result in ("error", "timeout"):
        click.echo(f"Error: Failed to send reload_config command ({result}).", err=True)
        raise SystemExit(1)
    else:
        click.echo(f"Error: Unexpected result: {result!r}", err=True)
        raise SystemExit(1)
