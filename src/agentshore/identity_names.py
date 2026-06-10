"""Canonical naming helpers for GitHub identities."""

from __future__ import annotations

import re

AGENTSHORE_KEYCHAIN_PREFIX = "agentshore/"

_GITHUB_LOGIN_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}(?:\[bot\])?$",
    re.IGNORECASE,
)


def canonical_identity_name(value: str) -> str:
    """Return AgentShore's machine key for a GitHub identity.

    GitHub logins are case-insensitive. AgentShore preserves display casing in
    git/user fields, but uses case-folded keys for config links, keychain
    services, and safety comparisons.
    """

    return value.strip().casefold()


def is_valid_github_login(value: str) -> bool:
    """Return True when *value* matches GitHub actor login syntax."""

    return bool(_GITHUB_LOGIN_RE.fullmatch(value.strip()))


def canonical_keychain_service(service: str) -> str:
    """Normalize AgentShore's keychain service conventions.

    Supported AgentShore-managed services are:

    * ``agentshore/<login>`` for legacy global tokens.
    * ``agentshore/<owner>/<repo>/<login>`` for repo-scoped tokens.
    """

    value = service.strip()
    if value.casefold().startswith(AGENTSHORE_KEYCHAIN_PREFIX):
        return AGENTSHORE_KEYCHAIN_PREFIX + value[len(AGENTSHORE_KEYCHAIN_PREFIX) :].casefold()
    return value


def canonical_repo_name_with_owner(value: str) -> str:
    """Return a case-folded ``owner/repo`` key for GitHub repository names."""

    owner, sep, repo = value.strip().strip("/").partition("/")
    if not sep or not owner or not repo or "/" in repo:
        raise ValueError(f"expected GitHub repository name in owner/repo form, got {value!r}")
    return f"{owner.casefold()}/{repo.casefold()}"


def keychain_service_for_login(login: str) -> str:
    """Return the legacy global keychain service for a GitHub login."""

    return AGENTSHORE_KEYCHAIN_PREFIX + canonical_identity_name(login)


def keychain_service_for_repo_login(repo_name_with_owner: str, login: str) -> str:
    """Return the repo-scoped keychain service for a GitHub login."""

    repo_key = canonical_repo_name_with_owner(repo_name_with_owner)
    login_key = canonical_identity_name(login)
    return f"{AGENTSHORE_KEYCHAIN_PREFIX}{repo_key}/{login_key}"


def login_from_agentshore_keychain_service(service: str) -> str | None:
    """Return the login encoded in an AgentShore-managed keychain service."""

    canonical = canonical_keychain_service(service)
    if not canonical.startswith(AGENTSHORE_KEYCHAIN_PREFIX):
        return None
    body = canonical[len(AGENTSHORE_KEYCHAIN_PREFIX) :].strip("/")
    if not body:
        return None
    return body.rsplit("/", 1)[-1] or None


def same_identity(left: str | None, right: str | None) -> bool:
    """Return True when two GitHub login strings identify the same account."""

    if left is None or right is None:
        return False
    return canonical_identity_name(left) == canonical_identity_name(right)


def resolve_github_login_for_token(token: str) -> str | None:
    """Call ``gh api user`` to discover the actual GitHub login for *token*.

    Returns the login string on success, ``None`` if the token is invalid
    or the ``gh`` CLI is unavailable. Used at identity-add time to catch
    typos before they're persisted to ``agentshore.yaml``.
    """
    from agentshore import command

    result = command.gh_sync(
        "api",
        "user",
        "--jq",
        ".login",
        env_overlay={"GH_TOKEN": token, "GITHUB_TOKEN": token},
        timeout_seconds=10.0,
    )
    if result.tool_missing or result.returncode != 0:
        return None
    login = result.stdout.strip()
    return login or None
