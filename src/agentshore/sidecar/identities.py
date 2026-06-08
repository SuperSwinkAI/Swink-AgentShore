"""Identity RPC helpers for the desktop sidecar."""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import tempfile
import threading
from enum import StrEnum
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

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
    from collections.abc import Callable
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
_GH_TOKEN_TIMEOUT_SECONDS = 10.0
_KEYRING_TIMEOUT_SECONDS = 5.0
_KEYRING_SLOTS = threading.BoundedSemaphore(value=2)


class _KeyringTimeoutError(RuntimeError):
    """Raised when the local OS credential backend does not answer promptly."""


def _run_keyring_operation[T](operation: Callable[[], T]) -> T:
    """Run an OS credential operation without letting it block the sidecar.

    Windows Credential Manager and third-party keyring backends can occasionally
    stall while probing desktop/session state. Running the operation in a
    bounded daemon thread lets the RPC return a conservative answer instead of
    pinning the setup screen until Tauri's outer sidecar timeout fires.
    """
    if not _KEYRING_SLOTS.acquire(blocking=False):
        raise _KeyringTimeoutError("another keyring operation is still running")

    result_queue: queue.Queue[object] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            result_queue.put(operation())
        except Exception as exc:  # pragma: no cover - exact backend exception varies by OS
            result_queue.put(exc)
        finally:
            _KEYRING_SLOTS.release()

    thread = threading.Thread(target=worker, name="agentshore-keyring", daemon=True)
    thread.start()
    thread.join(_KEYRING_TIMEOUT_SECONDS)
    if thread.is_alive():
        raise _KeyringTimeoutError("keyring operation timed out")
    try:
        result = result_queue.get_nowait()
    except queue.Empty as exc:
        raise _KeyringTimeoutError("keyring operation returned no result") from exc
    if isinstance(result, Exception):
        raise result
    return cast("T", result)


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


def _keyring_get(service: str) -> str | None:
    """Read a stored OS credential for *service*, returning ``None`` on failure.

    The keyring import and backend call both run behind a short timeout because
    Windows Credential Manager and third-party keyring providers may stall while
    discovering the active desktop session.
    """

    def operation() -> str | None:
        import keyring  # local import: keyring backends can do OS setup at import time

        return keyring.get_password(service, service)

    try:
        return _run_keyring_operation(operation)
    except Exception:
        return None


def _keychain_has_token(service: str) -> bool:
    """Return True when the OS credential store has a non-empty token for *service*."""
    token = _keyring_get(service)
    return bool(token and token.strip())


def _keyring_set_password(service: str, pat: str) -> None:
    """Store *pat* in the OS credential store without risking a hung setup RPC."""

    def operation() -> None:
        import keyring  # local import: keyring backends can do OS setup at import time

        keyring.set_password(service, service, pat)

    try:
        _run_keyring_operation(operation)
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
    env = os.environ.copy()
    gh_config_dir = raw.get("gh_config_dir")
    if isinstance(gh_config_dir, str) and gh_config_dir:
        env["GH_CONFIG_DIR"] = gh_config_dir
    gh_bin = shutil.which("gh")
    if gh_bin is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 — resolved absolute path
            [gh_bin, "auth", "token", "--user", str(raw["gh_token_login"])],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=_GH_TOKEN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


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
            token = resolver(raw)  # type: ignore[operator]
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


def check_identity_access(project_path: Path, login: str) -> IdentityRow:
    """Resolve one configured identity and verify its token can see the repo.

    This is intentionally separate from ``list_identities`` so the setup screen
    can render from YAML immediately, then update live access badges without
    making first paint depend on ``gh`` or the OS credential backend.
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
        row["repo_access_detail"] = "Token source is not configured in this desktop process."
        return row

    token, _resolved_source = _token_for_identity(raw)
    if not token:
        row["token_status"] = "missing"
        row["repo_access_detail"] = f"Token could not be resolved from {source}."
        return row

    row["token_status"] = "configured"
    try:
        verify_identity_repo_access(project_path, {"GH_TOKEN": token, "GITHUB_TOKEN": token})
    except (IdentityResolutionError, AgentAuthError) as exc:
        row["repo_access"] = "blocked"
        row["repo_access_detail"] = str(exc) or "GitHub repository access preflight failed."
    else:
        row["repo_access"] = "ok"
        row["repo_access_detail"] = "GitHub repository access verified."
    return row


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
