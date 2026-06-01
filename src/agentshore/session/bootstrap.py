"""Session-bootstrap policy extracted from the ``agentshore start`` command.

The Click handler used to inline ~360 lines of budget/socket/config/identity
resolution between argument parsing and orchestrator dispatch. That logic is
session-bootstrap policy, not CLI plumbing: it is the same work the desktop
sidecar performs before launching a run. This module owns it so ``start()``
reduces to *parse → bootstrap → summary → dispatch*.

Detection helpers (``_find_repo_root``, ``_detect_agents``, …) are still
resolved through the ``agentshore.cli`` package namespace at call time so the
legacy ``patch("agentshore.cli._find_repo_root", …)`` test contract keeps
intercepting them after the bootstrap moved out of the command body.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import click

from agentshore import cli as _cli_pkg
from agentshore.budget import MIN_ENABLED_BUDGET_USD
from agentshore.cli_helpers import _PROJECT_DIR
from agentshore.errors import OrchestratorError
from agentshore.session_path import (
    IpcEndpoint,
    is_session_running,
    resolve_start_ipc_endpoint,
    session_socket_path,
)

if TYPE_CHECKING:
    from agentshore.config.models import PolicyMode, RunMode, RuntimeConfig


@dataclass(frozen=True)
class StartOptions:
    """Parsed ``agentshore start`` arguments relevant to bootstrap."""

    project_path: Path
    run_session_id: str
    seed: str | None
    effective_budget: float | None
    policy_mode_override: PolicyMode | None
    run_mode: RunMode
    socket: str | None
    ipc_host: str
    ipc_port: int
    strict: bool
    config_path: str | None


@dataclass(frozen=True)
class ResolvedSession:
    """The fully-resolved bootstrap result handed to dispatch and renderers."""

    cfg: RuntimeConfig
    cfg_path: Path
    project_path: Path
    repo_root: Path
    gh_info: dict[str, str]
    agents: list[str]
    api_keys: dict[str, bool]
    run_mode: RunMode
    effective_budget: float | None
    effective_policy_mode: PolicyMode
    ipc_endpoint: IpcEndpoint
    resolved_socket: str
    well_known_socket: Path
    seed_path: Path | None
    seed_kind: str | None
    run_session_id: str


def resolve_effective_budget(budget: float, *, no_budget: bool) -> float | None:
    """Resolve ``--budget``/``--no-budget`` to an enforced cap or ``None``.

    ``--no-budget`` wins over a ``--budget`` value. Raises
    :class:`click.BadParameter` for non-positive or below-floor caps so the
    error surfaces at the CLI layer with the usual usage hint.
    """
    if no_budget:
        return None
    if budget <= 0:
        raise click.BadParameter("Budget must be positive. Use --no-budget to disable budgeting.")
    if budget < MIN_ENABLED_BUDGET_USD:
        raise click.BadParameter(
            f"Budget must be at least ${MIN_ENABLED_BUDGET_USD:.2f}. "
            "Use --no-budget to disable budgeting."
        )
    return budget


def _load_config_with_overrides(
    cfg_path: Path,
    *,
    effective_budget: float | None,
    policy_mode_override: PolicyMode | None,
    strict: bool,
) -> tuple[RuntimeConfig, PolicyMode]:
    """Load *cfg_path* and apply CLI overrides, returning (cfg, effective_policy_mode).

    On a YAML parse error with a locatable problem mark, prints a help message
    and exits 1. Other load failures fall back to defaults with a warning.
    """
    from agentshore.config import load_config
    from agentshore.errors import ConfigError

    try:
        cfg = load_config(cfg_path)
    except (ConfigError, OSError, ValueError) as exc:
        # Attempt to give a helpful YAML-specific message. The problem_mark may
        # be on the exception itself (yaml.YAMLError) or on its __cause__ (when
        # ConfigError wraps a YAML parse error).
        mark = getattr(exc, "problem_mark", None) or getattr(
            getattr(exc, "__cause__", None), "problem_mark", None
        )
        line_info = f" (line {mark.line + 1})" if mark is not None else ""
        if line_info:
            click.echo(
                f"Error: Invalid YAML in {cfg_path}{line_info}\n"
                f"  {exc}\n\n"
                "Hint: Run 'agentshore init --force' to regenerate the config.",
                err=True,
            )
            raise SystemExit(1) from exc
        cfg = load_config(None)
        click.echo(f"Warning: config load failed ({exc}), using defaults.", err=True)

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
    return cfg, effective_policy_mode


def bootstrap_session(opts: StartOptions) -> ResolvedSession:
    """Resolve everything ``agentshore start`` needs before dispatch.

    Performs the session-already-running guard, socket/IPC endpoint resolution,
    git/GitHub/agent/API-key detection, ``.agentshore`` + ``agentshore.yaml``
    creation, and config load + CLI-override application. Emits the same
    diagnostics and exit codes as the original inline ``start()`` body.
    """
    if is_session_running(opts.project_path):
        click.echo(
            "Error: An AgentShore session is already running for this project.\n"
            "Stop it with: agentshore stop",
            err=True,
        )
        raise SystemExit(1)

    # Default socket location: well-known per-project path. When --socket
    # overrides it, also register the override via a symlink at the well-known
    # path so external tools can still discover it by hashing the project.
    well_known_socket = session_socket_path(opts.project_path)
    ipc_endpoint, resolved_socket = resolve_start_ipc_endpoint(
        opts.project_path,
        socket_override=opts.socket,
        ipc_host=opts.ipc_host,
        ipc_port=opts.ipc_port,
    )

    # Detect git repo root.
    try:
        repo_root = _cli_pkg._find_repo_root(opts.project_path)
    except OrchestratorError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc

    # Resolve --seed as file or directory.
    seed_path: Path | None = None
    seed_kind: str | None = None
    if opts.seed is not None:
        seed_path, seed_kind = _cli_pkg._resolve_seed_input_path(opts.seed, repo_root)

    # Detect GitHub remote.
    try:
        gh_info = _cli_pkg._detect_gh_remote(repo_root)
    except OrchestratorError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc

    # Detect agents on PATH and API keys.
    agents = _cli_pkg._detect_agents()
    api_keys = _cli_pkg._detect_api_keys()

    # Hard-fail only if NEITHER a CLI agent is present NOR any API key is set.
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

    # Create .agentshore/ directory.
    agentshore_dir = repo_root / _PROJECT_DIR
    agentshore_dir.mkdir(exist_ok=True)

    # Ensure agentshore.yaml exists.
    default_config_path = repo_root / "agentshore.yaml"
    if not default_config_path.exists():
        name_with_owner = gh_info.get("nameWithOwner", "owner/repo")
        config_text = _cli_pkg._generate_default_config(
            name_with_owner, agents, opts.effective_budget, opts.strict
        )
        default_config_path.write_text(config_text)
        click.echo(f"Generated {default_config_path}")

    # Load config and apply CLI overrides.
    cfg_path = Path(opts.config_path) if opts.config_path else repo_root / "agentshore.yaml"
    cfg, effective_policy_mode = _load_config_with_overrides(
        cfg_path,
        effective_budget=opts.effective_budget,
        policy_mode_override=opts.policy_mode_override,
        strict=opts.strict,
    )

    return ResolvedSession(
        cfg=cfg,
        cfg_path=cfg_path,
        project_path=opts.project_path,
        repo_root=repo_root,
        gh_info=gh_info,
        agents=agents,
        api_keys=api_keys,
        run_mode=opts.run_mode,
        effective_budget=opts.effective_budget,
        effective_policy_mode=effective_policy_mode,
        ipc_endpoint=ipc_endpoint,
        resolved_socket=resolved_socket,
        well_known_socket=well_known_socket,
        seed_path=seed_path,
        seed_kind=seed_kind,
        run_session_id=opts.run_session_id,
    )


def echo_bootstrap_summary(resolved: ResolvedSession) -> None:
    """Print the ``AgentShore — bootstrap summary`` banner for a resolved session."""
    click.echo("=" * 60)
    click.echo("  AgentShore — bootstrap summary")
    click.echo("=" * 60)
    click.echo(f"  Session ID     : {resolved.run_session_id}")
    click.echo(f"  Repo root      : {resolved.repo_root}")
    click.echo(f"  GitHub remote  : {resolved.gh_info.get('url', '(unknown)')}")
    click.echo(f"  Agents on PATH : {', '.join(resolved.agents)}")
    if resolved.api_keys:
        click.echo(f"  API keys       : {', '.join(resolved.api_keys)}")
    else:
        click.echo("  API keys       : (none detected)")
    click.echo(f"  Mode           : {_cli_pkg._display_run_mode(resolved.run_mode)}")
    budget_display = (
        "disabled" if resolved.effective_budget is None else f"${resolved.effective_budget:.2f}"
    )
    click.echo(f"  Budget         : {budget_display}")
    click.echo(f"  Policy mode    : {resolved.effective_policy_mode.summary_label}")
    if resolved.seed_path is not None:
        click.echo(f"  Seed input     : {resolved.seed_path} ({resolved.seed_kind})")
    click.echo(f"  Project key    : {resolved.well_known_socket.parent.name} (stable path hash)")
    if resolved.ipc_endpoint.kind == "unix":
        click.echo(f"  Socket         : {resolved.ipc_endpoint.path}")
    else:
        click.echo(
            f"  IPC            : tcp://{resolved.ipc_endpoint.host}:{resolved.ipc_endpoint.port}"
        )
    click.echo("=" * 60)
    click.echo()


def preflight_identities(cfg: RuntimeConfig, repo_root: Path) -> None:
    """Print the identity-resolution banner and enforce identity preconditions.

    Emits the identity-binding report, the repo-access report, and the SSH
    signing-key probe, then enforces: (a) configured tokens validated, (b)
    configured tokens can reach this repository, and (c) at least two distinct
    GitHub identities exist for code review. Any failed precondition exits 1.
    No-op when no identities are configured.
    """
    if not (cfg.identities or any(a.identity for a in cfg.agents.values())):
        # Still enforce the ≥2-identity precondition below for parity with the
        # original flow (which ran it unconditionally).
        _require_two_distinct_identities(cfg)
        return

    from agentshore.agents.identity import (
        bad_identity_rows,
        report_identities,
        report_identity_repo_access,
    )
    from agentshore.cli_identity import echo_identity_report, echo_repo_access_report

    click.echo("=" * 60)
    identity_rows = report_identities(cfg)
    echo_identity_report(identity_rows)
    invalid = bad_identity_rows(identity_rows)
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
    if repo_access_rows:
        click.echo()
        echo_repo_access_report(repo_access_rows)
    click.echo("=" * 60)
    click.echo()

    # desktop-l7i: warn if no SSH key is loaded in the agent. Identity-
    # configured runs use SSH-signed commits via `git merge --no-ff`; an empty
    # ssh-agent means merge_pr plays will fail mid-session with
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

    _require_two_distinct_identities(cfg)


def _require_two_distinct_identities(cfg: RuntimeConfig) -> None:
    """Enforce the ≥2 distinct GH identities precondition for code review.

    Code review requires the reviewer's GH login to differ from the PR
    author's; a single-identity session can never approve any PR. Fail fast
    here rather than burning plays + PPO penalties at runtime.
    """
    from agentshore.agents.identity import require_two_distinct_gh_identities
    from agentshore.errors import ConfigError

    try:
        require_two_distinct_gh_identities(cfg)
    except ConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc
