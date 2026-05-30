"""``agentshore archive`` group and subcommands."""

from __future__ import annotations

from pathlib import Path

import click

from agentshore.cli_helpers import _PROJECT_DIR, _get_db_path
from agentshore.paths import GLOBAL_WEIGHTS_DIR as _GLOBAL_WEIGHTS_DIR


@click.group()
def archive() -> None:
    """Manage session archives."""


@archive.command("list")
@click.option(
    "--project",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    show_default=True,
)
def archive_list(project: str) -> None:
    """List archived sessions."""
    import asyncio

    db_path = _get_db_path(project)
    if not db_path.exists():
        click.echo(f"Error: No database found at {db_path}", err=True)
        raise SystemExit(1)

    async def _run() -> None:
        from agentshore.data.store import DataStore

        store = DataStore(db_path)
        await store.initialize()
        archives = await store.list_archives()
        if not archives:
            click.echo("No archived sessions.")
        else:
            click.echo(f"{'Date':<12} {'Session':<12} {'Plays':>6} {'Cost':>10} {'Alignment':>10}")
            click.echo("-" * 55)
            for a in archives:
                date = a.created_at[:10] if a.created_at else "?"
                click.echo(
                    f"{date:<12} {a.session_id[:10]:<12} {a.total_plays:>6} "
                    f"${a.total_cost:>8.2f} {a.final_alignment:>9.2f}"
                )
        await store.close()

    asyncio.run(_run())


@archive.command("create")
@click.option("--session", type=str, default=None, help="Session ID (default: last session)")
@click.option(
    "--project",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    show_default=True,
)
def archive_create(session: str | None, project: str) -> None:
    """Archive a completed session."""
    import asyncio

    project_path = Path(project).resolve()
    db_path = project_path / _PROJECT_DIR / "agentshore.db"
    if not db_path.exists():
        click.echo(f"Error: No database found at {db_path}", err=True)
        raise SystemExit(1)

    async def _run() -> None:
        from agentshore.archive import Archiver
        from agentshore.data.store import DataStore

        store = DataStore(db_path)
        await store.initialize()

        sess_id = session
        if sess_id is None:
            sessions = await store.list_sessions()
            if not sessions:
                click.echo("No sessions found.", err=True)
                await store.close()
                raise SystemExit(1)
            sess_id = sessions[0].session_id

        archive_dir = project_path / _PROJECT_DIR / "archives"
        archiver = Archiver(store, archive_dir)

        learnings_path = project_path / _PROJECT_DIR / "learnings.json"
        policy_dir = _GLOBAL_WEIGHTS_DIR
        policy_path = next(policy_dir.glob("*.pt"), None) if policy_dir.exists() else None

        path = await archiver.create_archive(
            sess_id,
            db_path=db_path,
            learnings_path=learnings_path if learnings_path.exists() else None,
            policy_path=policy_path,
        )
        click.echo(f"Session archived to: {path}")
        await store.close()

    asyncio.run(_run())


@archive.command("compare")
@click.argument("id1")
@click.argument("id2")
@click.option(
    "--project",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    show_default=True,
)
def archive_compare(id1: str, id2: str, project: str) -> None:
    """Compare two archived sessions."""
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
        gen = ReportGenerator(store)
        path = await gen.generate_comparison(id1, id2, output_dir)
        click.echo(f"Comparison report: {path}")
        await store.close()

    asyncio.run(_run())
