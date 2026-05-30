"""``agentshore start`` subcommand.

The helpers used here (``_find_repo_root``, ``_detect_agents``, …) are
intentionally looked up through ``agentshore.cli`` (the package namespace) at
call time rather than imported as local names.  This preserves the legacy
``patch("agentshore.cli._find_repo_root", …)`` test contract from before the
CLI was split into a package: tests patch attributes on the ``agentshore.cli``
module, and ``start()`` resolves them at call time so the patch is seen.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from agentshore import cli as _cli_pkg
from agentshore.budget import MIN_ENABLED_BUDGET_USD
from agentshore.cli.caffeinate import maybe_re_exec_under_caffeinate
from agentshore.cli.constants import _START_MODE_AGENT, _START_MODE_TUI
from agentshore.cli_helpers import _DEFAULT_BUDGET, _PROJECT_DIR
from agentshore.config.models import PolicyMode, RunMode
from agentshore.errors import OrchestratorError


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
    default=_DEFAULT_BUDGET,
    show_default=True,
    help="Total dollar budget for this session",
)
@click.option(
    "--no-budget",
    "no_budget",
    is_flag=True,
    default=False,
    help="Disable budget enforcement (run without a spending cap)",
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
    "--strict",
    is_flag=True,
    help="Enable scope.strict_mode (stricter scope-drift logging)",
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
@click.option("--session-id", hidden=True, default=None)
def start(
    seed: str | None,
    budget: float,
    no_budget: bool,
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
    strict: bool,
    project: str,
    config_path: str | None,
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

    # Resolve effective budget: --no-budget wins over --budget value
    effective_budget: float | None = None if no_budget else budget
    if effective_budget is not None and effective_budget <= 0:
        raise click.BadParameter("Budget must be positive. Use --no-budget to disable budgeting.")
    if effective_budget is not None and effective_budget < MIN_ENABLED_BUDGET_USD:
        raise click.BadParameter(
            f"Budget must be at least ${MIN_ENABLED_BUDGET_USD:.2f}. "
            "Use --no-budget to disable budgeting."
        )
    policy_mode_override = _cli_pkg._resolve_policy_mode_override(
        policy_mode=policy_mode,
        legacy_deterministic=legacy_deterministic,
    )

    project_path = Path(project).resolve()
    run_session_id = session_id or str(_cli_pkg.uuid.uuid4())

    # -- 0. Resolve public mode flags ----------------------------------------
    run_mode = _cli_pkg._resolve_start_run_mode(
        mode,
        tui=tui,
        dashboard=dashboard,
        headless=headless,
    )

    seed_path: Path | None = None
    seed_kind: str | None = None

    # -- 2. Resolve socket path (auto-discover or explicit) ---------------
    from agentshore.session_path import (
        IpcEndpoint,
        cleanup_session,
        default_ipc_endpoint,
        find_free_tcp_port,
        is_session_running,
        session_socket_path,
        stop_dashboard_process,
        write_pid,
        write_session_info,
    )

    if is_session_running(project_path):
        click.echo(
            "Error: An AgentShore session is already running for this project.\n"
            "Stop it with: agentshore stop",
            err=True,
        )
        raise SystemExit(1)

    # Default socket location: well-known per-project path under
    # ~/.config/swink/agentshore/sessions/<hash>/.  When --socket overrides the default, also
    # register the override via info.json (and a symlink at the well-known
    # path) so external tools can still discover it by hashing the project.
    well_known_socket = session_socket_path(project_path)
    if socket is None:
        ipc_endpoint = default_ipc_endpoint(project_path, host=ipc_host, port=ipc_port)
        if ipc_endpoint.kind == "tcp" and ipc_endpoint.port == 0:
            ipc_endpoint = IpcEndpoint.tcp(ipc_endpoint.host, find_free_tcp_port(ipc_endpoint.host))
        resolved_socket: str = str(well_known_socket)
    else:
        resolved_socket = socket
        ipc_endpoint = IpcEndpoint.unix(resolved_socket)
        explicit = Path(resolved_socket)
        # Best-effort symlink at the well-known path so `agentshore dashboard`
        # auto-discovery (which hashes the project dir) works regardless of
        # whether the user customised --socket.  If symlinks aren't supported
        # on this filesystem, fall back to recording the path in info.json
        # only — discover_socket() reads the sidecar before falling back to
        # the well-known location.
        # Skip when --socket already points at the well-known path: the
        # backgrounded dashboard launcher re-passes the resolved path to the
        # child, so symlinking would create socket.sock -> socket.sock and
        # bind() would later fail with ELOOP.
        if explicit.resolve() != well_known_socket.resolve():
            try:
                if well_known_socket.exists() or well_known_socket.is_symlink():
                    well_known_socket.unlink()
                well_known_socket.symlink_to(explicit.resolve())
            except OSError:
                pass
    socket = resolved_socket

    # -- 3. Detect git repo root ----------------------------------------
    try:
        repo_root = _cli_pkg._find_repo_root(project_path)
    except OrchestratorError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc

    # -- 3a. Resolve --seed as file or directory -------------------------
    if seed is not None:
        seed_path, seed_kind = _cli_pkg._resolve_seed_input_path(seed, repo_root)

    # -- 4. Detect GitHub remote ----------------------------------------
    try:
        gh_info = _cli_pkg._detect_gh_remote(repo_root)
    except OrchestratorError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc

    # -- 5. Detect agents on PATH ---------------------------------------
    agents = _cli_pkg._detect_agents()

    # -- 6. Detect API keys ---------------------------------------------
    api_keys = _cli_pkg._detect_api_keys()

    # Hard-fail only if NEITHER a CLI agent is present NOR any API key is set
    if not agents and not api_keys:
        click.echo(
            "Error: No coding agents found.\n\n"
            "AgentShore needs at least one agent. Options:\n"
            "  1. Install Claude Code:  npm install -g @anthropic-ai/claude-code\n"
            "  2. Install Codex CLI:    pip install codex-cli\n"
            "  3. Install Gemini CLI:   npm install -g @google/gemini-cli\n"
            "  4. Set an API key:       export ANTHROPIC_API_KEY=sk-ant-...\n"
            "                           export OPENAI_API_KEY=sk-...",
            err=True,
        )
        raise SystemExit(1)

    # -- 7. Create .agentshore/ directory -----------------------------------
    agentshore_dir = repo_root / _PROJECT_DIR
    agentshore_dir.mkdir(exist_ok=True)

    # -- 8. Ensure agentshore.yaml exists ----------------------------------
    default_config_path = repo_root / "agentshore.yaml"
    if not default_config_path.exists():
        name_with_owner = gh_info.get("nameWithOwner", "owner/repo")
        config_text = _cli_pkg._generate_default_config(
            name_with_owner, agents, effective_budget, strict
        )
        default_config_path.write_text(config_text)
        click.echo(f"Generated {default_config_path}")

    # -- 9. Load config and apply CLI overrides --------------------------------
    from agentshore.config import load_config
    from agentshore.errors import ConfigError

    _cfg_path = Path(config_path) if config_path else repo_root / "agentshore.yaml"
    try:
        cfg = load_config(_cfg_path)
    except (ConfigError, OSError, ValueError) as exc:
        # Attempt to give a helpful YAML-specific message.
        # The problem_mark may be on the exception itself (yaml.YAMLError) or
        # on its __cause__ (when ConfigError wraps a YAML parse error).
        mark = getattr(exc, "problem_mark", None) or getattr(
            getattr(exc, "__cause__", None), "problem_mark", None
        )
        line_info = f" (line {mark.line + 1})" if mark is not None else ""
        if line_info:
            click.echo(
                f"Error: Invalid YAML in {_cfg_path}{line_info}\n"
                f"  {exc}\n\n"
                "Hint: Run 'agentshore init --force' to regenerate the config.",
                err=True,
            )
            raise SystemExit(1) from exc
        cfg = load_config(None)
        click.echo(f"Warning: config load failed ({exc}), using defaults.", err=True)

    # Apply CLI overrides to a fresh config with replaced subfields
    import dataclasses

    effective_policy_mode = policy_mode_override or cfg.rl.policy_mode
    cfg = dataclasses.replace(
        cfg,
        budget=(
            dataclasses.replace(cfg.budget, enabled=True, total=effective_budget)
            if effective_budget is not None
            else dataclasses.replace(cfg.budget, enabled=False)
        ),
        rl=dataclasses.replace(cfg.rl, policy_mode=effective_policy_mode),
        scope=dataclasses.replace(cfg.scope, strict_mode=strict),
    )

    # -- 10. Print bootstrap summary -------------------------------------
    click.echo("=" * 60)
    click.echo("  AgentShore — bootstrap summary")
    click.echo("=" * 60)
    click.echo(f"  Session ID     : {run_session_id}")
    click.echo(f"  Repo root      : {repo_root}")
    click.echo(f"  GitHub remote  : {gh_info.get('url', '(unknown)')}")
    click.echo(f"  Agents on PATH : {', '.join(agents)}")
    if api_keys:
        click.echo(f"  API keys       : {', '.join(api_keys)}")
    else:
        click.echo("  API keys       : (none detected)")
    click.echo(f"  Mode           : {_cli_pkg._display_run_mode(run_mode)}")
    budget_display = "disabled" if effective_budget is None else f"${effective_budget:.2f}"
    click.echo(f"  Budget         : {budget_display}")
    click.echo(f"  Policy mode    : {effective_policy_mode.summary_label}")
    if seed_path is not None:
        click.echo(f"  Seed input     : {seed_path} ({seed_kind})")
    click.echo(f"  Project key    : {well_known_socket.parent.name} (stable path hash)")
    if ipc_endpoint.kind == "unix":
        click.echo(f"  Socket         : {ipc_endpoint.path}")
    else:
        click.echo(f"  IPC            : tcp://{ipc_endpoint.host}:{ipc_endpoint.port}")
    click.echo("=" * 60)
    click.echo()

    # -- 11. Identity resolution banner ------------------------------------------
    if cfg.identities or any(a.identity for a in cfg.agents.values()):
        from agentshore.agents.identity import report_identities, report_identity_repo_access
        from agentshore.cli_identity import echo_identity_report

        click.echo("=" * 60)
        identity_rows = report_identities(cfg)
        echo_identity_report(identity_rows)
        invalid = [
            r
            for r in identity_rows
            if r.identity_name is not None
            and r.token_source not in {"ambient", "none"}
            and not r.token_valid
        ]
        if invalid:
            click.echo("=" * 60)
            click.echo()
            details = ", ".join(f"{r.agent_key}:{r.detail}" for r in invalid)
            click.echo(
                f"Error: configured GitHub identity token failed validation: {details}",
                err=True,
            )
            raise SystemExit(1)

        repo_access_rows = report_identity_repo_access(cfg, repo_root)
        _cli_pkg._echo_repo_access_rows(repo_access_rows)
        click.echo("=" * 60)
        click.echo()

        # desktop-l7i: warn if no SSH key is loaded in the agent. Identity-
        # configured runs use SSH-signed commits via `git merge --no-ff`; an
        # empty ssh-agent means merge_pr plays will fail mid-session with
        # 'ssh-signing-key-not-loaded' (observed as 3 failures + 1 false-positive
        # loop_detected).
        ssh_loaded, ssh_detail = _cli_pkg._check_ssh_signing_key_loaded()
        if ssh_loaded:
            click.echo(f"SSH signing key: ok ({ssh_detail})")
        else:
            click.echo("⚠ SSH signing key: NOT LOADED")
            click.echo(f"  Detail: {ssh_detail}")
            click.echo(
                "  Fix: run `ssh-add --apple-use-keychain ~/.ssh/id_ed25519` "
                "in your terminal before starting AgentShore."
            )
            click.echo(
                "  Without it, merge_pr plays will fail with "
                "'ssh-signing-key-not-loaded' and PPO will trip loop_detected "
                "(see desktop-l7i)."
            )
        click.echo()

        blocked_repo_access = [r for r in repo_access_rows if not r.ok]
        if blocked_repo_access:
            details = ", ".join(
                f"{r.agent_key}:{' '.join(r.detail.split())}" for r in blocked_repo_access
            )
            click.echo(
                f"Error: configured GitHub identity token cannot access this repository: {details}",
                err=True,
            )
            raise SystemExit(1)

    # -- 11a. Precondition: ≥2 distinct GH identities for code review -----------
    # Code review requires the reviewer's GH login to differ from the PR
    # author's; a single-identity session can never approve any PR. Fail
    # fast here rather than burning plays + PPO penalties at runtime.
    from agentshore.agents.identity import require_two_distinct_gh_identities

    try:
        require_two_distinct_gh_identities(cfg)
    except ConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc

    # -- 12. Run the orchestrator -----------------------------------------------
    seed_path_obj = seed_path
    policy_path_obj = Path(policy) if policy else None

    if dashboard:
        _cli_pkg._prepare_session_discovery_paths(
            well_known_socket=well_known_socket,
            ipc_endpoint=ipc_endpoint,
            allow_metadata_failure=ipc_endpoint.kind == "tcp",
        )
        _cli_pkg._launch_dashboard_background(
            project_path=project_path,
            ipc_endpoint=ipc_endpoint,
            session_id=run_session_id,
            seed=seed,
            budget=effective_budget,
            policy_mode=effective_policy_mode,
            policy=policy,
            strict=strict,
            config_path=str(_cfg_path) if _cfg_path else None,
        )
        return

    metadata_available = _cli_pkg._prepare_session_discovery_paths(
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
                    "mode": _cli_pkg._display_run_mode(run_mode),
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
                _cli_pkg._run_agent_mode(
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
                _cli_pkg._run_headless_mode(
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
            _cli_pkg._run_solo_mode(
                cfg=cfg,
                repo_root=repo_root,
                seed_path=seed_path_obj,
                policy_path=policy_path_obj,
                policy_mode=effective_policy_mode,
                session_id=run_session_id,
            )
    finally:
        stop_dashboard_process(project_path)
        cleanup_session(project_path)
