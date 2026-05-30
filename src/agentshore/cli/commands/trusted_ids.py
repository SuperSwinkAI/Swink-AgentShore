"""``agentshore trusted-ids`` group and subcommands."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import click

from agentshore.identity_names import canonical_identity_name, is_valid_github_login


def _trusted_ids_config_path(project: str) -> Path:
    project_path = Path(project).resolve()
    cfg_path = project_path / "agentshore.yaml"
    if not cfg_path.exists():
        raise click.ClickException(f"No agentshore.yaml at {cfg_path}.")
    return cfg_path


def _canonicalize_cli_github_login(login: str) -> str:
    if not is_valid_github_login(login):
        raise click.ClickException(f"Invalid GitHub login: {login!r}")
    return canonical_identity_name(login)


def _read_trusted_ids_config(config_path: Path) -> tuple[dict[str, object], list[str], list[int]]:
    import yaml

    try:
        raw_loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise click.ClickException(f"Could not read {config_path}: {exc}") from exc
    if not isinstance(raw_loaded, dict):
        raise click.ClickException(f"{config_path} must contain a YAML mapping")

    raw = cast("dict[str, object]", raw_loaded)
    trusted_raw = raw.get("trusted_ids") or {}
    if not isinstance(trusted_raw, dict):
        raise click.ClickException("trusted_ids must be a mapping")
    github_raw = trusted_raw.get("github_logins", [])
    if not isinstance(github_raw, list):
        raise click.ClickException("trusted_ids.github_logins must be a list")
    pr_allow_list_raw = trusted_raw.get("pr_allow_list", [])
    if not isinstance(pr_allow_list_raw, list):
        raise click.ClickException("trusted_ids.pr_allow_list must be a list")

    logins: list[str] = []
    seen: set[str] = set()
    for value in github_raw:
        if not isinstance(value, str) or not value.strip():
            raise click.ClickException("trusted_ids.github_logins contains a non-string value")
        canonical = _canonicalize_cli_github_login(value)
        if canonical not in seen:
            logins.append(canonical)
            seen.add(canonical)
    pr_allow_list: list[int] = []
    seen_prs: set[int] = set()
    for value in pr_allow_list_raw:
        if not isinstance(value, int) or value <= 0:
            raise click.ClickException("trusted_ids.pr_allow_list contains a non-positive integer")
        if value not in seen_prs:
            pr_allow_list.append(value)
            seen_prs.add(value)
    return raw, logins, pr_allow_list


def _write_trusted_ids_config(
    config_path: Path,
    raw: dict[str, object],
    logins: list[str],
    pr_allow_list: list[int],
) -> None:
    import yaml

    trusted_raw = raw.get("trusted_ids") or {}
    if not isinstance(trusted_raw, dict):
        raise click.ClickException("trusted_ids must be a mapping")
    trusted = cast("dict[str, object]", trusted_raw)
    trusted["github_logins"] = logins
    trusted["pr_allow_list"] = pr_allow_list
    raw["trusted_ids"] = trusted
    try:
        config_path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    except OSError as exc:
        raise click.ClickException(f"Could not write {config_path}: {exc}") from exc


@click.group(name="trusted-ids")
def trusted_ids() -> None:
    """Manage extra trusted external identities."""


@trusted_ids.command("list")
@click.option(
    "--project",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    show_default=True,
    help="Target project directory",
)
def trusted_ids_list(project: str) -> None:
    """List trusted external GitHub logins."""
    cfg_path = _trusted_ids_config_path(project)
    _, logins, pr_allow_list = _read_trusted_ids_config(cfg_path)
    if not logins and not pr_allow_list:
        click.echo("No trusted IDs configured.")
        return
    if logins:
        click.echo("Trusted GitHub logins:")
        for login in logins:
            click.echo(f"  {login}")
    else:
        click.echo("No trusted GitHub logins configured.")
    if pr_allow_list:
        click.echo("PR allow list:")
        for pr_number in pr_allow_list:
            click.echo(f"  {pr_number}")
    else:
        click.echo("No PRs allow-listed.")


@trusted_ids.command("add-gh")
@click.argument("login")
@click.option(
    "--project",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    show_default=True,
    help="Target project directory",
)
def trusted_ids_add_gh(login: str, project: str) -> None:
    """Trust an external GitHub login."""
    cfg_path = _trusted_ids_config_path(project)
    raw, logins, pr_allow_list = _read_trusted_ids_config(cfg_path)
    canonical = _canonicalize_cli_github_login(login)
    if canonical not in logins:
        logins.append(canonical)
        _write_trusted_ids_config(cfg_path, raw, logins, pr_allow_list)
    click.echo(f"Trusted GitHub login: {canonical}")


@trusted_ids.command("remove-gh")
@click.argument("login")
@click.option(
    "--project",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    show_default=True,
    help="Target project directory",
)
def trusted_ids_remove_gh(login: str, project: str) -> None:
    """Stop trusting an external GitHub login."""
    cfg_path = _trusted_ids_config_path(project)
    raw, logins, pr_allow_list = _read_trusted_ids_config(cfg_path)
    canonical = _canonicalize_cli_github_login(login)
    updated = [item for item in logins if item != canonical]
    if updated != logins:
        _write_trusted_ids_config(cfg_path, raw, updated, pr_allow_list)
    click.echo(f"Removed trusted GitHub login: {canonical}")


@trusted_ids.command("add-pr")
@click.argument("pr_number", type=click.IntRange(min=1))
@click.option(
    "--project",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    show_default=True,
    help="Target project directory",
)
def trusted_ids_add_pr(pr_number: int, project: str) -> None:
    """Allow-list an external pull request number."""
    cfg_path = _trusted_ids_config_path(project)
    raw, logins, pr_allow_list = _read_trusted_ids_config(cfg_path)
    if pr_number not in pr_allow_list:
        pr_allow_list.append(pr_number)
        _write_trusted_ids_config(cfg_path, raw, logins, pr_allow_list)
    click.echo(f"Allow-listed PR: {pr_number}")


@trusted_ids.command("remove-pr")
@click.argument("pr_number", type=click.IntRange(min=1))
@click.option(
    "--project",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    show_default=True,
    help="Target project directory",
)
def trusted_ids_remove_pr(pr_number: int, project: str) -> None:
    """Remove a pull request number from the allow list."""
    cfg_path = _trusted_ids_config_path(project)
    raw, logins, pr_allow_list = _read_trusted_ids_config(cfg_path)
    updated = [item for item in pr_allow_list if item != pr_number]
    if updated != pr_allow_list:
        _write_trusted_ids_config(cfg_path, raw, logins, updated)
    click.echo(f"Removed allow-listed PR: {pr_number}")
