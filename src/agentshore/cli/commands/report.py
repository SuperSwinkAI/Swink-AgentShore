"""``agentshore report`` subcommand."""

from __future__ import annotations

from pathlib import Path

import click

from agentshore.cli_helpers import _PROJECT_DIR


@click.command()
@click.option("--session", type=str, default=None, help="Session ID (default: last session)")
@click.option(
    "--type",
    "report_type",
    type=click.Choice(["summary", "progress"]),
    default="summary",
)
@click.option("--open", "open_report", is_flag=True, help="Open in default browser")
@click.option(
    "--project",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    show_default=True,
)
def report(session: str | None, report_type: str, open_report: bool, project: str) -> None:
    """Generate a session report."""
    import asyncio

    project_path = Path(project).resolve()
    db_path = project_path / _PROJECT_DIR / "agentshore.db"
    if not db_path.exists():
        click.echo(f"Error: No database found at {db_path}", err=True)
        raise SystemExit(1)

    output_dir = project_path / _PROJECT_DIR / "reports"

    async def _run() -> None:
        from agentshore.data.store import DataStore
        from agentshore.reports.generator import ReportGenerator

        store = DataStore(db_path)
        await store.initialize()

        # Resolve session ID
        sess_id = session
        if sess_id is None:
            sessions = await store.list_sessions()
            if not sessions:
                click.echo("No sessions found.", err=True)
                await store.close()
                raise SystemExit(1)
            sess_id = sessions[0].session_id

        gen = ReportGenerator(store)
        if report_type == "summary":
            path = await gen.generate_session_summary(sess_id, output_dir, open_browser=open_report)
        else:
            path = await gen.generate_progress_report(sess_id, output_dir)

        click.echo(f"Report saved to: {path}")
        await store.close()

    asyncio.run(_run())
