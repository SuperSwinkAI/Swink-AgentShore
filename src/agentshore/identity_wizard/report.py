"""Identity/repo-access report renderers and the wizard entry point.

``run_identity_wizard`` is the public entry used by ``agentshore init`` and
``agentshore identity --reconfigure``; the echo helpers render the rows
produced by ``agentshore.agents.identity``.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404
import sys
from typing import TYPE_CHECKING

import click

from agentshore.identity_wizard.wizard import run_wizard
from agentshore.identity_wizard.yaml_patch import (
    normalize_trusted_ids_for_bound_agents,
    patch_yaml_with_bindings,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from agentshore.agents.identity import IdentityStatus, RepoAccessStatus
    from agentshore.identity_wizard.wizard import IdentityBinding, WizardResult


def echo_identity_report(rows: list[IdentityStatus], *, header: bool = True) -> None:
    """Pretty-print ``IdentityStatus`` rows from ``report_identities``.

    Format collapses status + source into a single bracketed clause so the
    line reads as one statement rather than two ambiguous columns:

        agent → identity  [token: ok via <source>]
        agent → identity  [token: INVALID — <reason>]
        agent → identity  [token: MISSING — <reason>]
    """
    if header:
        click.echo("Identity bindings")
        click.echo("─────────────────")
    if not rows:
        click.echo("  (no CLI agents configured)")
        return
    width = max(len(r.agent_key) for r in rows)
    for r in rows:
        if r.identity_name is None:
            line = f"  {r.agent_key:<{width}}  →  (no identity)  {r.detail}"
        elif r.token_valid or (r.token_resolved and r.validation_error is None):
            line = f"  {r.agent_key:<{width}}  →  {r.identity_name}  [token: ok via {r.detail}]"
        elif r.token_resolved:
            line = f"  {r.agent_key:<{width}}  →  {r.identity_name}  [token: INVALID — {r.detail}]"
        else:
            line = f"  {r.agent_key:<{width}}  →  {r.identity_name}  [token: MISSING — {r.detail}]"
        click.echo(line)


def echo_repo_access_report(rows: list[RepoAccessStatus], *, header: bool = True) -> None:
    """Pretty-print ``RepoAccessStatus`` rows from ``report_identity_repo_access``."""

    if not rows:
        return
    if header:
        click.echo("Repository access")
        click.echo("─────────────────")
    width = max(len(r.agent_key) for r in rows)
    for row in rows:
        identity = row.identity_name or "(no identity)"
        if row.ok:
            click.echo(f"  {row.agent_key:<{width}}  →  {identity}  [repo: ok]")
            continue
        detail = " ".join(row.detail.split())
        click.echo(f"  {row.agent_key:<{width}}  →  {identity}  [repo: BLOCKED — {detail}]")


def run_identity_wizard(
    config_path: Path,
    agent_keys: Iterable[str],
    *,
    force_run: bool = False,
    defaults: dict[str, str] | None = None,
    existing_identities: dict[str, IdentityBinding] | None = None,
    repo_name_with_owner: str | None = None,
) -> None:
    """Public entry point used by ``agentshore init`` and ``agentshore identity --reconfigure``.

    Gating:
    - ``AGENTSHORE_NONINTERACTIVE=1`` always wins (silent skip).
    - ``force_run=True`` and stdin not a TTY → print a notice and skip
      cleanly (no crash, no silent no-op).
    - ``force_run=False`` and stdin not a TTY → silent skip (legacy path).

    *defaults* maps ``agent_key`` → currently-bound ``login`` (read from the
    existing ``agentshore.yaml`` ``identities:`` block); the wizard pre-selects
    the binding and annotates it as ``(current)``.

    *existing_identities* maps ``login`` → ``IdentityBinding`` for every
    identity already in ``agentshore.yaml``. Used to surface keychain-only
    accounts (which aren't in ``gh auth status``) as picker candidates and
    to offer a "keep existing settings" shortcut in Step 2.

    *repo_name_with_owner* scopes wizard-managed keychain services to the
    current repository so fine-grained PATs do not collide across projects.
    """
    keys = [k for k in agent_keys]
    if not keys:
        return

    if os.environ.get("AGENTSHORE_NONINTERACTIVE"):
        normalize_trusted_ids_for_bound_agents(config_path)
        click.echo(
            "  (Identity wizard skipped — AGENTSHORE_NONINTERACTIVE is set. "
            "Edit agentshore.yaml manually or unset the variable.)"
        )
        return
    if not sys.stdin.isatty():
        normalize_trusted_ids_for_bound_agents(config_path)
        if force_run:
            click.echo(
                "  (Identity wizard requested but stdin is not a TTY; "
                "skipping. Run `agentshore identity --reconfigure` from an "
                "interactive shell.)"
            )
        return

    result = run_wizard(
        keys,
        defaults=defaults,
        existing_identities=existing_identities,
        repo_name_with_owner=repo_name_with_owner,
    )
    if not result.identities and not result.agent_to_identity:
        normalize_trusted_ids_for_bound_agents(config_path)
        return

    if patch_yaml_with_bindings(config_path, result):
        click.echo(f"\n  Wrote identity bindings to {config_path}")
        _echo_post_wizard_report(config_path, result)


def _echo_post_wizard_report(config_path: Path, result: WizardResult) -> None:
    """Reload the freshly-written config and print the resolution table.

    Closes the loop on the wizard: the user immediately sees whether each
    binding produces a usable token, with explicit ``export`` hints for
    any env-strategy identity that's still unset.
    """
    try:
        from agentshore.agents.identity import (
            bad_identity_rows,
            missing_token_rows,
            report_identities,
            report_identity_repo_access,
        )
        from agentshore.config import load_config
        from agentshore.errors import ConfigError

        cfg = load_config(config_path)
        rows = report_identities(cfg)
    except (ConfigError, OSError, subprocess.SubprocessError, RuntimeError) as exc:
        click.echo(f"\n  (Could not verify bindings — re-run `agentshore identity`: {exc})")
        return

    click.echo("")
    echo_identity_report(rows)
    bad = bad_identity_rows(rows)
    missing = missing_token_rows(rows)
    if bad and not missing:
        click.echo("\n  One or more identity tokens failed validation.")
        return
    if not bad:
        repo_access_rows = report_identity_repo_access(cfg, config_path.parent)
        if repo_access_rows:
            click.echo("")
            echo_repo_access_report(repo_access_rows)
        blocked = [r for r in repo_access_rows if not r.ok]
        if blocked:
            click.echo("\n  One or more identity tokens cannot access this repository.")
            raise SystemExit(1)
        suffix = " and can access the repository" if repo_access_rows else ""
        click.echo(f"\n  All identity tokens resolve{suffix}.")
        return

    click.echo(
        f"\n  {len(missing)} identit{'y' if len(missing) == 1 else 'ies'} need additional setup:"
    )
    env_hints = [
        f"    export {b.gh_token_env}=<paste PAT for {b.name}>"
        for b in result.identities.values()
        if b.gh_token_env
        and any(r.identity_name == b.name and not r.token_resolved for r in missing)
    ]
    for line in env_hints:
        click.echo(line)
    gh_login_hints = [
        f"    gh auth login -u {b.gh_token_login}"
        for b in result.identities.values()
        if b.gh_token_login
        and any(r.identity_name == b.name and not r.token_resolved for r in missing)
    ]
    for line in gh_login_hints:
        click.echo(line)
