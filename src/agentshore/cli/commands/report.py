"""``agentshore report`` subcommand."""

from __future__ import annotations

from pathlib import Path

import click

from agentshore.cli.helpers import open_store, resolve_session_id
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
    output_dir = project_path / _PROJECT_DIR / "reports"

    async def _run() -> None:
        from agentshore.reports.generator import ReportGenerator

        async with open_store(db_path) as store:
            sess_id = await resolve_session_id(store, session)
            gen = ReportGenerator(store)
            if report_type == "summary":
                path = await gen.generate_session_summary(
                    sess_id, output_dir, open_browser=open_report
                )
            else:
                path = await gen.generate_progress_report(sess_id, output_dir)
            click.echo(f"Report saved to: {path}")

    asyncio.run(_run())
