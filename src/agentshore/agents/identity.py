"""Resolve a per-agent GitHub identity into a subprocess env overlay.

The overlay is layered on top of ``os.environ`` when dispatching CLI agent
subprocesses (Claude Code, Codex, Gemini).

Token sources, in priority order:

1. ``gh_token_env``   — name of an env var holding a PAT.
2. ``gh_token_login`` — looked up at runtime via ``gh auth token -u <login>``.
3. ``gh_token_keychain`` — looked up in the OS credential store.

If all are unset, no token is injected and the agent inherits the user's
ambient ``gh`` auth (the active account in ``gh auth status``).
"""

from __future__ import annotations

import os
import subprocess  # nosec B404
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from agentshore.environment import resolve_executable
from agentshore.errors import AgentAuthError, OrchestratorError
from agentshore.identity_names import (
    canonical_identity_name,
    canonical_keychain_service,
    login_from_agentshore_keychain_service,
    same_identity,
)
from agentshore.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agentshore.config import AgentConfig, GitHubIdentity, RuntimeConfig

_logger = get_logger(__name__)

_SOURCE_NONE = "none"
_SOURCE_ENV = "env"
_SOURCE_AMBIENT = "ambient"
_SOURCE_GH_LOGIN = "gh_login"
# Token sources that carry no per-identity secret and therefore can never be
# "configured but failed validation" — they inherit ambient gh auth instead.
_NEUTRAL_TOKEN_SOURCES = frozenset({_SOURCE_AMBIENT, _SOURCE_NONE})
_REPO_ACCESS_TIMEOUT_SECONDS = 10


class IdentityResolutionError(OrchestratorError):
    """Raised when a configured identity cannot supply a validated GitHub token."""


@dataclass(frozen=True)
class _TokenResolution:
    source: str
    token: str | None
    detail: str
    # True only when GitHub actually accepted this token (strict resolution).
    # A non-strict resolution skips the live check, so it stays False rather
    # than asserting an invariant the code never verified.
    token_validated: bool = False
    resolved_login: str | None = None
    validation_error: str | None = None
    canonical_service: str | None = None


def _expanded_gh_config_dir(gh_config_dir: str | None) -> str | None:
    return os.path.expanduser(gh_config_dir) if gh_config_dir else None


def _keychain_services(service: str) -> list[str]:
    configured = service.strip()
    services = [configured]
    corrected = canonical_keychain_service(configured)
    if corrected != configured:
        services.append(corrected)
    return services


def _expected_login_from_keychain_service(service: str) -> str | None:
    return login_from_agentshore_keychain_service(service)


def configured_github_login_from_fields(
    *,
    ident_name: str,
    gh_token_login: str | None = None,
    gh_token_env: str | None = None,
    gh_token_keychain: str | None = None,
) -> str | None:
    """Return the configured GitHub login for identity fields, without validation."""
    if gh_token_login:
        return canonical_identity_name(gh_token_login)
    if gh_token_keychain:
        login = login_from_agentshore_keychain_service(gh_token_keychain)
        return canonical_identity_name(login) if login else None
    if gh_token_env:
        return canonical_identity_name(ident_name)
    return None


def configured_github_login_for_identity(ident: GitHubIdentity, ident_name: str) -> str | None:
    """Return the configured GitHub login for an identity, without validation."""
    return configured_github_login_from_fields(
        ident_name=ident_name,
        gh_token_login=ident.gh_token_login,
        gh_token_env=ident.gh_token_env,
        gh_token_keychain=ident.gh_token_keychain,
    )


def _isolated_gh_config_dir(identity_name: str) -> Path:
    from platformdirs import user_data_dir

    base = Path(user_data_dir("agentshore")) / "identities" / identity_name / "gh"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base


# ---------------------------------------------------------------------------
# IdentityResolver — owns token caches and the resolution pipeline
# ---------------------------------------------------------------------------


