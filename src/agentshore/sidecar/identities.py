"""Identity RPC helpers for the desktop sidecar."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, NotRequired, TypedDict

import yaml

from agentshore import subprocess_env
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


class _KeyringTimeoutError(RuntimeError):
    """Raised when the local OS credential backend does not answer promptly."""


@dataclass(frozen=True)
class _CredentialResolution:
    token: str | None
    status: str
    detail: str


def _resolve_github_login_for_token(token: str) -> str | None:
    token = _clean_token(token) or ""
    return resolve_github_login_for_token(token)


def _clean_token(token: str | None) -> str | None:
    if token is None:
        return None
    cleaned = "".join(ch for ch in token if ch.isprintable()).strip()
    return cleaned or None


_KEYRING_CHILD_CODE = r"""
import json
import sys

request = json.loads(sys.stdin.read() or "{}")
try:
    import keyring

    op = request.get("op")
    service = str(request.get("service") or "")
    if op == "get":
        token = keyring.get_password(service, service)
        print(json.dumps({"ok": True, "token": token}))
    elif op == "set":
        keyring.set_password(service, service, str(request.get("token") or ""))
        print(json.dumps({"ok": True}))
    elif op == "backend":
        print(json.dumps({"ok": True, "backend": type(keyring.get_keyring()).__name__}))
    else:
        print(json.dumps({"ok": False, "error": f"unsupported op: {op}"}))
except Exception as exc:
    print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
