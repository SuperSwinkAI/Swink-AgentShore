"""Resolve start-session config, budget, identity, seed, and IPC settings."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import click

from agentshore import cli_helpers
from agentshore.budget import MIN_ENABLED_BUDGET_USD
from agentshore.cli.helpers import (
    _display_run_mode,
    _resolve_seed_input_path,
    report_ssh_signing_status,
)
from agentshore.cli_helpers import _PROJECT_DIR
from agentshore.errors import OrchestratorError
from agentshore.session_path import (
    IpcEndpoint,
    is_session_running,
    resolve_start_ipc_endpoint,
    session_socket_path,
)

if TYPE_CHECKING:
    from agentshore.config.models import BudgetConfig, PolicyMode, RunMode, RuntimeConfig


@dataclass(frozen=True)
class StartOptions:
    """Parsed ``agentshore start`` arguments relevant to bootstrap."""

    project_path: Path
    run_session_id: str
    seed: str | None
    budget_override: float | None
    time_override: int | None
    unlimited: bool
    policy_mode_override: PolicyMode | None
    run_mode: RunMode
    socket: str | None
    ipc_host: str
    ipc_port: int
    strict: bool | None
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


def validate_budget_flag(budget: float | None) -> None:
    """Validate an explicit ``--budget`` value at CLI parse time.

    Only an explicitly-provided value is checked; ``None`` (flag omitted) defers
    to the config and is always valid here. Raises :class:`click.BadParameter`
    for non-positive or below-floor caps so the error surfaces at the CLI layer
    with the usual usage hint. The actual budget resolution (config vs override
    vs ``--unlimited``) happens in :func:`_load_config_with_overrides`, which has
    the loaded config in hand.
    """
    if budget is None:
        return
    if budget <= 0:
        raise click.BadParameter("Budget must be positive. Use --unlimited to disable budgeting.")
    if budget < MIN_ENABLED_BUDGET_USD:
        raise click.BadParameter(
            f"Budget must be at least ${MIN_ENABLED_BUDGET_USD:.2f}. "
            "Use --unlimited to disable budgeting."
        )


def resolve_budget_config(
    base: BudgetConfig,
    *,
    budget_override: float | None,
    time_override: int | None,
    unlimited: bool,
    absent: bool = False,
) -> BudgetConfig:
    """Resolve the effective dual-dimension :class:`BudgetConfig`.

    ``base`` is ``cfg.budget`` from the loaded config. ``absent=True`` signals
    that the YAML had NO ``budget:`` block — this replaces the old
    ``base != BudgetConfig()`` equality sentinel, which would silently
    re-impose caps after ``agentshore start --unlimited`` seeded the file.

    Resolution:

    * ``--unlimited`` → both caps off (overrides everything).
    * ``absent=False`` (budget block was present) → respect as-is, with
      ``--budget`` / ``--time`` overriding their own dimension only.
    * ``absent=True`` (no budget block) → apply safety defaults ($200 + 24h)
      only when neither flag is given; naming one dimension suppresses the
      other dimension's bare default (leaves it off).
    """
    if unlimited:
        return dataclasses.replace(base, enabled=False, time_enabled=False)

    if not absent:
        resolved = base
        if budget_override is not None:
            resolved = dataclasses.replace(resolved, enabled=True, total=budget_override)
        if time_override is not None:
            resolved = dataclasses.replace(
                resolved, time_enabled=True, time_total_minutes=time_override
            )
        return resolved

    # No budget block in YAML — apply defaults or flags.
    if budget_override is None and time_override is None:
        return dataclasses.replace(
            base,
            enabled=True,
            total=cli_helpers._DEFAULT_BUDGET,
            time_enabled=True,
            time_total_minutes=cli_helpers._DEFAULT_TIME_MINUTES,
        )
    return dataclasses.replace(
        base,
        enabled=budget_override is not None,
        total=budget_override if budget_override is not None else 0.0,
        time_enabled=time_override is not None,
        time_total_minutes=time_override if time_override is not None else 0,
    )


def _load_config_with_overrides(
    cfg_path: Path,
    *,
    budget_override: float | None,
    time_override: int | None,
    unlimited: bool,
    policy_mode_override: PolicyMode | None,
    strict: bool | None,
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

    # Budget: resolve both soft-cap dimensions (dollars + time) against the
    # loaded config. See :func:`resolve_budget_config` for the precedence rules
    # (configured yaml respected as-is; safety defaults only on empty config;
    # naming one dimension suppresses the other's bare default; --unlimited off).
    budget_cfg = resolve_budget_config(
        cfg.budget,
        budget_override=budget_override,
        time_override=time_override,
        unlimited=unlimited,
        absent=cfg.budget_absent,
    )

    # Scope strict mode: only override when --strict/--no-strict was given.
    scope_cfg = cfg.scope if strict is None else dataclasses.replace(cfg.scope, strict_mode=strict)

    cfg = dataclasses.replace(
        cfg,
        budget=budget_cfg,
        rl=dataclasses.replace(cfg.rl, policy_mode=effective_policy_mode),
        scope=scope_cfg,
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
        repo_root = cli_helpers._find_repo_root(opts.project_path)
    except OrchestratorError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc

    # Resolve --seed as file or directory.
    seed_path: Path | None = None
    seed_kind: str | None = None
    if opts.seed is not None:
        seed_path, seed_kind = _resolve_seed_input_path(opts.seed, repo_root)

    # Detect GitHub remote.
    try:
        gh_info = cli_helpers._detect_gh_remote(repo_root)
    except OrchestratorError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc

    # Detect agents on PATH and API keys.
    agents = cli_helpers._detect_agents()
    api_keys = cli_helpers._detect_api_keys()

    # Hard-fail only if NEITHER a CLI agent is present NOR any API key is set.
    if not agents and not api_keys:
        click.echo(
            "Error: No coding agents found.\n\n"
            "AgentShore needs at least one agent. Options:\n"
            "  1. Install Claude Code:  npm install -g @anthropic-ai/claude-code\n"
            "  2. Install Codex CLI:    pip install codex-cli\n"
            "  3. Install Gemini CLI:   npm install -g @google/gemini-cli\n"
            "  4. Install Grok CLI:     npm install -g @xai-official/grok\n"
            "  5. Install Antigravity CLI (agy):  https://antigravity.google/product/antigravity-cli\n"
            "  6. Set an API key:       export ANTHROPIC_API_KEY=sk-ant-...\n"
            "                           export OPENAI_API_KEY=sk-...",
            err=True,
        )
        raise SystemExit(1)

    # Create .agentshore/ directory.
    agentshore_dir = repo_root / _PROJECT_DIR
    agentshore_dir.mkdir(exist_ok=True)

    # Ensure agentshore.yaml exists. Seed a fresh config from the resolved CLI
    # intent so both soft-cap dimensions land in the file exactly as the run
    # will use them (naked start → $200 + 24h; --unlimited → both off; etc.).
    default_config_path = repo_root / "agentshore.yaml"
    if not default_config_path.exists():
        from agentshore.config.models import BudgetConfig

        name_with_owner = gh_info.get("nameWithOwner", "owner/repo")
        seeded = resolve_budget_config(
            BudgetConfig(),
            budget_override=opts.budget_override,
            time_override=opts.time_override,
            unlimited=opts.unlimited,
            absent=True,  # no existing budget block → apply defaults or flags
        )
        seed_budget = seeded.total if seeded.enabled else None
        seed_time = seeded.time_total_minutes if seeded.time_enabled else None
        config_text = cli_helpers._generate_default_config(
            name_with_owner, agents, seed_budget, bool(opts.strict), time_minutes=seed_time
        )
        default_config_path.write_text(config_text)
        click.echo(f"Generated {default_config_path}")

    # Load config and apply CLI overrides.
    cfg_path = Path(opts.config_path) if opts.config_path else repo_root / "agentshore.yaml"
    cfg, effective_policy_mode = _load_config_with_overrides(
        cfg_path,
        budget_override=opts.budget_override,
        time_override=opts.time_override,
        unlimited=opts.unlimited,
        policy_mode_override=opts.policy_mode_override,
        strict=opts.strict,
    )
    # The merged config is the source of truth for the effective budget shown in
    # the banner and propagated to detached subprocesses.
    effective_budget = cfg.budget.total if cfg.budget.enabled else None

    return ResolvedSession(
        cfg=cfg,
        cfg_path=cfg_path,
        project_path=opts.project_path,
        repo_root=repo_root,
        gh_info=gh_info,
        agents=agents,
        api_keys=api_keys,
        run_mode=opts.run_mode,
        effective_budget=effective_budget,
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
    click.echo(f"  Mode           : {_display_run_mode(resolved.run_mode)}")
    budget_display = (
        "disabled" if resolved.effective_budget is None else f"${resolved.effective_budget:.2f}"
    )
    click.echo(f"  Budget         : {budget_display}")
    budget_cfg = resolved.cfg.budget
    if budget_cfg.time_enabled:
        hours, minutes = divmod(int(budget_cfg.time_total_minutes), 60)
        time_display = f"{hours}h {minutes}m" if minutes else f"{hours}h"
    else:
        time_display = "disabled"
    click.echo(f"  Time budget    : {time_display}")
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
    from agentshore.identity_wizard import echo_identity_report, echo_repo_access_report

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
    report_ssh_signing_status(repo_root)
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


def preflight_cli_agent_auth(cfg: RuntimeConfig) -> None:
    """Probe each configured CLI agent's backend auth before the loop starts.

    Validates the model-provider session each CLI agent uses (e.g. Codex's
    cached chatgpt.com token), which `preflight_identities` does NOT cover.
    Prints a per-agent banner row and exits 1 if any configured CLI agent has
    a definitively expired/dead backend session. Non-blocking statuses
    (timeout/error/unprobeable) are surfaced as warnings, not failures.
    """
    from agentshore.agents.auth_probe import probe_configured_cli_auth

    results = probe_configured_cli_auth(cfg)
    if not results:
        # No CLI agents configured — nothing to probe. Stay quiet.
        return

    click.echo("=" * 60)
    click.echo("  AgentShore — CLI agent backend auth")
    click.echo("=" * 60)
    for result in results:
        mark = "✓" if result.ok else "✗"
        click.echo(f"  {mark} {result.agent_type.value:<12} {result.status:<12} {result.detail}")
    click.echo("=" * 60)
    click.echo()

    # Non-blocking non-ok statuses (timeout/error/unprobeable that aren't "ok"):
    # surface as warnings but never gate the launch.
    for result in results:
        if not result.ok and not result.blocks_launch:
            click.echo(
                f"Warning: could not confirm {result.agent_type.value} backend auth "
                f"({result.status}): {result.detail}",
                err=True,
            )

    blocked = [r for r in results if r.blocks_launch]
    if blocked:
        names = ", ".join(r.agent_type.value for r in blocked)
        click.echo(
            f"Error: CLI agent backend session expired/dead: {names}. "
            f"Re-authenticate before starting (e.g. run 'codex login'), or pass "
            "--skip-auth-preflight for an offline/air-gapped run.",
            err=True,
        )
        raise SystemExit(1)


def preflight_git_auth(cfg: RuntimeConfig, project_path: Path) -> None:
    """Probe each configured GitHub identity's git-remote auth before the loop.

    Validates that EACH configured identity can authenticate to the repo's git
    remote non-interactively (HTTPS token header or SSH key), which neither
    ``preflight_identities`` (GitHub *API* token validity) nor
    ``preflight_cli_agent_auth`` (model-provider session) covers. Surfaces a
    broken identity at launch instead of via a mid-run credential-prompt hang.

    Prints a per-identity banner row and exits 1 only if an identity has a
    definitive git-auth failure. Non-blocking statuses (timeout/error/
    unprobeable) are surfaced as warnings, not failures. No-op when no
    identities are configured.
    """
    from agentshore.agents.git_auth_probe import probe_all_identities

    results = probe_all_identities(cfg, project_path=project_path)
    if not results:
        # No identities configured — nothing to probe. Stay quiet.
        return

    click.echo("=" * 60)
    click.echo("  AgentShore — git remote auth")
    click.echo("=" * 60)
    for result in results:
        mark = "✓" if result.ok else "✗"
        click.echo(f"  {mark} {result.identity_name:<16} {result.status:<12} {result.detail}")
    click.echo("=" * 60)
    click.echo()

    # Non-blocking non-ok statuses (timeout/error/unprobeable): surface as
    # warnings but never gate the launch.
    for result in results:
        if not result.ok and not result.blocks_launch:
            click.echo(
                f"Warning: could not confirm git auth for identity "
                f"{result.identity_name!r} ({result.status}): {result.detail}",
                err=True,
            )

    blocked = [r for r in results if r.blocks_launch]
    if blocked:
        remote = blocked[0].remote or "the git remote"
        names = ", ".join(r.identity_name for r in blocked)
        click.echo(
            f"Error: identity {names} cannot authenticate to {remote}; "
            "check its token/SSH key. Re-provision the identity (see "
            "docs/identity.md), or pass --skip-git-auth-preflight for an "
            "offline/air-gapped run.",
            err=True,
        )
        raise SystemExit(1)


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