class IdentityResolver:
    """Resolves GitHub identity tokens from configured sources (env, gh CLI,
    keychain) with per-instance caching. Owns the four token caches that were
    previously module-level globals, making test isolation cleaner.
    """

    def __init__(self) -> None:
        self._token_cache: dict[tuple[str, str | None], str] = {}
        self._keychain_cache: dict[str, str] = {}
        self._validation_cache: dict[str, tuple[bool, str | None, str | None]] = {}
        self._repo_access_cache: set[tuple[str, str]] = set()

    def reset_caches(self) -> None:
        self._token_cache.clear()
        self._keychain_cache.clear()
        self._validation_cache.clear()
        self._repo_access_cache.clear()

    def read_gh_token_for_login(self, login: str, gh_config_dir: str | None = None) -> str | None:
        expanded_config_dir = _expanded_gh_config_dir(gh_config_dir)
        cache_key = (canonical_identity_name(login), expanded_config_dir)
        if cache_key in self._token_cache:
            return self._token_cache[cache_key]

        gh_path = resolve_executable("gh")
        if gh_path is None:
            _logger.warning("identity_gh_cli_missing", login=login)
            return None

        env = os.environ.copy()
        if expanded_config_dir:
            env["GH_CONFIG_DIR"] = expanded_config_dir

        try:
            result = subprocess.run(  # nosec B603
                [gh_path, "auth", "token", "-h", "github.com", "-u", login],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
                env=env,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            _logger.warning(
                "identity_gh_token_lookup_failed",
                login=login,
                error=str(exc),
            )
            return None

        token = result.stdout.strip()
        if not token:
            _logger.warning("identity_gh_token_empty", login=login)
            return None

        self._token_cache[cache_key] = token
        return token

    def read_keychain_token(self, service: str) -> str | None:
        if service in self._keychain_cache:
            return self._keychain_cache[service]

        try:
            import keyring
            from keyring.errors import KeyringError
        except ImportError:
            _logger.warning("identity_keyring_unavailable", service=service)
            return None

        try:
            token = keyring.get_password(service, service)
        except KeyringError as exc:
            _logger.warning(
                "identity_keychain_lookup_failed",
                service=service,
                error=str(exc),
            )
            return None

        if not token:
            return None

        self._keychain_cache[service] = token
        return token

    def validate_github_token(self, token: str) -> tuple[bool, str | None, str | None]:
        if token in self._validation_cache:
            return self._validation_cache[token]

        validation: tuple[bool, str | None, str | None]
        gh_path = resolve_executable("gh")
        if gh_path is None:
            validation = (False, None, "gh CLI not found for token validation")
            self._validation_cache[token] = validation
            return validation

        env = {**os.environ, "GH_TOKEN": token, "GITHUB_TOKEN": token}
        try:
            completed = subprocess.run(  # nosec B603
                [gh_path, "api", "user", "--jq", ".login"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
                env=env,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            validation = (False, None, str(exc))
            self._validation_cache[token] = validation
            return validation

        if completed.returncode != 0:
            err = (completed.stderr or completed.stdout or "GitHub rejected token").strip()
            validation = (False, None, err[:500])
            self._validation_cache[token] = validation
            return validation

        login = completed.stdout.strip()
        if not login:
            validation = (False, None, "GitHub token validation returned no login")
            self._validation_cache[token] = validation
            return validation

        validation = (True, login, None)
        self._validation_cache[token] = validation
        return validation

    def validate_resolution(
        self,
        *,
        source: str,
        token: str | None,
        detail: str,
        expected_login: str | None = None,
        canonical_service: str | None = None,
        require_expected_login: bool = False,
        validate: bool,
    ) -> _TokenResolution:
        if token is None:
            return _TokenResolution(
                source=source,
                token=None,
                detail=detail,
                validation_error=detail,
                canonical_service=canonical_service,
            )
        if not validate:
            # Not validated against GitHub: carry the token through but do not
            # claim it is valid. ``resolved_login`` keeps the *configured*
            # expectation so non-strict callers still get a usable overlay.
            return _TokenResolution(
                source=source,
                token=token,
                detail=detail,
                token_validated=False,
                resolved_login=expected_login,
                canonical_service=canonical_service,
            )
        if require_expected_login and expected_login is None:
            return _TokenResolution(
                source=source,
                token=token,
                detail=detail,
                token_validated=False,
                validation_error=(
                    f"{source} token has no configured GitHub login to validate against"
                ),
                canonical_service=canonical_service,
            )

        valid, login, error = self.validate_github_token(token)
        if valid and expected_login and login and not same_identity(login, expected_login):
            valid = False
            error = f"token resolved to GitHub login {login!r}, expected {expected_login!r}"
        detail_with_login = f"{detail} ({login})" if valid and login else detail
        return _TokenResolution(
            source=source,
            token=token,
            detail=detail_with_login,
            token_validated=valid,
            resolved_login=canonical_identity_name(login) if valid and login else None,
            validation_error=error,
            canonical_service=canonical_service,
        )

    def resolve_token_details(
        self,
        name: str,
        ident: GitHubIdentity,
        *,
        validate: bool,
    ) -> _TokenResolution:
        if ident.gh_token_env:
            expected_login = configured_github_login_for_identity(ident, name)
            token = os.environ.get(ident.gh_token_env)
            if not token:
                detail = f"env var {ident.gh_token_env} is unset"
                _logger.warning(
                    "identity_token_env_unset",
                    identity=name,
                    var=ident.gh_token_env,
                )
                return self.validate_resolution(
                    source="env",
                    token=None,
                    detail=detail,
                    expected_login=expected_login,
                    validate=validate,
                )
            return self.validate_resolution(
                source="env",
                token=token,
                detail=f"env var {ident.gh_token_env}",
                expected_login=expected_login,
                require_expected_login=True,
                validate=validate,
            )

        if ident.gh_token_login:
            token = self.read_gh_token_for_login(ident.gh_token_login, ident.gh_config_dir)
            detail = (
                f"gh auth token -u {ident.gh_token_login}"
                if token is not None
                else f"gh auth token -u {ident.gh_token_login} returned no token"
            )
            return self.validate_resolution(
                source="gh_login",
                token=token,
                detail=detail,
                expected_login=ident.gh_token_login,
                validate=validate,
            )

        if ident.gh_token_keychain:
            configured_service = ident.gh_token_keychain
            first_failure: str | None = None
            for service in _keychain_services(configured_service):
                token = self.read_keychain_token(service)
                if token is None:
                    first_failure = first_failure or f"keychain {service} has no entry"
                    continue
                expected_login = _expected_login_from_keychain_service(service)
                resolution = self.validate_resolution(
                    source="keychain",
                    token=token,
                    detail=f"keychain {service}",
                    expected_login=expected_login,
                    require_expected_login=True,
                    canonical_service=service,
                    validate=validate,
                )
                if resolution.token_validated or not validate:
                    if service != configured_service:
                        _logger.info(
                            "identity_keychain_service_corrected",
                            configured_service=configured_service,
                            resolved_service=service,
                            resolved_login=resolution.resolved_login,
                        )
                    return resolution
                first_failure = first_failure or resolution.validation_error

            detail = first_failure or f"keychain {configured_service} has no valid entry"
            return self.validate_resolution(
                source="keychain",
                token=None,
                detail=detail,
                canonical_service=configured_service,
                validate=validate,
            )

        return _TokenResolution(
            source="ambient",
            token=None,
            detail="no token configured — inherits ambient gh auth",
            validation_error="no token configured",
        )

    def resolve_env(
        self,
        cfg: RuntimeConfig,
        agent_cfg: AgentConfig,
        *,
        strict: bool = False,
    ) -> dict[str, str]:
        """Return env overlay for *agent_cfg*'s identity. Empty dict if no identity."""
        name = agent_cfg.identity
        if not name:
            return {}
        ident = cfg.identities.get(name)
        if ident is None:
            _logger.warning("identity_missing_at_dispatch", identity=name)
            if strict:
                raise IdentityResolutionError(f"identity {name!r} not defined")
            return {}

        overlay: dict[str, str] = {
            "GIT_AUTHOR_NAME": ident.git_user_name,
            "GIT_AUTHOR_EMAIL": ident.git_user_email,
            "GIT_COMMITTER_NAME": ident.git_user_name,
            "GIT_COMMITTER_EMAIL": ident.git_user_email,
        }

        resolution = self.resolve_token_details(name, ident, validate=strict)
        if strict:
            if resolution.token is None:
                raise IdentityResolutionError(
                    f"identity {name!r} token missing: {resolution.detail}"
                )
            if not resolution.token_validated:
                raise IdentityResolutionError(
                    f"identity {name!r} token invalid: "
                    f"{resolution.validation_error or resolution.detail}"
                )
        if resolution.token is not None:
            overlay["GH_TOKEN"] = resolution.token
            overlay["GITHUB_TOKEN"] = resolution.token

        expanded_config_dir = _expanded_gh_config_dir(ident.gh_config_dir)
        if expanded_config_dir:
            overlay["GH_CONFIG_DIR"] = expanded_config_dir
        else:
            overlay["GH_CONFIG_DIR"] = str(_isolated_gh_config_dir(name))

        if ident.ssh_key_path:
            overlay["GIT_SSH_COMMAND"] = (
                f"ssh -i {os.path.expanduser(ident.ssh_key_path)} -o IdentitiesOnly=yes"
            )

        return overlay

    def verify_repo_access(self, project_path: Path, identity_env: dict[str, str]) -> None:
        """Raise when an injected GitHub token cannot resolve the current repository."""
        token = identity_env.get("GH_TOKEN") or identity_env.get("GITHUB_TOKEN")
        if not token:
            return

        repo_path = str(project_path.resolve())
        cache_key = (sha256(token.encode("utf-8")).hexdigest(), repo_path)
        if cache_key in self._repo_access_cache:
            return

        gh_path = resolve_executable("gh")
        if gh_path is None:
            raise IdentityResolutionError("gh CLI not found for repository access preflight")

        env = {**os.environ, **identity_env}
        try:
            completed = subprocess.run(  # nosec B603
                [gh_path, "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                cwd=project_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=_REPO_ACCESS_TIMEOUT_SECONDS,
                env=env,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise AgentAuthError(f"GitHub repository access preflight failed: {exc}") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "gh repo view failed").strip()
            raise AgentAuthError(
                "GitHub repository access preflight failed for the assigned identity token: "
                f"{detail[:500]}"
            )

        if not completed.stdout.strip():
            raise AgentAuthError(
                "GitHub repository access preflight returned no repository for the assigned "
                "identity token"
            )

        self._repo_access_cache.add(cache_key)


# Module-level default instance — the public free functions below delegate here.
_default_resolver = IdentityResolver()


# ---------------------------------------------------------------------------
# Public free-function API — delegate to the module-level default resolver
# ---------------------------------------------------------------------------


def resolve_identity_env(
    cfg: RuntimeConfig,
    agent_cfg: AgentConfig,
    *,
    strict: bool = False,
) -> dict[str, str]:
    """Return env overlay for *agent_cfg*'s identity. Empty dict if no identity."""
    return _default_resolver.resolve_env(cfg, agent_cfg, strict=strict)


def verify_identity_repo_access(project_path: Path, identity_env: dict[str, str]) -> None:
    """Raise when an injected GitHub token cannot resolve the current repository."""
    _default_resolver.verify_repo_access(project_path, identity_env)


def resolved_github_login_for_agent(cfg: RuntimeConfig, agent_cfg: AgentConfig) -> str | None:
    """Return the validated GitHub login for *agent_cfg*, or None when unbound."""
    name = agent_cfg.identity
    if not name:
        return None
    ident = cfg.identities.get(name)
    if ident is None:
        raise IdentityResolutionError(f"identity {name!r} not defined")
    resolution = _default_resolver.resolve_token_details(name, ident, validate=True)
    if resolution.token is None:
        raise IdentityResolutionError(f"identity {name!r} token missing: {resolution.detail}")
    if not resolution.token_validated:
        raise IdentityResolutionError(
            f"identity {name!r} token invalid: {resolution.validation_error or resolution.detail}"
        )
    return resolution.resolved_login


def reset_token_cache() -> None:
    """Clear the per-process token caches. Intended for tests."""
    _default_resolver.reset_caches()


# ---------------------------------------------------------------------------
# Diagnostic / startup report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentityStatus:
    """One row of the startup identity-resolution table."""

    agent_key: str
    identity_name: str | None
    token_source: str  # one of: "env", "gh_login", "ambient", "none"
    token_resolved: bool
    detail: str  # human-readable explanation, e.g. env var name or login
    token_valid: bool = False
    resolved_login: str | None = None
    validation_error: str | None = None


@dataclass(frozen=True)
class RepoAccessStatus:
    """Repository access preflight result for one configured CLI agent identity."""

    agent_key: str
    identity_name: str | None
    ok: bool
    detail: str


def report_identities(cfg: RuntimeConfig) -> list[IdentityStatus]:
    """Walk *cfg* and produce a status row per CLI agent.

    API-only agents are skipped. ``token_resolved`` reflects whether the
    configured token source produced a value at the moment of inspection
    (env var present / gh CLI returned a token); ``token_valid`` means GitHub
    accepted that exact token and returned a login.
    """
    rows: list[IdentityStatus] = []
    for agent_key, agent_cfg in sorted(cfg.agents.items()):
        identity_name = agent_cfg.identity
        if identity_name is None:
            rows.append(
                IdentityStatus(
                    agent_key=agent_key,
                    identity_name=None,
                    token_source=_SOURCE_NONE,
                    token_resolved=False,
                    token_valid=False,
                    detail="no identity bound — inherits ambient gh auth",
                )
            )
            continue

        ident = cfg.identities.get(identity_name)
        if ident is None:
            rows.append(
                IdentityStatus(
                    agent_key=agent_key,
                    identity_name=identity_name,
                    token_source=_SOURCE_NONE,
                    token_resolved=False,
                    token_valid=False,
                    detail=f"identity {identity_name!r} not defined in identities block",
                    validation_error=f"identity {identity_name!r} not defined",
                )
            )
            continue

        if ident.gh_token_env:
            # Defensive redaction: a previous wizard bug let a literal PAT
            # land in `gh_token_env`. Detect and redact rather than echo it
            # to terminals (and from there into shell scrollback).
            from agentshore.identity_wizard import looks_like_pat

            if looks_like_pat(ident.gh_token_env):
                rows.append(
                    IdentityStatus(
                        agent_key=agent_key,
                        identity_name=identity_name,
                        token_source=_SOURCE_ENV,
                        token_resolved=False,
                        token_valid=False,
                        detail=(
                            "gh_token_env value looks like a PAT — REDACTED. "
                            "Edit agentshore.yaml: replace with a variable NAME "
                            f"(e.g. {identity_name.upper()}_GH_TOKEN) and "
                            "rotate the leaked PAT in GitHub."
                        ),
                        validation_error=(
                            "gh_token_env contains a token value instead of an env var name"
                        ),
                    )
                )
                continue

        resolution = _default_resolver.resolve_token_details(identity_name, ident, validate=True)
        rows.append(
            IdentityStatus(
                agent_key=agent_key,
                identity_name=identity_name,
                token_source=resolution.source,
                token_resolved=resolution.token is not None,
                token_valid=resolution.token_validated,
                resolved_login=resolution.resolved_login,
                validation_error=resolution.validation_error,
                detail=(
                    resolution.detail
                    if resolution.token_validated
                    else (resolution.validation_error or resolution.detail)
                ),
            )
        )
    return rows


def bad_identity_rows(rows: Iterable[IdentityStatus]) -> list[IdentityStatus]:
    """Return the rows whose configured identity token failed validation.

    A row is "bad" when it binds an explicit identity (``identity_name`` is
    set) backed by a real per-identity token source (i.e. not the neutral
    ``ambient``/``none`` sources, which inherit ambient gh auth) yet the token
    did not validate. This is the single canonical statement of the
    identity-health rule shared by ``agentshore start``, ``agentshore
    identity``, and the wizard post-report.
    """
    return [
        r
        for r in rows
        if r.identity_name is not None
        and r.token_source not in _NEUTRAL_TOKEN_SOURCES
        and not r.token_valid
    ]


def missing_token_rows(rows: Iterable[IdentityStatus]) -> list[IdentityStatus]:
    """Return the bad rows whose token simply never resolved (vs. invalid).

    A subset of :func:`bad_identity_rows`: the identity is configured to read
    its token from an env var or ``gh`` login but no token was produced at all,
    so the fix is "set it up" rather than "rotate an invalid token". Used to
    emit ``export``/``gh auth login`` hints in the wizard post-report.
    """
    return [
        r
        for r in bad_identity_rows(rows)
        if not r.token_resolved and r.token_source in {_SOURCE_ENV, _SOURCE_GH_LOGIN}
    ]


def report_identity_repo_access(cfg: RuntimeConfig, project_path: Path) -> list[RepoAccessStatus]:
    """Verify enabled CLI agent identity tokens can resolve *project_path*'s repo.

    Token validity proves the PAT belongs to a GitHub account; it does not prove
    that the PAT is scoped to this repository. This startup preflight catches
    fine-grained PATs scoped to the wrong repository before AgentShore dispatches a
    code-review agent that will inevitably fail inside ``gh pr view``.
    """
    rows: list[RepoAccessStatus] = []
    for agent_key, agent_cfg in sorted(cfg.agents.items()):
        if not agent_cfg.enabled:
            continue
        identity_name = agent_cfg.identity
        if identity_name is None:
            continue

        try:
            identity_env = resolve_identity_env(cfg, agent_cfg, strict=True)
            verify_identity_repo_access(project_path, identity_env)
        except (IdentityResolutionError, AgentAuthError) as exc:
            rows.append(
                RepoAccessStatus(
                    agent_key=agent_key,
                    identity_name=identity_name,
                    ok=False,
                    detail=str(exc),
                )
            )
            continue

        rows.append(
            RepoAccessStatus(
                agent_key=agent_key,
                identity_name=identity_name,
                ok=True,
                detail="ok",
            )
        )
    return rows


def _resolved_login(ident: GitHubIdentity, ident_name: str) -> str | None:
    """Return the GitHub login this identity resolves to, or None if ambient.

    ``gh_token_login`` is the most explicit. AgentShore-managed keychain
    services encode the login in the service name. Env-token identities use
    the identity key as the configured login. With no token source set at all,
    the identity inherits ambient ``gh auth`` and we can't verify what login
    that resolves to.
    """
    return configured_github_login_for_identity(ident, ident_name)


def distinct_gh_logins(cfg: RuntimeConfig) -> set[str]:
    """Return the set of distinct GitHub logins across enabled CLI agents.

    Logins are resolved per ``_resolved_login``.
    """
    logins: set[str] = set()
    for agent_cfg in cfg.agents.values():
        if not agent_cfg.enabled:
            continue
        ident_name = agent_cfg.identity
        if not ident_name:
            continue
        ident = cfg.identities.get(ident_name)
        if ident is None:
            continue
        try:
            login = resolved_github_login_for_agent(cfg, agent_cfg)
        except IdentityResolutionError:
            login = _resolved_login(ident, ident_name)
        if login is not None:
            logins.add(login)
    return logins


def _agents_missing_explicit_login(cfg: RuntimeConfig) -> list[str]:
    """Return enabled agent keys whose identity has no token source.

    An identity with at least one of ``gh_token_login`` / ``gh_token_env``
    / ``gh_token_keychain`` is acceptable: the user has explicitly bound a
    real token to a known login name. An identity with none of those
    inherits ambient ``gh auth`` and is rejected because we can't verify
    what login it resolves to.
    """
    missing: list[str] = []
    for agent_key, agent_cfg in cfg.agents.items():
        if not agent_cfg.enabled:
            continue
        ident_name = agent_cfg.identity
        if not ident_name:
            missing.append(f"{agent_key} (no identity bound)")
            continue
        ident = cfg.identities.get(ident_name)
        if ident is None:
            missing.append(f"{agent_key} (identity {ident_name!r} not defined)")
            continue
        if _resolved_login(ident, ident_name) is None:
            if not (ident.gh_token_login or ident.gh_token_env or ident.gh_token_keychain):
                missing.append(
                    f"{agent_key} (identity {ident_name!r} has no token source — "
                    "set gh_token_login, gh_token_env, or gh_token_keychain)"
                )
            else:
                missing.append(
                    f"{agent_key} (identity {ident_name!r} has no configured GitHub login — "
                    "set gh_token_login, gh_token_env, or an AgentShore-managed gh_token_keychain)"
                )
    return missing


def require_two_distinct_gh_identities(cfg: RuntimeConfig) -> None:
    """Raise ``ConfigError`` when fewer than 2 distinct GH logins are configured.

    Code review requires the reviewer's GH login to differ from the PR
    author's login. With only one identity (or with identities that all
    inherit the same ambient ``gh auth`` user), no PR can ever be reviewed —
    the orchestrator would dispatch code_review plays that always fail
    anti-confirmation. Fail fast at startup with a clear message.

    Each enabled CLI agent must bind an ``identity:`` with an explicit
    token source (``gh_token_login``, ``gh_token_env``, or
    ``gh_token_keychain``). Identities without any of those inherit ambient
    auth and are rejected because we can't verify the resolved login.

    Configurations with zero enabled CLI agents are skipped — code_review is
    structurally unavailable, so the diversity check is moot.
    """
    from agentshore.errors import ConfigError

    enabled_cli_agents = [key for key, agent_cfg in cfg.agents.items() if agent_cfg.enabled]
    if not enabled_cli_agents:
        return  # No CLI agents → no code_review → no diversity requirement.

    missing = _agents_missing_explicit_login(cfg)
    if missing:
        raise ConfigError(
            "Code review requires every enabled CLI agent to bind an `identity:` "
            "with an explicit token source (gh_token_login, gh_token_env, or "
            "gh_token_keychain). Ambient `gh auth` users are not accepted because "
            "their resolved login can't be verified at startup.\n"
            f"  Missing: {', '.join(missing)}\n"
            "Run `agentshore identity --reconfigure` or see docs/identity.md for setup."
        )

    logins = distinct_gh_logins(cfg)
    if len(logins) >= 2:
        return

    detail = f"only one identity is configured: {sorted(logins)[0]!r}"
    raise ConfigError(
        f"Code review requires ≥2 distinct GitHub identities; {detail}.\n"
        "Configure a different `identity:` for at least one agent in agentshore.yaml.\n"
        "Run `agentshore identity --reconfigure` or see docs/identity.md for setup."
    )