"""


def _run_keyring_child(request: dict[str, object]) -> dict[str, object]:
    """Run one OS credential operation in a killable child process.

    Keyring backends can wedge inside OS credential discovery. A daemon thread
    can return a timeout to the UI, but the backend call keeps running inside
    the sidecar forever. A subprocess gives the timeout real teeth: Python kills
    and reaps the child when ``subprocess.run(..., timeout=...)`` expires.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - same Python executable, fixed -c payload
            [sys.executable, "-c", _KEYRING_CHILD_CODE],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            check=False,
            timeout=subprocess_env.timeout_for("keyring"),
            creationflags=subprocess_env.no_window_creationflags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise _KeyringTimeoutError("keyring operation timed out") from exc
    except OSError as exc:
        raise RuntimeError(f"keyring child failed to start: {exc}") from exc

    try:
        payload = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("keyring child returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("keyring child returned non-object JSON")
    if payload.get("ok") is True:
        return payload
    error = payload.get("error")
    raise RuntimeError(str(error or "keyring operation failed"))


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
    actual_login = _resolve_github_login_for_token(token)
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


def _keyring_get(service: str) -> str | None:
    """Read a stored OS credential for *service*, returning ``None`` on failure.

    The keyring import and backend call both run behind a short timeout because
    Windows Credential Manager and third-party keyring providers may stall while
    discovering the active desktop session.
    """
    try:
        result = _run_keyring_child({"op": "get", "service": service})
        token = result.get("token")
        return token if isinstance(token, str) else None
    except Exception:
        return None


def _keychain_has_token(service: str) -> bool:
    """Return True when the OS credential store has a non-empty token for *service*."""
    token = _keyring_get(service)
    return bool(token and token.strip())


def _keyring_set_password(service: str, pat: str) -> None:
    """Store *pat* in the OS credential store without risking a hung setup RPC."""
    try:
        _run_keyring_child({"op": "set", "service": service, "token": pat})
    except _KeyringTimeoutError as exc:
        raise ValueError(f"credential store did not respond in time: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"failed to store PAT in Keychain/Credential Manager: {exc}") from exc


def keychain_status(login: str) -> dict[str, object]:
    """Report whether an AgentShore-managed Keychain PAT already exists for *login*.

    Mirrors the CLI wizard's pre-flight check (``identity_wizard.keychain._keychain_has_token``)
    so the desktop "Add identity" form can offer to reuse a stored PAT instead of
    forcing the user to paste it again. Returns the canonical login, the managed
    keychain service name, and whether a non-empty token is present there.
    """
    if not is_valid_github_login(login):
        raise ValueError(f"invalid GitHub login: {login!r}")
    canonical = canonical_identity_name(login)
    service = keychain_service_for_login(canonical)
    return {
        "login": canonical,
        "service": service,
        "has_token": _keychain_has_token(service),
    }


def _resolve_login_token(raw: dict[str, object]) -> str | None:
    """Resolve a token via ``gh auth token`` for the configured GitHub login."""
    return _resolve_login_auth(raw).token


def _resolve_login_auth(raw: dict[str, object]) -> _CredentialResolution:
    """Resolve GitHub CLI auth for the configured login.

    This is separate from PAT/keychain token handling because setup should
    surface gh-auth problems as gh-auth problems, not as generic missing tokens.
    """
    expected_login = canonical_identity_name(str(raw["gh_token_login"]))
    gh_config_dir = raw.get("gh_config_dir")
    overlay = (
        {"GH_CONFIG_DIR": gh_config_dir}
        if isinstance(gh_config_dir, str) and gh_config_dir
        else None
    )
    env = subprocess_env.hardened_env(overlay, for_gh=True)
    gh_bin = subprocess_env.resolve_tool("gh")
    if gh_bin is None:
        return _CredentialResolution(
            token=None,
            status="auth_missing",
            detail="GitHub CLI auth could not be checked because gh.exe is not on PATH.",
        )
    try:
        proc = subprocess.run(  # noqa: S603 — resolved absolute path
            [gh_bin, "auth", "token", "-h", "github.com", "-u", str(raw["gh_token_login"])],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=subprocess_env.timeout_for("gh"),
            creationflags=subprocess_env.no_window_creationflags(),
        )
    except subprocess.TimeoutExpired:
        return _CredentialResolution(
            token=None,
            status="auth_timeout",
            detail=(f"GitHub CLI auth lookup timed out while resolving {raw['gh_token_login']!r}."),
        )
    except OSError as exc:
        return _CredentialResolution(
            token=None,
            status="auth_error",
            detail=f"GitHub CLI auth lookup failed: {exc}",
        )
    token = _clean_token(proc.stdout) if proc.returncode == 0 else None
    if token:
        return _CredentialResolution(
            token=token,
            status="auth_ok",
            detail=f"GitHub CLI auth resolved for {raw['gh_token_login']}.",
        )

    try:
        fallback = subprocess.run(  # noqa: S603 — resolved absolute path
            [gh_bin, "auth", "token", "-h", "github.com"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=subprocess_env.timeout_for("gh"),
            creationflags=subprocess_env.no_window_creationflags(),
        )
    except subprocess.TimeoutExpired:
        return _CredentialResolution(
            token=None,
            status="auth_timeout",
            detail=(
                "GitHub CLI active-auth lookup timed out after the configured "
                f"login {raw['gh_token_login']!r} did not return a token."
            ),
        )
    except OSError as exc:
        return _CredentialResolution(
            token=None,
            status="auth_error",
            detail=f"GitHub CLI active-auth lookup failed: {exc}",
        )
    token = _clean_token(fallback.stdout) if fallback.returncode == 0 else None
    if not token:
        detail = (proc.stderr or fallback.stderr or "gh auth token returned no token").strip()
        return _CredentialResolution(
            token=None,
            status="auth_missing",
            detail=(f"GitHub CLI auth could not resolve {raw['gh_token_login']!r}: {detail[:500]}"),
        )
    actual_login = _resolve_github_login_for_token(token)
    if actual_login and canonical_identity_name(actual_login) == expected_login:
        return _CredentialResolution(
            token=token,
            status="auth_ok",
            detail=(
                f"GitHub CLI active auth matched the configured login {raw['gh_token_login']}."
            ),
        )
    if actual_login:
        return _CredentialResolution(
            token=None,
            status="auth_mismatch",
            detail=(
                f"GitHub CLI active auth belongs to {actual_login!r}, "
                f"not {raw['gh_token_login']!r}."
            ),
        )
    return _CredentialResolution(
        token=None,
        status="auth_missing",
        detail=(
            "GitHub CLI active auth produced a token, but AgentShore could not "
            f"confirm that it belongs to {raw['gh_token_login']!r}."
        ),
    )


# Per-source token resolver: dict[TokenSource, callable(raw) -> str | None].
# Each callable receives the raw identity dict and returns the resolved token or None.
_TOKEN_RESOLVERS: dict[TokenSource, object] = {
    TokenSource.LOGIN: _resolve_login_token,
    TokenSource.ENV: lambda raw: os.environ.get(str(raw["gh_token_env"])),
    TokenSource.KEYCHAIN: lambda raw: _keyring_get(str(raw["gh_token_keychain"])) or None,
}


def _token_for_identity(raw: dict[str, object]) -> tuple[str | None, str]:
    for source in (TokenSource.LOGIN, TokenSource.ENV, TokenSource.KEYCHAIN):
        if isinstance(raw.get(source.value), str) and raw[source.value]:
            resolver = _TOKEN_RESOLVERS[source]
            token = _clean_token(resolver(raw))  # type: ignore[operator]
            return token, source.value
    return None, TokenSource.AMBIENT.value


def _source_for_identity(raw: dict[str, object]) -> tuple[str, str]:
    """Return token status/source for fast setup listing without I/O.

    The setup rail needs to list configured identities quickly. Deep token
    resolution (`gh auth token`, OS credential reads) and repo access checks are
    startup/runtime validation concerns; doing them here makes the Windows
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


async def check_identity_access(project_path: Path, login: str) -> IdentityRow:
    """Resolve one configured identity and verify its token can see the repo.

    This is intentionally separate from ``list_identities`` so the setup screen
    can render from YAML immediately, then update live access badges without
    making first paint depend on ``gh`` or the OS credential backend.

    Runs the blocking gh / keyring / repo-access calls inside a thread so the
    serve loop stays pumping while concurrent identity checks complete in
    parallel.  A monotonic deadline (``timeout_for("identity_check")``) caps
    the total time — every individual operation already has its own inner
    timeout, so this outer cap is a safety net only.
    """
    canonical = canonical_identity_name(login)
    cfg_path = _config_path(project_path)
    data = _load_yaml(cfg_path)
    identities_raw = data.get("identities")
    if not isinstance(identities_raw, dict):
        raise ValueError("identities block must be a mapping")
    raw = identities_raw.get(canonical)
    if not isinstance(raw, dict):
        raise ValueError(f"identity not found: {canonical}")

    token_status, source = _source_for_identity(raw)
    row: IdentityRow = {
        "login": canonical,
        "source": source,
        "token_status": token_status,
        "repo_access": "unknown",
    }
    if source == TokenSource.AMBIENT:
        row["token_status"] = "ambient"
        row["repo_access_detail"] = "Ambient gh authentication is verified when the agent starts."
        return row
    if token_status != "configured":
        row["repo_access_detail"] = _unconfigured_source_message(source)
        return row

    def _run_check() -> IdentityRow:
        if source == TokenSource.LOGIN.value:
            return _check_gh_auth_identity_access(project_path, raw, row)
        return _check_token_identity_access(project_path, raw, row, source)

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_run_check),
            timeout=subprocess_env.timeout_for("identity_check"),
        )
    except TimeoutError:
        timed_out: IdentityRow = dict(row)  # type: ignore[assignment]
        timed_out["repo_access"] = "check_failed"
        timed_out["token_status"] = (
            "auth_timeout" if source == TokenSource.LOGIN.value else "token_timeout"
        )
        timed_out["repo_access_detail"] = _with_identity_diagnostics(
            _timeout_message(source),
            raw,
            source,
            include_gh_status=False,
        )
        return timed_out


def _check_gh_auth_identity_access(
    project_path: Path,
    raw: dict[str, object],
    row: IdentityRow,
) -> IdentityRow:
    checked: IdentityRow = dict(row)  # type: ignore[assignment]
    resolution = _resolve_login_auth(raw)
    if not resolution.token:
        checked["token_status"] = resolution.status
        checked["repo_access_detail"] = _with_identity_diagnostics(
            resolution.detail,
            raw,
            TokenSource.LOGIN.value,
        )
        return checked

    checked["token_status"] = "auth_ok"
    try:
        verify_identity_repo_access(
            project_path,
            {"GH_TOKEN": resolution.token, "GITHUB_TOKEN": resolution.token},
        )
    except (IdentityResolutionError, AgentAuthError) as exc:
        checked["repo_access"] = "blocked"
        checked["repo_access_detail"] = _with_identity_diagnostics(
            str(exc) or "GitHub repository access preflight failed.",
            raw,
            TokenSource.LOGIN.value,
        )
    else:
        checked["repo_access"] = "ok"
        checked["repo_access_detail"] = "GitHub CLI auth and repository access verified."
    return checked


def _check_token_identity_access(
    project_path: Path,
    raw: dict[str, object],
    row: IdentityRow,
    source: str,
) -> IdentityRow:
    checked: IdentityRow = dict(row)  # type: ignore[assignment]
    token, _resolved_source = _token_for_identity(raw)
    if not token:
        checked["token_status"] = "missing"
        checked["repo_access_detail"] = _with_identity_diagnostics(
            f"Token could not be resolved from {source}.",
            raw,
            source,
        )
        return checked

    checked["token_status"] = "configured"
    try:
        verify_identity_repo_access(project_path, {"GH_TOKEN": token, "GITHUB_TOKEN": token})
    except (IdentityResolutionError, AgentAuthError) as exc:
        checked["repo_access"] = "blocked"
        checked["repo_access_detail"] = _with_identity_diagnostics(
            str(exc) or "GitHub repository access preflight failed.",
            raw,
            source,
        )
    else:
        checked["repo_access"] = "ok"
        checked["repo_access_detail"] = "GitHub token and repository access verified."
    return checked



def _unconfigured_source_message(source: str) -> str:
    if source == TokenSource.LOGIN.value:
        return "GitHub CLI auth login is not configured in this desktop process."
    return "Token source is not configured in this desktop process."


def _timeout_message(source: str) -> str:
    secs = subprocess_env.timeout_for("identity_check")
    if source == TokenSource.LOGIN.value:
        return (
            "GitHub CLI auth and repository access verification timed out after "
            f"{secs:.0f}s."
        )
    return (
        "GitHub token and repository access verification timed out after "
        f"{secs:.0f}s."
    )


def _with_identity_diagnostics(
    message: str,
    raw: dict[str, object],
    source: str,
    *,
    include_gh_status: bool = True,
) -> str:
    diagnostics = _identity_diagnostics(raw, source, include_gh_status=include_gh_status)
    return f"{message} Diagnostics: {diagnostics}" if diagnostics else message


def _identity_diagnostics(
    raw: dict[str, object],
    source: str,
    *,
    include_gh_status: bool = True,
) -> str:
    """Return redacted desktop environment hints for setup-screen failures."""
    parts = [
        f"python={sys.executable}",
        f"gh={subprocess_env.resolve_tool('gh') or '<missing>'}",
        f"GH_CONFIG_DIR={_gh_env(raw).get('GH_CONFIG_DIR', '<unset>')}",
    ]
    for key in ("APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        parts.append(f"{key}={'set' if os.environ.get(key) else 'missing'}")
    if source == TokenSource.KEYCHAIN.value:
        parts.append(f"keyring={_keyring_backend_name()}")
    if include_gh_status:
        status = _gh_auth_status_summary(raw)
        if status:
            parts.append(f"gh_status={status}")
    return "; ".join(parts)


def _gh_env(raw: dict[str, object]) -> dict[str, str]:
    gh_config_dir = raw.get("gh_config_dir")
    overlay = (
        {"GH_CONFIG_DIR": gh_config_dir}
        if isinstance(gh_config_dir, str) and gh_config_dir
        else None
    )
    return subprocess_env.hardened_env(overlay, for_gh=True)


def _keyring_backend_name() -> str:
    try:
        result = _run_keyring_child({"op": "backend"})
        backend = result.get("backend")
        return backend if isinstance(backend, str) and backend else "unknown"
    except Exception as exc:  # pragma: no cover - backend discovery varies by OS
        return f"unavailable ({type(exc).__name__})"


def _gh_auth_status_summary(raw: dict[str, object]) -> str:
    gh_bin = subprocess_env.resolve_tool("gh")
    if gh_bin is None:
        return "gh missing"
    try:
        proc = subprocess.run(  # noqa: S603 — resolved absolute path
            [gh_bin, "auth", "status", "-h", "github.com"],
            capture_output=True,
            text=True,
            check=False,
            env=_gh_env(raw),
            timeout=subprocess_env.timeout_for("gh"),
            creationflags=subprocess_env.no_window_creationflags(),
        )
    except subprocess.TimeoutExpired:
        return "timed out"
    except OSError as exc:
        return f"failed ({type(exc).__name__})"
    output = " ".join((proc.stdout or proc.stderr or "").split())
    if len(output) > 240:
        output = f"{output[:237]}..."
    return f"exit={proc.returncode}; {output or '<no output>'}"


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
    _apply_source(entry, source, canonical)
    if source == TokenSource.KEYCHAIN and pat:
        service = keychain_service_for_login(canonical)
        _keyring_set_password(service, pat)
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
        _apply_source(next_raw, source, canonical)
    for key in ("git_user_name", "git_user_email", "gh_config_dir", "ssh_key_path"):
        if key in patch and patch[key] is not None:
            next_raw[key] = patch[key]

    identities[canonical] = next_raw
    _write_yaml_atomic(cfg_path, data)


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
