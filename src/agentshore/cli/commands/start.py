"""``agentshore start`` subcommand."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import click

from agentshore.budget import parse_duration
from agentshore.cli.caffeinate import maybe_re_exec_under_caffeinate
from agentshore.cli.constants import _START_MODE_AGENT, _START_MODE_TUI
from agentshore.cli.helpers import (
    _display_run_mode,
    _prepare_session_discovery_paths,
    _resolve_policy_mode_override,
    _resolve_start_run_mode,
)
from agentshore.cli.runtime import (
    _finalize_cli_timelapse,
    _launch_dashboard_background,
    _run_agent_mode,
    _run_headless_mode,
    _run_solo_mode,
)
from agentshore.config.models import PolicyMode, RunMode
from agentshore.session.bootstrap import (
    StartOptions,
    bootstrap_session,
    echo_bootstrap_summary,
    preflight_cli_agent_auth,
    preflight_identities,
    validate_budget_flag,
)


@click.command()
@click.option(
    "--seed",
    type=click.Path(),
    default=None,
    help=(
        "Path to seed material (file or directory) for the initial Seed Project play. "
        "Directories are recursively bundled from supported UTF-8 files with a 512 KiB cap."
    ),
)
@click.option(
    "--budget",
    type=float,
    default=None,
    help=(
        "Dollar soft cap for this session. AgentShore stops assigning new plays "
        "within $5 of the cap and lets in-flight agents finish, so final spend "
        "may land slightly above it. When omitted, agentshore.yaml is used "
        "(falling back to a $200 default only when the config defines no budget)."
    ),
)
@click.option(
    "--time",
    "time_budget",
    type=str,
    default=None,
    help=(
        "Wall-clock soft cap (e.g. '24h', '90m', or bare minutes), 1h–72h. "
        "AgentShore stops assigning new plays 20 minutes before the cap and "
        "lets in-flight agents finish. When omitted, agentshore.yaml is used."
    ),
)
@click.option(
    "--unlimited",
    "unlimited",
    is_flag=True,
    default=False,
    help="Disable both the dollar and time soft caps (run with no budget).",
)
@click.option(
    "--mode",
    type=click.Choice([_START_MODE_TUI, _START_MODE_AGENT]),
    default=_START_MODE_TUI,
    show_default=True,
    help="UI mode: 'tui' launches the Textual TUI, 'agent' uses IPC",
)
@click.option(
    "--tui",
    "tui",
    is_flag=True,
    help="Launch the Textual TUI (same as --mode tui)",
)
@click.option(
    "--socket",
    type=click.Path(),
    default=None,
    help="Unix socket path for IPC (default on macOS/Linux)",
)
@click.option(
    "--ipc-host",
    type=str,
    default="127.0.0.1",
    show_default=True,
    help="TCP IPC host (default transport on Windows)",
)
@click.option(
    "--ipc-port",
    type=int,
    default=0,
    show_default=True,
    help="TCP IPC port; 0 auto-selects a free port",
)
@click.option("--headless", is_flag=True, help="Run without TUI (logs only, no interactive UI)")
@click.option(
    "--dashboard",
    is_flag=True,
    help="Auto-open the browser dashboard (uses agent IPC mode)",
)
@click.option(
    "--policy-mode",
    type=click.Choice([mode.value for mode in PolicyMode]),
    default=None,
    help="Policy behavior: learning or audit-replay",
)
@click.option("--deterministic", "legacy_deterministic", is_flag=True, hidden=True)
@click.option(
    "--policy",
    type=click.Path(exists=True, file_okay=True, dir_okay=False),
    default=None,
    help="Path to a saved policy checkpoint (.pt)",
)
@click.option(
    "--strict/--no-strict",
    "strict",
    default=None,
    help=(
        "Enable/disable scope.strict_mode (stricter scope-drift logging). "
        "When omitted, the value from agentshore.yaml is used."
    ),
)
@click.option(
    "--project",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    help="Project root directory",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, file_okay=True, dir_okay=False),
    default=None,
    help="Path to agentshore.yaml (overrides auto-detected location)",
)
@click.option(
    "--skip-auth-preflight",
    "skip_auth_preflight",
    is_flag=True,
    default=False,
    help=(
        "Skip the CLI-agent backend auth preflight probe (for offline/CI/"
        "air-gapped runs where the model-provider session can't be reached)."
    ),
)
@click.option("--session-id", hidden=True, default=None)
def start(
    seed: str | None,
    budget: float | None,
    time_budget: str | None,
    unlimited: bool,
    mode: str,
    tui: bool,
    socket: str | None,
    ipc_host: str,
    ipc_port: int,
    headless: bool,
    dashboard: bool,
    policy_mode: str | None,
    legacy_deterministic: bool,
    policy: str | None,
    strict: bool | None,
    project: str,
    config_path: str | None,
    skip_auth_preflight: bool,
    session_id: str | None,
) -> None:
    """Start an AgentShore session.

    Detects available agents, loads config, and launches the orchestrator in
    either TUI or agent (IPC) mode.

    Examples:

      agentshore start --seed spec.md --budget 40.00

      agentshore start --tui

      agentshore start --mode agent --socket /tmp/agentshore.sock

      agentshore start --mode agent --ipc-port 9500

      agentshore start --mode agent --dashboard
    """
    # On macOS, re-exec under `caffeinate -i` so the OS can't defer the
    # sidecar's SQLite fsyncs while the screen is locked (desktop-tvsb /
    # desktop-n7ci). No-op on Linux/Windows and inside pytest.
    maybe_re_exec_under_caffeinate()

    # Parse: validate flag-level options the bootstrap needs. The caps are
    # resolved against the loaded config inside bootstrap (so omitted flags can
    # defer to agentshore.yaml); validate the explicit values here so a bad flag
    # surfaces as a usage error before any work happens.
    if unlimited and (budget is not None or time_budget is not None):
        raise click.BadParameter("--unlimited cannot be combined with --budget or --time.")
    validate_budget_flag(budget)
    if time_budget is not None:
        try:
            time_override: int | None = parse_duration(time_budget)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--time") from exc
    else:
        time_override = None
    policy_mode_override = _resolve_policy_mode_override(
        policy_mode=policy_mode,
        legacy_deterministic=legacy_deterministic,
    )
    run_mode = _resolve_start_run_mode(
        mode,
        tui=tui,
        dashboard=dashboard,
        headless=headless,
    )

    # Bootstrap: resolve budget/socket/config/detection into a ResolvedSession.
    resolved = bootstrap_session(
        StartOptions(
            project_path=Path(project).resolve(),
            run_session_id=session_id or str(uuid.uuid4()),
            seed=seed,
            budget_override=budget,
            time_override=time_override,
            unlimited=unlimited,
            policy_mode_override=policy_mode_override,
            run_mode=run_mode,
            socket=socket,
            ipc_host=ipc_host,
            ipc_port=ipc_port,
            strict=strict,
            config_path=config_path,
        )
    )

    # Summary + identity preflight (echoes, validates, exits 1 on failure).
    echo_bootstrap_summary(resolved)
    preflight_identities(resolved.cfg, resolved.repo_root)
    if not skip_auth_preflight:
        preflight_cli_agent_auth(resolved.cfg)

    # -- Dispatch: run the orchestrator -----------------------------------------
    from agentshore.session_path import (
        cleanup_session,
        stop_dashboard_process,
        write_pid,
        write_session_info,
    )

    cfg = resolved.cfg
    _cfg_path = resolved.cfg_path
    repo_root = resolved.repo_root
    run_session_id = resolved.run_session_id
    effective_policy_mode = resolved.effective_policy_mode
    ipc_endpoint = resolved.ipc_endpoint
    well_known_socket = resolved.well_known_socket
    socket = resolved.resolved_socket
    project_path = resolved.project_path
    seed_path_obj = resolved.seed_path
    policy_path_obj = Path(policy) if policy else None

    if dashboard:
        _prepare_session_discovery_paths(
            well_known_socket=well_known_socket,
            ipc_endpoint=ipc_endpoint,
            allow_metadata_failure=ipc_endpoint.kind == "tcp",
        )
        _launch_dashboard_background(
            project_path=project_path,
            ipc_endpoint=ipc_endpoint,
            session_id=run_session_id,
            seed=seed,
            budget_cfg=cfg.budget,
            policy_mode=effective_policy_mode,
            policy=policy,
            strict=strict,
            config_path=str(_cfg_path) if _cfg_path else None,
            timelapse_enabled=cfg.timelapse.enabled,
        )
        return

    metadata_available = _prepare_session_discovery_paths(
        well_known_socket=well_known_socket,
        ipc_endpoint=ipc_endpoint,
        allow_metadata_failure=ipc_endpoint.kind == "tcp",
    )
    if metadata_available:
        try:
            write_pid(project_path)
            write_session_info(
                project_path,
                socket_path=socket,
                ipc_endpoint=ipc_endpoint,
                extra={
                    "mode": _display_run_mode(run_mode),
                    "headless": headless,
                    "session_id": run_session_id,
                    "project_key": well_known_socket.parent.name,
                },
            )
        except OSError as exc:
            click.echo(f"Warning: session metadata unavailable: {exc}", err=True)
    try:
        if run_mode == RunMode.AGENT:
            asyncio.run(
                _run_agent_mode(
                    cfg=cfg,
                    repo_root=repo_root,
                    socket_path=socket,
                    ipc_endpoint=ipc_endpoint,
                    seed_path=seed_path_obj,
                    policy_path=policy_path_obj,
                    policy_mode=effective_policy_mode,
                    session_id=run_session_id,
                    config_path=_cfg_path,
                    open_dashboard=False,
                )
            )
        elif headless:
            asyncio.run(
                _run_headless_mode(
                    cfg=cfg,
                    repo_root=repo_root,
                    seed_path=seed_path_obj,
                    policy_path=policy_path_obj,
                    policy_mode=effective_policy_mode,
                    session_id=run_session_id,
                    config_path=_cfg_path,
                )
            )
        else:
            _run_solo_mode(
                cfg=cfg,
                repo_root=repo_root,
                seed_path=seed_path_obj,
                policy_path=policy_path_obj,
                policy_mode=effective_policy_mode,
                session_id=run_session_id,
            )
    finally:
        # Finalise any CLI-started dashboard timelapse before cleanup removes the
        # sidecar — covers a natural session end and a graceful drain. No-op when
        # no capture was started (e.g. headless/solo).
        _finalize_cli_timelapse(project_path)
        stop_dashboard_process(project_path)
        cleanup_session(project_path)
