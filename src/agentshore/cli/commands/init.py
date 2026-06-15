"""``agentshore init`` subcommand."""

from __future__ import annotations

from pathlib import Path

import click

from agentshore import cli_helpers, command
from agentshore.cli.agent_select import (
    _interactive_agent_select,
    _load_config_for_agent_setup,
)
from agentshore.cli.identity_helpers import (
    _agent_keys_from_yaml,
    _existing_identities_from_yaml,
    _identity_defaults_from_yaml,
    _identity_repo_name_with_owner,
)
from agentshore.cli_helpers import _DEFAULT_BUDGET, _PROJECT_DIR
from agentshore.config.models import AgentConfig
from agentshore.config.yaml_io import ruamel_set_nested
from agentshore.errors import OrchestratorError


def _reset_agentshore_database(project_path: Path) -> list[Path]:
    """Remove AgentShore's SQLite database files for a project."""
    db_path = project_path / _PROJECT_DIR / "agentshore.db"
    removed: list[Path] = []
    for path in (
        db_path,
        db_path.with_name("agentshore.db-wal"),
        db_path.with_name("agentshore.db-shm"),
    ):
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def _detect_default_target_branch(project_path: Path) -> str | None:
    """Return a sensible default for the target-branch prompt.

    Order: ``origin/HEAD`` (the GitHub default branch) → current local
    branch → ``None``. All git commands run without raising; any failure
    falls through to the next source. Returns ``None`` when nothing
    sensible is available (e.g., not a git repo).
    """
    # 1. origin/HEAD — what GitHub treats as the default branch.
    result = command.git_sync(
        "symbolic-ref", "refs/remotes/origin/HEAD", cwd=project_path, timeout_seconds=5.0
    )
    if result.returncode == 0:
        ref = result.stdout.strip()
        prefix = "refs/remotes/origin/"
        if ref.startswith(prefix):
            return ref[len(prefix) :] or None

    # 2. Currently-checked-out branch.
    result = command.git_sync(
        "rev-parse", "--abbrev-ref", "HEAD", cwd=project_path, timeout_seconds=5.0
    )
    if result.returncode == 0:
        name = result.stdout.strip()
        if name and name != "HEAD":
            return name

    return None


def _write_target_branch_to_yaml(config_path: Path, branch: str) -> None:
    """Persist ``project.target_branch`` to *config_path* via ruamel round-trip.

    Mirrors ``agentshore.sidecar.project._write_target_branch`` so the desktop
    wizard and the CLI converge on the same on-disk shape. Comments and key
    ordering on other top-level entries are preserved.
    """
    ruamel_set_nested(config_path, ("project", "target_branch"), branch)


def _maybe_prompt_target_branch(
    project_path: Path,
    config_path: Path,
    *,
    explicit_target_branch: str | None,
) -> None:
    """Resolve and persist ``project.target_branch`` in agentshore.yaml.

    Precedence:

    1. ``--target-branch`` flag — never prompts, always writes.
    2. Interactive TTY — prompts with ``_detect_default_target_branch`` as
       the default. Empty input keeps any pre-existing value.
    3. Non-interactive without the flag — leaves the YAML untouched so
       scripted ``agentshore init`` runs are deterministic.

    The desktop setup wizard's ``TargetBranchScreen`` writes the same key
    via ``project.set_target_branch``; this keeps CLI parity (desktop-3t62).
    """
    if explicit_target_branch is not None:
        branch = explicit_target_branch.strip()
        if not branch:
            raise click.UsageError("--target-branch must not be empty")
        _write_target_branch_to_yaml(config_path, branch)
        click.echo(f"Set project.target_branch = {branch}")
        return

    from agentshore.subprocess_env import is_interactive

    if not is_interactive():
        # Non-interactive (no TTY, or AGENTSHORE_NONINTERACTIVE set) and no
        # explicit flag — leave the file alone so scripted runs are deterministic.
        return

    default = _detect_default_target_branch(project_path) or "main"
    try:
        branch = click.prompt(
            "Target branch for PRs and merges",
            default=default,
            type=str,
            show_default=True,
        )
    except click.Abort:
        click.echo("Skipped target-branch configuration.")
        return

    branch = (branch or "").strip()
    if not branch:
        click.echo("Skipped target-branch configuration (empty input).")
        return

    _write_target_branch_to_yaml(config_path, branch)
    click.echo(f"Set project.target_branch = {branch}")


