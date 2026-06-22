"""Identity-related helpers shared between init, configure, and identity commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore import cli_helpers
from agentshore.cli.agent_select import _agent_key_for_detected_binary
from agentshore.config.coerce import str_or_none
from agentshore.errors import OrchestratorError

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.identity_wizard import IdentityBinding


def _raw_agent_resolves_to_type(name: str, cfg: object) -> bool:
    """True when a raw YAML agent entry maps to a supported ``AgentType``.

    Mirrors ``config._parsers._resolve_agent_type`` for the wizard's pre-load
    view: prefer the ``binary``→type registry, else the key itself. Keeps the
    wizard from offering identity bindings for keys ``load_config`` would reject
    (typos, or the removed ``api_*`` concept).
    """
    from agentshore.agents.registry import BINARY_TO_AGENT_TYPE
    from agentshore.state import AgentType

    binary = cfg.get("binary") if isinstance(cfg, dict) else None
    if isinstance(binary, str) and BINARY_TO_AGENT_TYPE.get(binary) is not None:
        return True
    try:
        AgentType(name)
    except ValueError:
        return False
    return True


def _agent_keys_from_yaml(
    config_path: Path,
    *,
    detected_agents: list[str] | None = None,
) -> list[str]:
    """Return enabled CLI agent keys declared in agentshore.yaml.

    Filters out agents with ``enabled: false`` so the identity wizard
    doesn't ask about them. Missing or non-bool ``enabled`` defaults to
    enabled, mirroring the config loader. Keys that resolve to no supported
    ``AgentType`` are dropped too — ``load_config`` rejects them, so the wizard
    must not offer them as identity-binding options. When *detected_agents* is
    provided, only supported CLI agents currently detected on PATH are returned.
    """
    import yaml as _yaml

    try:
        data = _yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, _yaml.YAMLError):
        return []
    agents = data.get("agents") or {}
    if not isinstance(agents, dict):
        return []
    available_keys: set[str] | None = None
    if detected_agents is not None:
        available_keys = {
            agent_key
            for binary in detected_agents
            if (agent_key := _agent_key_for_detected_binary(binary)) is not None
        }
    keys: list[str] = []
    for name, cfg in agents.items():
        if not isinstance(name, str):
            continue
        if available_keys is not None and name not in available_keys:
            continue
        if isinstance(cfg, dict) and cfg.get("enabled") is False:
            continue
        if not _raw_agent_resolves_to_type(name, cfg):
            continue
        keys.append(name)
    return keys


def _identity_defaults_from_yaml(config_path: Path) -> dict[str, str]:
    """Read ``identities:`` from agentshore.yaml; return agent_key → login map.

    Maps each entry in the agents block to its configured GitHub login so the
    interactive wizard can pre-select the existing binding. ``git_user_name``
    is display metadata and is never used as a GitHub login.
    """
    import yaml

    from agentshore.agents.identity import configured_github_login_from_yaml_fields
    from agentshore.identity_names import canonical_identity_name

    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}

    identities = raw.get("identities") or {}
    agents = raw.get("agents") or {}
    if not isinstance(identities, dict) or not isinstance(agents, dict):
        return {}

    identities_by_canonical = {
        canonical_identity_name(str(name)): value for name, value in identities.items()
    }
    defaults: dict[str, str] = {}
    for agent_key, agent_cfg in agents.items():
        if not isinstance(agent_cfg, dict):
            continue
        identity_name = agent_cfg.get("identity")
        if not identity_name:
            continue
        identity = (
            identities.get(identity_name)
            or identities.get(canonical_identity_name(str(identity_name)))
            or identities_by_canonical.get(canonical_identity_name(str(identity_name)))
        )
        if not isinstance(identity, dict):
            continue
        login = configured_github_login_from_yaml_fields(str(identity_name), identity)
        if login:
            defaults[agent_key] = login
    return defaults


def _identity_repo_name_with_owner(project_path: Path) -> str | None:
    """Best-effort GitHub ``owner/repo`` name for repo-scoped identity secrets."""

    try:
        name_with_owner = cli_helpers._detect_gh_remote(project_path).get("nameWithOwner")
    except OrchestratorError:
        return None
    return name_with_owner or None


def _existing_identities_from_yaml(config_path: Path) -> dict[str, IdentityBinding]:
    """Read ``identities:`` from agentshore.yaml; return login → IdentityBinding map.

    Uses the configured GitHub login for each identity. ``git_user_name`` is
    display metadata and is never used as a GitHub login. This lets the wizard
    surface keychain-stored or env-stored identities as candidates even when
    they aren't in ``gh auth status``.
    """
    import yaml

    from agentshore.agents.identity import configured_github_login_from_yaml_fields
    from agentshore.identity_names import canonical_identity_name, canonical_keychain_service
    from agentshore.identity_wizard import IdentityBinding

    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}

    identities = raw.get("identities") or {}
    if not isinstance(identities, dict):
        return {}

    out: dict[str, IdentityBinding] = {}
    for ident_key, ident in identities.items():
        if not isinstance(ident, dict):
            continue
        login = configured_github_login_from_yaml_fields(str(ident_key), ident)
        if not login:
            continue
        default_email = f"{login}@users.noreply.github.com"
        canonical_login = canonical_identity_name(login)
        out[canonical_login] = IdentityBinding(
            name=canonical_identity_name(str(ident_key)),
            git_user_name=str(ident.get("git_user_name") or login),
            git_user_email=str(ident.get("git_user_email") or default_email),
            gh_token_login=str_or_none(ident.get("gh_token_login")),
            gh_token_env=str_or_none(ident.get("gh_token_env")),
            gh_token_keychain=(
                canonical_keychain_service(str(ident.get("gh_token_keychain")))
                if ident.get("gh_token_keychain") is not None
                else None
            ),
        )
    return out
