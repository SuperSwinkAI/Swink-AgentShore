"""Run modes for the orchestrator (agent / headless / solo / dashboard launch).

The module-level ``_logger`` is re-exported as ``agentshore.cli._logger`` so
``patch("agentshore.cli._logger", …)`` in legacy tests still affects the
logger used by ``_dispatch_command``.  All logger access inside this
module goes through ``agentshore.cli`` (the package) at call time so the
patch is observed.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import TYPE_CHECKING

import click
import structlog

from agentshore import cli as _cli_pkg
from agentshore.cli.constants import _SOCKET_POLL_INTERVAL_S, _SOCKET_WAIT_RETRIES
from agentshore.cli.helpers import (
    _install_loop_signal_handler,
    _track_background_task,
)
from agentshore.config.models import PolicyMode, RunMode
from agentshore.paths import project_archive_dir, project_db_path, project_reports_dir

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.config import RuntimeConfig
    from agentshore.core import Orchestrator

_logger = structlog.get_logger("agentshore.cli")


async def _dispatch_command(cmd: dict[str, object], orch: Orchestrator) -> None:
    """Dispatch a single IPC command dict to the orchestrator.

    Every validated command must have a handler here.  Commands without a full
    backend implementation return an explicit ``not_implemented`` log entry rather
    than silently doing nothing.
    """
    command = cmd.get("command")
    if command == "pause":
        await orch.pause("ipc_request")
    elif command == "resume":
        await orch.resume()
    elif command == "shutdown":
        await orch.stop()
    elif command == "drain":
        reason = str(cmd.get("reason", "user_request"))
        if cmd.get("end_session_report") is True:
            orch.request_end_session_report(open_browser=cmd.get("open_report") is not False)
        await orch.begin_drain(reason)
    elif command == "hard_stop":
        await orch.hard_stop()
    elif command == "adjust_budget":
        delta_raw = cmd.get("delta_usd", 0)
        try:
            delta = float(delta_raw if isinstance(delta_raw, (int, float, str)) else 0)
        except ValueError:
            _cli_pkg._logger.warning("ipc.adjust_budget_invalid", delta_usd=delta_raw)
            return
        if orch.adjust_budget(delta):
            await orch.resume()
    elif command == "rescan_issues":
        await orch._completion.refresh_issues()
    elif command == "feedback_response":
        action = cmd.get("action")
        if action == "continue":
            # Dashboard feedback modal Continue button: clear the
            # loop_detected / verification pause and let PPO pick the next
            # play. Was previously a no-op (logged "obsolete") so users would
            # click Continue and see no effect.
            await orch.resume()
        elif action == "pause":
            # Pause is the modal's default state once feedback fires; an
            # explicit Pause click is informational only.
            _cli_pkg._logger.info("ipc.feedback_response_pause_acknowledged")
        elif action in {"stop", "end_session", "drain"}:
            await orch.begin_drain("user_request")
        elif action == "rescan_issues":
            await orch._completion.refresh_issues()
            await orch.resume()
    elif command == "abort_play":
        # Cancel all in-flight play tasks.  The orchestrator loop will pick up
        # new work on the next iteration.
        _cli_pkg._logger.warning("ipc.abort_play_received", in_flight=list(orch._in_flight.keys()))
        for task in list(orch._in_flight.values()):
            task.cancel()
    elif command == "verification_response":
        # A human has responded to a verification_checkpoint.  If the checkpoint
        # passed, resume the paused orchestrator; otherwise keep it paused and log
        # the failure so the operator can decide what to do next.
        passed = cmd.get("passed")
        checkpoint_id = cmd.get("checkpoint_id")
        notes = cmd.get("notes")
        if passed:
            _cli_pkg._logger.info(
                "ipc.verification_response_passed",
                checkpoint_id=checkpoint_id,
                notes=notes,
            )
            await orch.resume()
        else:
            _cli_pkg._logger.warning(
                "ipc.verification_response_failed",
                checkpoint_id=checkpoint_id,
                notes=notes,
                message="Verification checkpoint failed; orchestrator remains paused",
            )
    elif command == "generate_report":
        report_type = str(cmd.get("report_type", "summary"))
        from agentshore.reports.generator import ReportGenerator

        gen = ReportGenerator(orch._store)
        output_dir = project_reports_dir(orch._repo_root)
        if report_type == "progress":
            await gen.generate_progress_report(orch._session_id, output_dir)
        else:
            await gen.generate_session_summary(orch._session_id, output_dir)
    elif command == "archive_session":
        from agentshore.archive import Archiver

        archive_dir = project_archive_dir(orch._repo_root)
        db_path = project_db_path(orch._repo_root)
        archiver = Archiver(orch._store, archive_dir)
        await archiver.create_archive(orch._session_id, db_path=db_path)
    elif command == "list_archives":
        archives = await orch._store.list_archives()
        _cli_pkg._logger.info("ipc.archives_listed", count=len(archives))
    # "start" is accepted by the validator (so connecting clients can send it)
    # but is a no-op at dispatch time — the orchestrator is already running by
    # the time IPC commands are processed.
    elif command == "start":
        _cli_pkg._logger.info("ipc.start_received_noop", message="Orchestrator already running")


def _launch_dashboard_background(
    *,
    project_path: Path,
    ipc_endpoint: object,
    session_id: str,
    seed: str | None,
    budget: float | None,
    policy_mode: PolicyMode,
    policy: str | None,
    strict: bool,
    config_path: str | None,
) -> None:
    """Launch AgentShore + dashboard as two detached background processes and return.

    1. Starts the orchestrator (agent mode) with stdout/stderr → session log.
    2. Waits up to 15 s for the Unix IPC socket to appear when using Unix IPC.
    3. Starts ``agentshore dashboard`` (the bridge) with stdout/stderr → dashboard log.
    4. Opens the browser once the bridge is up.
    5. Returns immediately — terminal is freed.
    """
    import subprocess  # nosec B404
    import time
    import webbrowser

    from agentshore.session_path import IpcEndpoint, session_dir

    endpoint = ipc_endpoint if isinstance(ipc_endpoint, IpcEndpoint) else IpcEndpoint.unix("")
    log_dir = session_dir(project_path)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "agentshore.log"

    cmd: list[str] = [
        sys.executable,
        "-m",
        "agentshore",
        "start",
        "--mode",
        RunMode.AGENT.value,
        "--project",
        str(project_path),
        "--session-id",
        session_id,
    ]
    if endpoint.kind == "unix":
        cmd.extend(["--socket", str(endpoint.path)])
    else:
        cmd.extend(["--ipc-host", endpoint.host, "--ipc-port", str(endpoint.port)])
    if budget is not None:
        cmd.extend(["--budget", str(budget)])
    else:
        cmd.append("--no-budget")
    if seed:
        cmd.extend(["--seed", seed])
    cmd.extend(["--policy-mode", policy_mode.value])
    if policy:
        cmd.extend(["--policy", policy])
    if strict:
        cmd.append("--strict")
    if config_path:
        cmd.extend(["--config", config_path])

    with log_file.open("w") as lf:
        subprocess.Popen(  # nosec B603
            cmd,
            stdout=lf,
            stderr=lf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    click.echo(f"AgentShore starting in background (log: {log_file})")

    # Sync-only wait: this runs pre-event-loop, before asyncio.run() is called.
    # time.sleep is correct here; using asyncio.sleep would require an event loop
    # that does not yet exist at this point in the process lifecycle.
    for _ in range(_SOCKET_WAIT_RETRIES):
        if endpoint.kind == "tcp":
            break
        if endpoint.path is not None and endpoint.path.exists():
            break
        time.sleep(_SOCKET_POLL_INTERVAL_S)
    else:
        click.echo("Warning: timed out waiting for IPC socket — check the log.", err=True)
        return

    port = _find_free_dashboard_port()
    dashboard_cmd: list[str] = [
        sys.executable,
        "-m",
        "agentshore",
        "dashboard",
        "--project",
        str(project_path),
        "--port",
        str(port),
        "--no-open",
    ]
    if endpoint.kind == "unix":
        dashboard_cmd.extend(["--socket", str(endpoint.path)])
    else:
        dashboard_cmd.extend(["--ipc-host", endpoint.host, "--ipc-port", str(endpoint.port)])
    dashboard_log = log_dir / "dashboard.log"
    with dashboard_log.open("w") as dl:
        dashboard_proc = subprocess.Popen(  # nosec B603
            dashboard_cmd,
            stdout=dl,
            stderr=dl,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    from agentshore.session_path import write_dashboard_pid

    write_dashboard_pid(project_path, dashboard_proc.pid)

    # Sync-only delay: give the dashboard bridge a moment to bind its port
    # before opening the browser.  This runs pre-event-loop (background launch
    # path), so time.sleep is the correct primitive here.
    time.sleep(1)
    url = f"http://localhost:{port}"
    webbrowser.open(url)
    click.echo(f"Dashboard: {url}")
    click.echo("Stop with: agentshore stop")


async def _run_agent_mode(
    *,
    cfg: RuntimeConfig,
    repo_root: Path,
    socket_path: str | None = None,
    ipc_endpoint: object | None = None,
    seed_path: Path | None = None,
    policy_path: Path | None = None,
    policy_mode: PolicyMode = PolicyMode.LEARNING,
    session_id: str | None = None,
    config_path: Path | None = None,
    open_dashboard: bool = False,
    dashboard_port: int | None = None,
) -> None:
    """Run the orchestrator in embedded agent (IPC) mode."""
    from agentshore.core import Orchestrator
    from agentshore.ipc import IpcServer, IpcStateProvider, StateWriter
    from agentshore.session_path import IpcEndpoint, session_dir

    endpoint = (
        ipc_endpoint
        if isinstance(ipc_endpoint, IpcEndpoint)
        else IpcEndpoint.unix(socket_path or "")
    )
    server = IpcServer(endpoint)
    await server.start()
    endpoint = server.endpoint

    # The StateWriter persists state snapshots + events into the session
    # directory for file-tailing consumers (next-gen dashboard sidecar).
    writer = StateWriter(session_dir(repo_root))
    provider = IpcStateProvider(writer, server=server)

    orch = await Orchestrator.bootstrap(
        cfg=cfg,
        repo_root=repo_root,
        seed_path=seed_path,
        policy_path=policy_path,
        policy_mode=policy_mode,
        state_provider=provider,
        session_id=session_id,
        config_path=config_path,
    )

    background_tasks: set[asyncio.Task[None]] = set()
    dashboard_task: asyncio.Task[None] | None = None
    if open_dashboard:
        dashboard_task = _track_background_task(
            background_tasks,
            _start_dashboard_bridge(
                ipc_endpoint=endpoint,
                session_dir=session_dir(repo_root),
                port=dashboard_port,
            ),
            name="dashboard_bridge",
        )

    loop = asyncio.get_running_loop()
    sigint_count = 0

    def _on_sigint() -> None:
        nonlocal sigint_count
        sigint_count += 1
        if sigint_count == 1:
            # Graceful drain: let in-flight plays finish, then end each agent.
            # Second signal escalates to hard stop (cancel all tasks).
            orch.request_drain("signal_sigterm")
        else:
            for t in asyncio.all_tasks():
                t.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        _install_loop_signal_handler(loop, sig, _on_sigint)

    if hasattr(signal, "SIGHUP"):
        _install_loop_signal_handler(
            loop,
            signal.SIGHUP,
            lambda: _track_background_task(
                background_tasks,
                orch.reload_config(),
                name="config_reload",
            ),
        )

    async def _drain_commands() -> None:
        while True:
            try:
                cmd = await server.command_queue.get()
            except asyncio.CancelledError:
                break
            try:
                await _dispatch_command(cmd, orch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _cli_pkg._logger.warning(
                    "ipc.dispatch_error", command=cmd.get("command"), error=str(exc)
                )

    cmd_task = asyncio.create_task(_drain_commands())

    try:
        async with orch:
            await orch.run_until_idle()
    finally:
        cmd_task.cancel()
        wait_tasks: set[asyncio.Task[None]] = {cmd_task}
        if dashboard_task is not None:
            dashboard_task.cancel()
            wait_tasks.add(dashboard_task)
        for task in list(background_tasks):
            task.cancel()
            wait_tasks.add(task)
        await asyncio.gather(*wait_tasks, return_exceptions=True)
        await server.stop()


def _find_free_dashboard_port(start: int = 9400, end: int = 9410) -> int:
    """Return the first free TCP port in [start, end), or start if all busy."""
    import socket as _socket

    for port in range(start, end):
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


async def _start_dashboard_bridge(
    socket_path: str | None = None,
    *,
    ipc_endpoint: object | None = None,
    session_dir: Path,
    port: int | None = None,
) -> None:
    """Start the dashboard bridge and open the browser once it's ready."""
    import webbrowser

    from agentshore.dashboard import DashboardBridge
    from agentshore.session_path import IpcEndpoint

    port = port or _find_free_dashboard_port()
    url = f"http://localhost:{port}"

    def _on_ready() -> None:
        click.echo(f"Dashboard ready → {url}")
        webbrowser.open(url)

    endpoint = ipc_endpoint if isinstance(ipc_endpoint, IpcEndpoint) else None
    bridge = DashboardBridge(
        socket_path=socket_path,
        ipc_endpoint=endpoint,
        session_dir=session_dir,
        port=port,
        on_ready=_on_ready,
    )
    await bridge.start()


