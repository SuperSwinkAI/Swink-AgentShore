"""YAML config CRUD for agentshore.yaml identities and trusted_ids sections.

This module owns the persistence layer: reading and writing the ``identities:``
and ``trusted_ids:`` blocks in ``agentshore.yaml``.  Credential probing (token
resolution, repo access checks, diagnostics) lives in the sibling module
``sidecar/identities.py``, which also provides the public ``add_identity``
function (it needs token-match validation that depends on credential probing).
"""

from __future__ import annotations

import os
import tempfile
from enum import StrEnum
from typing import TYPE_CHECKING, NotRequired, TypedDict

import yaml

from agentshore.identity_names import (
    canonical_identity_name,
    is_valid_github_login,
    keychain_service_for_login,
)

if TYPE_CHECKING:
    from pathlib import Path


class IdentityRow(TypedDict):
    login: str
    source: str
    token_status: str
    repo_access: str
    repo_access_detail: NotRequired[str]


class TokenSource(StrEnum):
    """Token-source kinds for a configured identity.

    Values are the exact YAML field names written into ``agentshore.yaml`` so
    that enum members round-trip through config I/O and test assertions without
    any mapping step.
    """

    LOGIN = "gh_token_login"
    ENV = "gh_token_env"
    KEYCHAIN = "gh_token_keychain"
    AMBIENT = "ambient"


# Derived from the enum so the validation set stays in sync with the enum members.
_TOKEN_SOURCE_FIELDS = frozenset(s.value for s in TokenSource if s is not TokenSource.AMBIENT)
_KNOWN_PATCH_KEYS = frozenset(
    {"token_source", "git_user_name", "git_user_email", "gh_config_dir", "ssh_key_path"}
)


# ---------------------------------------------------------------------------
# YAML I/O helpers
# ---------------------------------------------------------------------------


def _config_path(project_path: Path) -> Path:
    return project_path / "agentshore.yaml"