def _run_beads_init(project_path: Path, config_path: Path | None) -> None:
    """Verify bd is installed, run bd init, and install bd git hooks.

    Called as the final step of `agentshore init`. Any failure is reported as a
    warning rather than aborting init — beads is a dependency but a missing
    bd binary should not block the rest of project setup.
    """
    from agentshore.beads.setup import run_beads_init
    from agentshore.config import load_config
    from agentshore.errors import ConfigError
    from agentshore.state import AgentType

    enabled_types: set[AgentType] = {AgentType.CLAUDE_CODE}
    if config_path and config_path.exists():
        try:
            cfg = load_config(config_path)
            valid_values = {at.value for at in AgentType}
            enabled_types = {
                AgentType(key)
                for key, agent_cfg in cfg.agents.items()
                if isinstance(agent_cfg, AgentConfig) and agent_cfg.enabled and key in valid_values
            }
        except (ConfigError, ValueError):
            pass

    try:
        run_beads_init(project_path, enabled_types)
        click.echo("Beads project graph initialised (bd).")
    except RuntimeError as exc:
        click.echo(f"Warning: beads setup skipped — {exc}", err=True)


@click.command()
@click.option(
    "--project",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    help="Target project directory",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite existing agentshore.yaml and reset the AgentShore database",
)
@click.option(
    "--install-skills",
    "install_skills_only",
    is_flag=True,
    help="Only install skill files, skip config generation",
)
@click.option(
    "--target-branch",
    "target_branch",
    type=str,
    default=None,
    help=(
        "Target branch for PRs and merges (writes project.target_branch in "
        "agentshore.yaml and skips the interactive prompt)."
    ),
)
def init(
    project: str,
    force: bool,
    install_skills_only: bool,
    target_branch: str | None,
) -> None:
    """Initialise an AgentShore project: generate agentshore.yaml and install skills.

    Examples:

      agentshore init

      agentshore init --force

      agentshore init --install-skills

      agentshore init --target-branch develop
    """
    from agentshore.skills import install_skills

    project_path = Path(project).resolve()
    config_yaml = project_path / "agentshore.yaml"

    # --install-skills: run only phases 2 + 4, skip all config-mutating steps.
    if install_skills_only:
        if target_branch is not None:
            raise click.UsageError(
                "--target-branch has no effect with --install-skills "
                "(skill install skips config generation)."
            )
        # -- 2. Install skill files ---------------------------------------
        installed = install_skills(project_path, force=force)
        if installed:
            click.echo(f"Installed {len(installed)} skill(s): {', '.join(installed)}")
        else:
            click.echo("All skills are up-to-date.")
        # -- 4. Ensure artifact dirs are gitignored -----------------------
        if (project_path / ".git").exists():
            gitignore = project_path / ".gitignore"
            existed = gitignore.exists()
            for _entry in (".agentshore/", ".agents/", ".beads/"):
                if cli_helpers._ensure_gitignore_entry(project_path, _entry):
                    verb = "Added" if existed else "Created"
                    click.echo(f"{verb} {_entry} to {gitignore}")
                    existed = True
        return

    # -- 1. Generate or merge agentshore.yaml ----------------------------
    if force:
        removed = _reset_agentshore_database(project_path)
        if removed:
            click.echo("Reset AgentShore database: " + ", ".join(str(path) for path in removed))

    if config_yaml.exists() and not force:
        click.echo(
            f"agentshore.yaml already exists at {config_yaml}. "
            f"Re-running the setup wizards to update settings; use `agentshore init --force` "
            f"to merge fresh template defaults into your existing config."
        )
    else:
        if force and config_yaml.exists():
            click.echo(
                f"Merging fresh template into {config_yaml} "
                f"(preserves user-edited keys outside `agents:`)"
            )

        # Detect project metadata for the template. Agent detection is
        # authoritative: init should not invent unavailable CLI agents.
        try:
            gh_info = cli_helpers._detect_gh_remote(project_path)
            name_with_owner = gh_info.get("nameWithOwner", "owner/repo")
        except OrchestratorError:
            name_with_owner = "owner/repo"

        agents = cli_helpers._detect_agents()
        written = cli_helpers._render_or_merge_agentshore_yaml(
            config_yaml,
            name_with_owner=name_with_owner,
            agents=agents,
            budget=_DEFAULT_BUDGET,
            strict=False,
        )
        if written and not config_yaml.exists():
            # Defensive — should always exist after _render_or_merge.
            click.echo(f"Created {config_yaml}")
        elif not force:
            click.echo(f"Created {config_yaml}")

    # -- 2. Install skill files ------------------------------------------
    installed = install_skills(project_path, force=force)
    if installed:
        click.echo(f"Installed {len(installed)} skill(s): {', '.join(installed)}")
    else:
        click.echo("All skills are up-to-date.")

    # -- 2b. Prompt for / persist the target branch ---------------------
    # Mirrors the desktop wizard's TargetBranchScreen so CLI-bootstrapped
    # projects also have project.target_branch set. See desktop-3t62.
    if config_yaml.exists():
        _maybe_prompt_target_branch(
            project_path,
            config_yaml,
            explicit_target_branch=target_branch,
        )

    # -- 3. Refresh availability + run wizards --------------------------
    # ``init`` is an explicit user command; all wizards run with prefill
    # from the (possibly merged) config. Both wizards skip cleanly when
    # stdin isn't a TTY.
    from agentshore.availability import refresh as refresh_availability
    from agentshore.errors import ConfigError
    from agentshore.identity_wizard import run_identity_wizard

    if config_yaml.exists():
        refresh_availability()
        _init_agents = cli_helpers._detect_agents()

        # -- 3a. Agent / tier / model wizard ------------------------------
        try:
            _init_cfg = _load_config_for_agent_setup(config_yaml)
            _interactive_agent_select(
                _init_cfg,
                _init_agents,
                config_yaml,
                force_run=True,
            )
        except (ConfigError, OSError, ValueError):
            pass  # unparseable YAML — skip; `agentshore configure` can fix

        # -- 3b. Identity wizard ------------------------------------------
        agent_keys = _agent_keys_from_yaml(config_yaml, detected_agents=_init_agents)
        if agent_keys:
            defaults = _identity_defaults_from_yaml(config_yaml)
            existing = _existing_identities_from_yaml(config_yaml)
            run_identity_wizard(
                config_yaml,
                agent_keys,
                force_run=True,
                defaults=defaults,
                existing_identities=existing,
                repo_name_with_owner=_identity_repo_name_with_owner(project_path),
            )

            # -- 3c. SSH signing pre-flight -----------------------------------
            # init always precedes running a session, so surface a missing
            # signing key here (with platform-correct guidance) rather than
            # letting it first bite a merge_pr play mid-run.
            from agentshore.cli.helpers import report_ssh_signing_status

            click.echo()
            report_ssh_signing_status(project_path)

    # -- 4. Ensure artifact dirs are gitignored --------------------------
    if (project_path / ".git").exists():
        gitignore = project_path / ".gitignore"
        existed = gitignore.exists()
        for _entry in (".agentshore/", ".agents/", ".beads/"):
            if cli_helpers._ensure_gitignore_entry(project_path, _entry):
                verb = "Added" if existed else "Created"
                click.echo(f"{verb} {_entry} to {gitignore}")
                existed = True

    # -- 5. Beads project-graph initialisation --------------------------
    _run_beads_init(project_path, config_yaml if config_yaml.exists() else None)
