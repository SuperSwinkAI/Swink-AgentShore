"""``agentshore dashboard`` subcommand."""

from __future__ import annotations

from pathlib import Path

import click


@click.command()
@click.option(
    "--socket",
    type=click.Path(),
    default=None,
    help="IPC socket path (auto-discovered from project directory if omitted)",
)
@click.option(
    "--ipc-host",
    type=str,
    default=None,
    help="TCP IPC host (used with --ipc-port)",
)
@click.option(
    "--ipc-port",
    type=int,
    default=None,
    help="TCP IPC port",
)
@click.option(
    "--port",
    type=int,
    default=9400,
    show_default=True,
    help="HTTP/WebSocket port for the dashboard",
)
@click.option(
    "--no-open",
    is_flag=True,
    help="Don't auto-open the browser",
)
@click.option(
    "--project",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    help="Project root directory (used for socket auto-discovery)",
)
def dashboard(
    socket: str | None,
    ipc_host: str | None,
    ipc_port: int | None,
    port: int,
    no_open: bool,
    project: str,
) -> None:
    """Open the pixel-art dashboard for a running AgentShore session.

    Auto-discovers the IPC endpoint for the current project directory.
    Use --socket to override with an explicit Unix socket path, or
    --ipc-host/--ipc-port for TCP.

    Examples:

      agentshore dashboard

      agentshore dashboard --socket /tmp/agentshore.sock --port 8080
    """
    import asyncio
    import os

    from agentshore.session_path import (
        IpcEndpoint,
        discover_ipc_endpoint,
        session_dir,
        write_dashboard_pid,
    )

    project_path = Path(project).resolve()

    if socket is not None:
        ipc_endpoint = IpcEndpoint.unix(Path(socket))
    elif ipc_port is not None:
        ipc_endpoint = IpcEndpoint.tcp(ipc_host or "127.0.0.1", ipc_port)
    else:
        discovered = discover_ipc_endpoint(project_path)
        if discovered is None:
            click.echo(
                "Error: No running AgentShore session found for this project.\n"
                "Start one with: agentshore start --mode agent\n\n"
                "Or specify an IPC endpoint: agentshore dashboard --socket <path> "
                "or --ipc-host <host> --ipc-port <port>",
                err=True,
            )
            raise SystemExit(1)
        ipc_endpoint = discovered
        click.echo(f"Discovered session IPC: {ipc_endpoint.label}")

    if ipc_endpoint.kind == "unix" and (
        ipc_endpoint.path is None or not ipc_endpoint.path.exists()
    ):
        click.echo(
            f"Error: Socket not found at {ipc_endpoint.path}\nIs an AgentShore session running?",
            err=True,
        )
        raise SystemExit(1)

    # Record this process's PID so a stale dashboard.pid from an earlier
    # supervisor-launched sidecar can be distinguished from a manual restart.
    write_dashboard_pid(project_path, os.getpid())

    from agentshore.dashboard import DashboardBridge

    async def _run() -> None:
        url = f"http://localhost:{port}"

        def _on_ready() -> None:
            click.echo(f"Dashboard ready → {url}")
            if not no_open:
                import webbrowser

                webbrowser.open(url)

        bridge = DashboardBridge(
            ipc_endpoint=ipc_endpoint,
            session_dir=session_dir(project_path),
            port=port,
            on_ready=_on_ready,
        )
        await bridge.start()

    asyncio.run(_run())
