"""``agentshore stop`` subcommand."""

from __future__ import annotations

from pathlib import Path

import click

from agentshore.cli.constants import (
    _DRAIN_WAIT_POLL_INTERVAL_S,
    _DRAIN_WAIT_RETRIES,
)
from agentshore.cli.helpers import _drain_wait_timeout_label, open_store, resolve_session_id
from agentshore.cli_helpers import _PROJECT_DIR


def _generate_end_session_report_cli(project_path: Path) -> Path:
    """Generate and open the latest session's ESR from a completed project DB."""
    import asyncio

    db_path = project_path / _PROJECT_DIR / "agentshore.db"

    async def _run() -> Path:
        from agentshore.reports.generator import ReportGenerator

        async with open_store(db_path) as store:
            sess_id = await resolve_session_id(store, None)
            generator = ReportGenerator(store)
            return await generator.generate_end_session_report(
                sess_id,
                project_path / _PROJECT_DIR / "reports",
                open_browser=False,
            )

    return asyncio.run(_run())


@click.command()
@click.option(
    "--project",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    help="Project root directory",
)
@click.option(
    "--hard",
    is_flag=True,
    default=False,
    help="Force immediate shutdown. Default: graceful drain.",
)
@click.option(
    "--esr",
    is_flag=True,
    default=False,
    help="Deprecated; graceful stops always generate and open an end-of-session report.",
)
def stop(project: str, hard: bool, esr: bool) -> None:
    """Stop a running AgentShore session for this project.

    By default, triggers a graceful drain: in-flight plays finish, agents are
    ended one-by-one, then the session shuts down cleanly.

    Use --hard to request immediate platform-specific process-tree termination.
    """
    from agentshore.session_path import (
        hard_stop_session,
        is_session_running,
        read_timelapse_info,
        request_drain,
    )

    project_path = Path(project).resolve()

    if not is_session_running(project_path):
        click.echo("No running AgentShore session found for this project.")
        raise SystemExit(0)

    # Capture the timelapse handle before stopping: a graceful drain lets the
    # orchestrator finalise and clear the sidecar on its way out, so read it now
    # and finalise as a backstop (mainly for --hard, where the orchestrator is
    # killed before it can run its own shutdown).
    timelapse_info = read_timelapse_info(project_path)

    if hard:
        if esr:
            click.echo("Ignoring --esr with --hard; hard stop can bypass clean shutdown.")
        if hard_stop_session(project_path):
            click.echo("AgentShore session force-stopped.")
        else:
            click.echo("Error: Failed to stop AgentShore session.", err=True)
            raise SystemExit(1)
    else:
        result = request_drain(
            project_path,
            end_session_report=True,
            open_report=True,
        )
        if result == "sent":
            click.echo("Drain requested — waiting for session to finish in-flight plays…")
            click.echo(
                f"(Press Ctrl+C to force-stop sooner; "
                f"auto hard stop after {_drain_wait_timeout_label()})"
            )
            clean_exit = _wait_for_session_exit(project_path)
            click.echo("AgentShore session stopped.")
            if not clean_exit:
                click.echo("End session report skipped because the session did not stop cleanly.")
        elif result == "fallback_hard":
            click.echo("No IPC endpoint found — falling back to hard stop.")
            if esr:
                click.echo("End session report skipped because hard stop was required.")
            if hard_stop_session(project_path):
                click.echo("AgentShore session stopped.")
            else:
                click.echo("Error: Failed to stop AgentShore session.", err=True)
                raise SystemExit(1)
        elif result in ("error", "timeout"):
            click.echo("Error: Failed to request drain.", err=True)
            raise SystemExit(1)
        else:
            click.echo("Error: Failed to request drain.", err=True)
            raise SystemExit(1)

    if timelapse_info is not None:
        from agentshore.cli.runtime import _finalize_cli_timelapse

        _finalize_cli_timelapse(project_path, info=timelapse_info, echo=True)


def _wait_for_session_exit(project_path: Path) -> bool:
    """Poll until the orchestrator PID is gone. Return False if escalated."""
    import os
    import time

    from agentshore.session_path import hard_stop_session, read_pid

    pid = read_pid(project_path)
    if pid is None:
        return True

    polls = 0
    interrupted = False
    try:
        while polls < _DRAIN_WAIT_RETRIES:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                click.echo(
                    "Warning: cannot check PID ownership; treating as still running.",
                    err=True,
                )
                break
            time.sleep(_DRAIN_WAIT_POLL_INTERVAL_S)
            polls += 1
    except KeyboardInterrupt:
        interrupted = True

    if interrupted:
        click.echo("Force-stop requested; escalating to hard stop...")
    else:
        click.echo(
            f"Session still running after {_drain_wait_timeout_label()}; escalating to hard stop..."
        )
    if not hard_stop_session(project_path):
        click.echo("Error: hard stop failed.", err=True)
    return False
