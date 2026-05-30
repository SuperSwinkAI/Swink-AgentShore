"""Identity RPC helpers for the desktop sidecar."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import TYPE_CHECKING, TypedDict

import yaml

from agentshore.agents.identity import IdentityResolutionError, verify_identity_repo_access
from agentshore.errors import AgentAuthError
from agentshore.identity_names import (
    canonical_identity_name,
    is_valid_github_login,
    keychain_service_for_login,
    resolve_github_login_for_token,
)

if TYPE_CHECKING:
    from pathlib import Path


class IdentityRow(TypedDict):
    login: str
    source: str
    token_status: str
    repo_access: str


_TOKEN_SOURCE_FIELDS = frozenset({"gh_token_login", "gh_token_env", "gh_token_keychain"})
_KNOWN_PATCH_KEYS = frozenset(
    {"token_source", "git_user_name", "git_user_email", "gh_config_dir", "ssh_key_path"}
)
def _validate_token_matches_login(entry: dict[str, object], expected_login: str) -> None:
    """Resolve the token from *entry* and verify it belongs to *expected_login*.

    Raises ``ValueError`` with a corrective message when the token resolves
    to a different GitHub account. Skips validation silently when the token
    cannot be resolved (e.g. env var unset, gh CLI missing) — those
    failures surface later at session start.
    """
    token, _source = _token_for_identity(entry)
    if token is None:
        return
    actual_login = resolve_github_login_for_token(token)
    if actual_login is None:
        return
    if canonical_identity_name(actual_login) != canonical_identity_name(expected_login):
        raise ValueError(
            f"token belongs to GitHub user {actual_login!r}, "
            f"not {expected_login!r} — check the login for typos"
        )


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


def _token_for_identity(raw: dict[str, object]) -> tuple[str | None, str]:
    if isinstance(raw.get("gh_token_login"), str) and raw["gh_token_login"]:
        env = os.environ.copy()
        gh_config_dir = raw.get("gh_config_dir")
        if isinstance(gh_config_dir, str) and gh_config_dir:
            env["GH_CONFIG_DIR"] = gh_config_dir
        gh_bin = shutil.which("gh")
        if gh_bin is None:
            return None, "gh_token_login"
        try:
            proc = subprocess.run(  # noqa: S603 — resolved absolute path
                [gh_bin, "auth", "token", "--user", str(raw["gh_token_login"])],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
        except OSError:
            return None, "gh_token_login"
        if proc.returncode != 0:
            return None, "gh_token_login"
        token = proc.stdout.strip()
        return (token or None), "gh_token_login"
    if isinstance(raw.get("gh_token_env"), str) and raw["gh_token_env"]:
        return os.environ.get(str(raw["gh_token_env"])), "gh_token_env"
    if isinstance(raw.get("gh_token_keychain"), str) and raw["gh_token_keychain"]:
        service = str(raw["gh_token_keychain"])
        try:
            import keyring  # local import: keyring's macOS backend runs setup at import time
            from keyring.errors import KeyringError
        except Exception:
            return None, "gh_token_keychain"
        try:
            keychain_token: str | None = keyring.get_password(service, service)
        except KeyringError:
            return None, "gh_token_keychain"
        except Exception:
            return None, "gh_token_keychain"
        return (keychain_token or None), "gh_token_keychain"
    return None, "ambient"


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
        token, source = _token_for_identity(ident_raw)
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
        if not token:
            rows.append(
                {
                    "login": login,
                    "source": source,
                    "token_status": "missing",
                    "repo_access": "unknown",
                }
            )
            continue
        try:
            verify_identity_repo_access(project_path, {"GH_TOKEN": token, "GITHUB_TOKEN": token})
            repo_access = "ok"
        except (IdentityResolutionError, AgentAuthError):
            repo_access = "blocked"
        rows.append(
            {
                "login": login,
                "source": source,
                "token_status": "configured",
                "repo_access": repo_access,
            }
        )
    return rows


def add_identity(
    project_path: Path,
    login: str,
    token_source: str,
    *,
    pat: str | None = None,
) -> None:
    source = token_source.strip()
    if not is_valid_github_login(login):
        raise ValueError(f"invalid GitHub login: {login!r}")
    canonical = canonical_identity_name(login)
    if source not in _TOKEN_SOURCE_FIELDS:
        raise ValueError(f"unsupported token_source: {token_source}")
    cfg_path = _config_path(project_path)
    data = _load_yaml(cfg_path)
    identities = data.get("identities")
    if identities is None:
        identities = {}
        data["identities"] = identities
    if not isinstance(identities, dict):
        raise ValueError("identities block must be a mapping")
    if canonical in identities:
        raise ValueError(f"identity already exists: {canonical}")

    entry: dict[str, object] = {
        "git_user_name": login,
        "git_user_email": f"{canonical}@users.noreply.github.com",
    }
    if source == "gh_token_login":
        entry["gh_token_login"] = canonical
    elif source == "gh_token_env":
        entry["gh_token_env"] = f"{canonical.upper().replace('-', '_')}_GH_TOKEN"
    else:
        service = keychain_service_for_login(canonical)
        entry["gh_token_keychain"] = service
        if pat:
            try:
                import keyring  # local import: keyring's macOS backend runs setup at import time
                from keyring.errors import KeyringError
            except Exception as exc:
                raise ValueError(f"keyring package unavailable: {exc}") from exc
            try:
                keyring.set_password(service, service, pat)
            except KeyringError as exc:
                raise ValueError(f"failed to store PAT in Keychain: {exc}") from exc
    _validate_token_matches_login(entry, login)
    identities[canonical] = entry
    _write_yaml_atomic(cfg_path, data)


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
        if source == "gh_token_login":
            next_raw["gh_token_login"] = canonical
        elif source == "gh_token_env":
            next_raw["gh_token_env"] = f"{canonical.upper().replace('-', '_')}_GH_TOKEN"
        else:
            next_raw["gh_token_keychain"] = keychain_service_for_login(canonical)
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