async def _run_headless_mode(
    *,
    cfg: RuntimeConfig,
    repo_root: Path,
    seed_path: Path | None,
    policy_path: Path | None,
    policy_mode: PolicyMode,
    session_id: str | None = None,
    config_path: Path | None = None,
) -> None:
    """Run the orchestrator headless — no TUI, no IPC, just logs."""
    from agentshore.core import Orchestrator

    orch = await Orchestrator.bootstrap(
        cfg=cfg,
        repo_root=repo_root,
        seed_path=seed_path,
        policy_path=policy_path,
        policy_mode=policy_mode,
        session_id=session_id,
        config_path=config_path,
    )

    loop = asyncio.get_running_loop()
    background_tasks: set[asyncio.Task[None]] = set()
    sigint_count = 0

    def _on_sigint() -> None:
        nonlocal sigint_count
        sigint_count += 1
        if sigint_count == 1:
            # See _run_agent_mode for why this isn't a background task.
            orch.request_stop()
        else:
            for t in asyncio.all_tasks():
                t.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        _install_loop_signal_handler(loop, sig, _on_sigint)

    if hasattr(signal, "SIGHUP"):
        _install_loop_signal_handler(
            loop,
            signal.SIGHUP,
            lambda: _track_background_task(
                background_tasks,
                orch.reload_config(),
                name="config_reload",
            ),
        )

    try:
        async with orch:
            await orch.run_until_idle()
    finally:
        for task in list(background_tasks):
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)


def _run_solo_mode(
    *,
    cfg: RuntimeConfig,
    repo_root: Path,
    seed_path: Path | None,
    policy_path: Path | None,
    policy_mode: PolicyMode,
    session_id: str | None = None,
) -> None:
    """Run the orchestrator in local TUI mode."""
    from agentshore.ui import OrchestratorApp
    from agentshore.ui.app import AppWiring

    app = OrchestratorApp(
        wiring=AppWiring(
            cfg=cfg,
            repo_root=repo_root,
            seed_path=seed_path,
            policy_path=policy_path,
            policy_mode=policy_mode,
            session_id=session_id,
        )
    )
    app.run()