def _load_yaml(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError("agentshore.yaml must be a mapping")
    return loaded


def _write_yaml_atomic(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".agentshore_", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(data, handle, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Source / fast listing helpers
# ---------------------------------------------------------------------------


def _source_for_identity(raw: dict[str, object]) -> tuple[str, str]:
    """Return token status/source for fast setup listing without I/O.

    The setup rail needs to list configured identities quickly. Deep token
    resolution (``gh auth token``, OS credential reads) and repo access checks
    are startup/runtime validation concerns; doing them here makes the Windows
    screen feel slow or frozen.
    """
    if isinstance(raw.get(TokenSource.LOGIN.value), str) and raw[TokenSource.LOGIN.value]:
        return "configured", TokenSource.LOGIN.value
    env_var = raw.get(TokenSource.ENV.value)
    if isinstance(env_var, str) and env_var:
        return ("configured" if os.environ.get(env_var) else "missing", TokenSource.ENV.value)
    if isinstance(raw.get(TokenSource.KEYCHAIN.value), str) and raw[TokenSource.KEYCHAIN.value]:
        return "configured", TokenSource.KEYCHAIN.value
    return "ambient", TokenSource.AMBIENT.value


def _apply_source(entry: dict[str, object], source: str, canonical: str) -> None:
    """Write the token-source field(s) for *source* into *entry* (in-place).

    Used by both ``add_identity`` and ``update_identity`` so the logic for
    mapping a token-source kind onto the corresponding YAML field lives in one place.
    """
    if source == TokenSource.LOGIN:
        entry["gh_token_login"] = canonical
    elif source == TokenSource.ENV:
        entry["gh_token_env"] = f"{canonical.upper().replace('-', '_')}_GH_TOKEN"
    else:
        entry["gh_token_keychain"] = keychain_service_for_login(canonical)


# ---------------------------------------------------------------------------
# Identity listing and non-validating CRUD
# ---------------------------------------------------------------------------


def list_identities(project_path: Path) -> list[IdentityRow]:
    cfg_path = _config_path(project_path)
    if not cfg_path.exists():
        return []
    data = _load_yaml(cfg_path)
    identities_raw = data.get("identities")
    if not isinstance(identities_raw, dict):
        return []
    rows: list[IdentityRow] = []
    for login_raw, ident_raw in sorted(identities_raw.items()):
        login = canonical_identity_name(str(login_raw))
        if not isinstance(ident_raw, dict):
            continue
        token_status, source = _source_for_identity(ident_raw)
        if source == "ambient":
            rows.append(
                {
                    "login": login,
                    "source": source,
                    "token_status": "ambient",
                    "repo_access": "unknown",
                }
            )
            continue
        if token_status != "configured":
            rows.append(
                {
                    "login": login,
                    "source": source,
                    "token_status": token_status,
                    "repo_access": "unknown",
                }
            )
            continue
        rows.append(
            {
                "login": login,
                "source": source,
                "token_status": "configured",
                "repo_access": "unknown",
            }
        )
    return rows


def update_identity(project_path: Path, login: str, patch: dict[str, object]) -> None:
    unknown = set(patch) - _KNOWN_PATCH_KEYS
    if unknown:
        raise ValueError(f"unknown patch keys: {sorted(unknown)}")
    canonical = canonical_identity_name(login)
    cfg_path = _config_path(project_path)
    data = _load_yaml(cfg_path)
    identities = data.get("identities")
    if not isinstance(identities, dict):
        raise ValueError("identities block must be a mapping")
    raw = identities.get(canonical)
    if not isinstance(raw, dict):
        raise ValueError(f"identity not found: {canonical}")

    next_raw = dict(raw)
    if "token_source" in patch:
        source = str(patch["token_source"]).strip()
        if source not in _TOKEN_SOURCE_FIELDS:
            raise ValueError(f"unsupported token_source: {source}")
        for key in _TOKEN_SOURCE_FIELDS:
            next_raw.pop(key, None)
        _apply_source(next_raw, source, canonical)
    for key in ("git_user_name", "git_user_email", "gh_config_dir", "ssh_key_path"):
        if key in patch and patch[key] is not None:
            next_raw[key] = patch[key]

    identities[canonical] = next_raw
    _write_yaml_atomic(cfg_path, data)


def remove_identity(project_path: Path, login: str) -> None:
    canonical = canonical_identity_name(login)
    cfg_path = _config_path(project_path)
    data = _load_yaml(cfg_path)
    identities = data.get("identities")
    if not isinstance(identities, dict):
        raise ValueError("identities block must be a mapping")
    if canonical not in identities:
        raise ValueError(f"identity not found: {canonical}")
    identities.pop(canonical, None)

    agents = data.get("agents")
    if isinstance(agents, dict):
        for _, body in agents.items():
            if (
                isinstance(body, dict)
                and canonical_identity_name(str(body.get("identity", ""))) == canonical
            ):
                body.pop("identity", None)

    _write_yaml_atomic(cfg_path, data)


# ---------------------------------------------------------------------------
# Trusted-source CRUD
# ---------------------------------------------------------------------------


def _trusted_ids_mapping(data: dict[str, object]) -> dict[str, object]:
    """Get-or-create the ``trusted_ids`` mapping inside *data* (in-place).

    Raises ``ValueError`` if a non-mapping ``trusted_ids`` is already present.
    """
    trusted = data.get("trusted_ids")
    if trusted is None:
        trusted = {}
        data["trusted_ids"] = trusted
    if not isinstance(trusted, dict):
        raise ValueError("trusted_ids block must be a mapping")
    return trusted


def _read_trusted_source_logins(trusted: dict[str, object]) -> list[str]:
    """Return the canonicalized, de-duplicated ``github_logins`` list."""
    raw = trusted.get("github_logins", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("trusted_ids.github_logins must be a list")
    logins: list[str] = []
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("trusted_ids.github_logins contains a non-string value")
        canonical = canonical_identity_name(value)
        if canonical not in seen:
            logins.append(canonical)
            seen.add(canonical)
    return logins


def list_trusted_sources(project_path: Path) -> list[str]:
    """List the no-auth trusted GitHub logins (``trusted_ids.github_logins``).

    These are identities trusted as *sources* of issues/PRs only — they are
    never assigned to an agent and carry no token. Returned sorted for a
    stable UI ordering.
    """
    cfg_path = _config_path(project_path)
    if not cfg_path.exists():
        return []
    data = _load_yaml(cfg_path)
    trusted_raw = data.get("trusted_ids")
    if not isinstance(trusted_raw, dict):
        return []
    return sorted(_read_trusted_source_logins(trusted_raw))


def add_trusted_source(project_path: Path, login: str) -> None:
    """Add *login* to ``trusted_ids.github_logins`` (idempotent).

    Validates the GitHub login and canonicalizes it. Leaves ``pr_allow_list``
    and ``restrict_issues_to_trusted_authors`` untouched.
    """
    if not is_valid_github_login(login):
        raise ValueError(f"invalid GitHub login: {login!r}")
    canonical = canonical_identity_name(login)
    cfg_path = _config_path(project_path)
    data = _load_yaml(cfg_path)
    trusted = _trusted_ids_mapping(data)
    logins = _read_trusted_source_logins(trusted)
    if canonical not in logins:
        logins.append(canonical)
        trusted["github_logins"] = logins
        _write_yaml_atomic(cfg_path, data)


def remove_trusted_source(project_path: Path, login: str) -> None:
    """Remove *login* from ``trusted_ids.github_logins`` (idempotent)."""
    canonical = canonical_identity_name(login)
    cfg_path = _config_path(project_path)
    data = _load_yaml(cfg_path)
    trusted_raw = data.get("trusted_ids")
    if not isinstance(trusted_raw, dict):
        return
    logins = _read_trusted_source_logins(trusted_raw)
    updated = [item for item in logins if item != canonical]
    if updated != logins:
        trusted_raw["github_logins"] = updated
        _write_yaml_atomic(cfg_path, data)
