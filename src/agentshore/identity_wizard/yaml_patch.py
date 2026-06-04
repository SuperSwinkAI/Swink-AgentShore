"""YAML patcher for identity bindings + trusted-id normalization.

Writes wizard results back into ``agentshore.yaml`` while preserving the
file's leading comment block, and keeps ``trusted_ids.github_logins`` in
sync with the agents' bound identities.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import click
import yaml

from agentshore.config.coerce import str_or_none
from agentshore.identity_names import (
    canonical_identity_name,
    canonical_keychain_service,
    is_valid_github_login,
)

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.identity_wizard.wizard import IdentityBinding, WizardResult


def _split_leading_comments(text: str) -> tuple[str, str]:
    """Return (leading_comments_block, rest)."""
    lines = text.splitlines(keepends=True)
    head: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].lstrip()
        if stripped.startswith("#") or stripped == "" or stripped == "\n":
            head.append(lines[i])
            i += 1
            continue
        break
    return "".join(head), "".join(lines[i:])


def _warn_if_token_login_mismatch(name: str, yaml_dict: dict[str, str]) -> None:
    """Resolve the token for *yaml_dict* and warn if it belongs to a different user."""
    from agentshore.sidecar.identities import _token_for_identity

    token, _source = _token_for_identity(dict(yaml_dict))
    if token is None:
        return
    from agentshore.identity_names import resolve_github_login_for_token

    actual_login = resolve_github_login_for_token(token)
    if actual_login is None:
        return
    if canonical_identity_name(actual_login) != canonical_identity_name(name):
        click.echo(
            f"\n  Warning: token for {name!r} belongs to GitHub user "
            f"{actual_login!r} — check the login for typos",
            err=True,
        )


def _identity_to_yaml_dict(b: IdentityBinding) -> dict[str, str]:
    out: dict[str, str] = {
        "git_user_name": b.git_user_name,
        "git_user_email": b.git_user_email,
    }
    if b.gh_token_login is not None:
        out["gh_token_login"] = b.gh_token_login
    if b.gh_token_env is not None:
        out["gh_token_env"] = b.gh_token_env
    if b.gh_token_keychain is not None:
        out["gh_token_keychain"] = canonical_keychain_service(b.gh_token_keychain)
    return out


def _agent_bound_identity_logins(data: dict[object, object]) -> list[str]:
    """Return configured GitHub logins for CLI-agent identities in YAML data."""

    from agentshore.agents.identity import configured_github_login_from_fields

    raw_agents = data.get("agents") or {}
    raw_identities = data.get("identities") or {}
    if not isinstance(raw_agents, dict) or not isinstance(raw_identities, dict):
        return []

    identities_by_canonical = {
        canonical_identity_name(str(name)): value for name, value in raw_identities.items()
    }
    logins: list[str] = []
    seen: set[str] = set()
    for agent_key, agent_cfg in raw_agents.items():
        if not isinstance(agent_key, str):
            continue
        if not isinstance(agent_cfg, dict) or agent_cfg.get("enabled") is False:
            continue
        raw_identity_name = agent_cfg.get("identity")
        if not isinstance(raw_identity_name, str) or not raw_identity_name:
            continue

        canonical_identity = canonical_identity_name(raw_identity_name)
        ident = raw_identities.get(raw_identity_name) or identities_by_canonical.get(
            canonical_identity
        )
        if not isinstance(ident, dict):
            continue

        login = configured_github_login_from_fields(
            ident_name=canonical_identity,
            gh_token_login=str_or_none(ident.get("gh_token_login")),
            gh_token_env=str_or_none(ident.get("gh_token_env")),
            gh_token_keychain=str_or_none(ident.get("gh_token_keychain")),
        )
        if not login or not is_valid_github_login(login) or login in seen:
            continue
        logins.append(login)
        seen.add(login)
    return logins


def normalize_trusted_ids_for_bound_agents_in_data(data: dict[object, object]) -> bool:
    """Merge currently bound CLI identity logins into trusted_ids.github_logins."""

    raw_trusted = data.get("trusted_ids") or {}
    trusted: dict[object, object]
    trusted = dict(raw_trusted) if isinstance(raw_trusted, dict) else {}

    raw_logins = trusted.get("github_logins", [])
    merged: list[str] = []
    seen: set[str] = set()
    if isinstance(raw_logins, list):
        for value in raw_logins:
            if not isinstance(value, str):
                continue
            canonical = canonical_identity_name(value)
            if canonical and canonical not in seen:
                merged.append(canonical)
                seen.add(canonical)

    for login in _agent_bound_identity_logins(data):
        canonical = canonical_identity_name(login)
        if not canonical or not is_valid_github_login(canonical) or canonical in seen:
            continue
        merged.append(canonical)
        seen.add(canonical)

    if trusted.get("github_logins") == merged and data.get("trusted_ids") == trusted:
        return False
    trusted["github_logins"] = merged
    data["trusted_ids"] = trusted
    return True


def normalize_trusted_ids_for_bound_agents(config_path: Path) -> bool:
    """Normalize ``trusted_ids`` in *config_path* from existing agent bindings."""

    text = config_path.read_text(encoding="utf-8")
    head, body = _split_leading_comments(text)
    data = yaml.safe_load(body) or {}
    if not isinstance(data, dict):
        return False
    if not normalize_trusted_ids_for_bound_agents_in_data(data):
        return False
    new_text = head + yaml.dump(data, default_flow_style=False, sort_keys=False)
    if new_text == text:
        return False
    config_path.write_text(new_text, encoding="utf-8")
    return True


def patch_yaml_with_bindings(config_path: Path, result: WizardResult) -> bool:
    """Inject identities + agent identity links into *config_path*.

    Preserves leading ``#`` comment lines. Returns True if the file was
    modified, False if there was nothing to write.
    """
    if not result.identities and not result.agent_to_identity:
        return False

    text = config_path.read_text(encoding="utf-8")
    head, body = _split_leading_comments(text)
    data = yaml.safe_load(body) or {}
    if not isinstance(data, dict):
        # Should never happen for a valid agentshore.yaml.
        return False

    raw_identities_block = data.get("identities") or {}
    identities_block: dict[str, object] = {}
    if isinstance(raw_identities_block, dict):
        for name, value in raw_identities_block.items():
            canonical_name = canonical_identity_name(str(name))
            if canonical_name:
                identities_block[canonical_name] = value
    for name, binding in result.identities.items():
        yaml_dict = _identity_to_yaml_dict(binding)
        _warn_if_token_login_mismatch(name, yaml_dict)
        identities_block[canonical_identity_name(name)] = yaml_dict
    if identities_block:
        data["identities"] = identities_block

    agents_block = data.get("agents") or {}
    if isinstance(agents_block, dict):
        for agent_key, identity_name in result.agent_to_identity.items():
            agent_cfg = agents_block.get(agent_key)
            if isinstance(agent_cfg, dict):
                agent_cfg["identity"] = canonical_identity_name(identity_name)
        data["agents"] = agents_block

    normalize_trusted_ids_for_bound_agents_in_data(data)

    new_body = yaml.dump(data, default_flow_style=False, sort_keys=False)
    config_path.write_text(head + new_body, encoding="utf-8")
    return True
