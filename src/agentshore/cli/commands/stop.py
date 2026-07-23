"""``agentshore stop`` subcommand."""

from __future__ import annotations

from pathlib import Path

import click

from agentshore.cli.constants import _DRAIN_WAIT_POLL_INTERVAL_S
from agentshore.cli.helpers import open_store, resolve_session_id
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
def stop(project: str, hard: bool) -> None:
    """Stop a running AgentShore session for this project.

    By default, triggers a graceful drain: in-flight plays finish, agents are
    ended one-by-one, then the session shuts down cleanly.

    Use --hard to request immediate platform-specific process-tree termination.
    """
    from agentshore.session_path import read_timelapse_info
    from agentshore.session_process import hard_stop_session, is_session_running, request_drain

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
            click.echo("Drain requested — waiting for in-flight plays to finish…")
            click.echo("(Press Ctrl+C to force-stop now.)")
            outcome = _wait_for_session_exit(project_path)
            if outcome is None:
                # Escalated to hard stop but the process is still alive (#31) —
                # don't claim a clean stop.
                click.echo(
                    "Error: AgentShore session is still running after hard stop.",
                    err=True,
                )
                raise SystemExit(1)
            click.echo("AgentShore session stopped.")
            if outcome is False:
                click.echo("End session report skipped because the session did not stop cleanly.")
        elif result == "fallback_hard":
            click.echo("No IPC endpoint found — falling back to hard stop.")
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


def _wait_for_session_exit(project_path: Path) -> bool | None:
    """Poll until the orchestrator PID is gone.

    Waits indefinitely so in-flight plays finish on their own — there is no
    automatic hard-stop deadline. The user escalates explicitly with Ctrl+C (or
    by re-running ``agentshore stop --hard``). Returns ``True`` for a clean drain
    (process exited on its own), ``False`` when an escalated hard stop succeeded,
    and ``None`` when that hard stop ran but the process is still alive (#31).
    """
    import time

    from agentshore.session_path import read_pid
    from agentshore.session_process import _process_alive, hard_stop_session

    pid = read_pid(project_path)
    if pid is None:
        return True

    # NB: probe via _process_alive, never a bare ``os.kill(pid, 0)`` — on Windows
    # signal 0 is CTRL_C_EVENT, so that "probe" delivers a Ctrl+C and its result
    # reflects console-group membership, not liveness (see _process_alive_windows).
    interrupted = False
    try:
        while _process_alive(pid):
            time.sleep(_DRAIN_WAIT_POLL_INTERVAL_S)
        return True
    except KeyboardInterrupt:
        interrupted = True

    if interrupted:
        click.echo("Force-stop requested; escalating to hard stop...")
    if not hard_stop_session(project_path):
        click.echo("Error: hard stop failed.", err=True)
        return None
    return False
