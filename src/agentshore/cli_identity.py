"""Interactive wizard for binding GitHub identities to coding-agent types.

Invoked from ``agentshore init`` after ``agentshore.yaml`` is generated, and from
``agentshore identity --reconfigure`` to update an existing project's bindings
without resetting state. Detects gh-authenticated accounts, prompts the user
to bind one to each detected agent CLI, and patches the YAML in-place.

Behavior:
- ``agentshore init`` always passes ``force_run=True``; the wizard then prints
  a TTY notice and skips cleanly when stdin is piped, instead of silently
  no-op'ing.
- ``AGENTSHORE_NONINTERACTIVE=1`` always wins (escape hatch for CI/scripts).
- The two-pass flow first collects agent→identity mappings, then prompts
  for per-identity details once, with a "(used by: ...)" annotation.
- Garbage input at the agent picker re-prompts; an ``(n) new account`` path
  lets users bind a login that isn't yet authenticated via ``gh``.
"""

from __future__ import annotations

import os
import re
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import click
import yaml

from agentshore.environment import resolve_executable
from agentshore.identity_names import (
    canonical_identity_name,
    canonical_keychain_service,
    is_valid_github_login,
    keychain_service_for_login,
    keychain_service_for_repo_login,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from agentshore.agents.identity import IdentityStatus, RepoAccessStatus

# ---------------------------------------------------------------------------
# gh detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GhAccount:
    login: str
    active: bool


_LOGIN_LINE = re.compile(
    r"Logged in to (?P<host>\S+) account (?P<login>[A-Za-z0-9_.\-]+)(?P<active>\s*\(active\))?"
)

# GitHub username rules: 1-39 chars, alphanumeric or single non-leading,
# non-trailing, non-consecutive hyphens.
_GH_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")

# POSIX shell variable name shape — what `gh_token_env:` MUST hold.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# GitHub Personal Access Token prefixes. Used to detect when a user pastes
# a PAT into a slot that wants a label/name. Covers classic + fine-grained
# + the various OAuth/server token shapes documented at
# https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/about-authentication-to-github#githubs-token-formats.
_PAT_PREFIX_RE = re.compile(r"^(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)")


def looks_like_pat(value: str) -> bool:
    """Heuristically detect a pasted GitHub Personal Access Token.

    Used both at input time (reject the wrong slot) and at report time
    (redact a value that already landed in agentshore.yaml so the leak
    doesn't escape further into terminals/scrollback).
    """
    return bool(_PAT_PREFIX_RE.match(value.strip()))


def parse_gh_auth_status(text: str) -> list[GhAccount]:
    """Parse the human-text output of ``gh auth status -a``.

    Only github.com accounts are returned. Order matches the order in *text*
    so the active account naturally appears first when gh sorts that way.
    """
    accounts: dict[str, GhAccount] = {}
    current_host: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Section headers like "github.com" appear at indent 0.
        if not raw_line.startswith((" ", "\t")) and line.endswith(".com"):
            current_host = line
            continue
        m = _LOGIN_LINE.search(line)
        if not m:
            continue
        host = m.group("host")
        if host != "github.com" and current_host != "github.com":
            continue
        login = m.group("login")
        is_active = bool(m.group("active"))
        # Last write wins; gh occasionally prints a login twice.
        accounts[login] = GhAccount(login=login, active=is_active)

    return list(accounts.values())


def detect_gh_accounts() -> list[GhAccount]:
    """Return github.com accounts gh is currently authenticated to.

    Empty list if gh is missing or no accounts are logged in.
    """
    gh_path = resolve_executable("gh")
    if gh_path is None:
        return []
    try:
        result = subprocess.run(  # nosec B603
            [gh_path, "auth", "status", "-a"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    # `gh auth status` writes to stderr historically and stdout in newer
    # versions; combine both to be safe.
    return parse_gh_auth_status(result.stdout + "\n" + result.stderr)


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentityBinding:
    """One identity that the wizard chose to write to agentshore.yaml.

    Exactly one of ``gh_token_login`` / ``gh_token_env`` /
    ``gh_token_keychain`` is set, mirroring ``GitHubIdentity``.
    """

    name: str
    git_user_name: str
    git_user_email: str
    gh_token_login: str | None = None
    gh_token_env: str | None = None
    gh_token_keychain: str | None = None


@dataclass(frozen=True)
class WizardResult:
    """Outcome of a wizard run.

    ``identities`` and ``agent_to_identity`` may be empty if the user
    skipped every prompt; the YAML patcher is a no-op in that case.
    """

    identities: dict[str, IdentityBinding]
    agent_to_identity: dict[str, str]


class _WizardAbortError(Exception):
    """Raised when input retries are exhausted; converts to a clean skip."""


# Sentinel returned by ``_prompt_choice`` when the user picks an extra-key
# option (e.g. ``"n"`` for new account). Callers compare against the sentinel
# prefix; the trailing key identifies which extra option was chosen.
_EXTRA_PREFIX = "__extra__:"


def _stdin_is_tty() -> bool:
    """Whether stdin looks interactive (TTY and not gated by env)."""
    if os.environ.get("AGENTSHORE_NONINTERACTIVE"):
        return False
    return sys.stdin.isatty()


def _default_email_for(login: str) -> str:
    return f"{login}@users.noreply.github.com"


def _keychain_backend_label() -> str | None:
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
    except ImportError:
        return None
    backend = keyring.get_keyring()
    if isinstance(backend, FailKeyring):
        return None
    cls = type(backend).__name__
    if sys.platform == "darwin":
        return f"macOS Keychain ({cls})"
    if sys.platform.startswith("win"):
        return f"Windows Credential Manager ({cls})"
    return f"OS credential store ({cls})"


def _store_in_keychain(service: str, token: str) -> tuple[bool, str]:
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:
        return False, "keyring library not installed"
    try:
        keyring.set_password(service, service, token)
    except KeyringError as exc:
        return False, f"keyring write failed: {exc}"
    return True, f"Stored under service {service!r} ({_keychain_backend_label()})"


def _migrate_keychain_token(from_service: str, to_service: str) -> bool:
    """Copy a token from one keychain service to another. Returns True on success."""
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:
        return False
    try:
        token = keyring.get_password(from_service, from_service)
    except KeyringError:
        return False
    if not token or not token.strip():
        return False
    try:
        keyring.set_password(to_service, to_service, token)
    except KeyringError:
        return False
    return True


def _keychain_has_token(service: str) -> bool:
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:
        return False
    try:
        token = keyring.get_password(service, service)
    except KeyringError:
        return False
    return bool(token and token.strip())


def _managed_keychain_service(login: str, repo_name_with_owner: str | None) -> str:
    if repo_name_with_owner:
        return keychain_service_for_repo_login(repo_name_with_owner, login)
    return keychain_service_for_login(login)


def _agentshore_managed_service(service: str) -> bool:
    return canonical_keychain_service(service).startswith("agentshore/")


class KeychainManager:
    """Convenience grouping of keychain operations — delegates to module-level
    functions so monkeypatching in tests works transparently.
    """

    @staticmethod
    def backend_label() -> str | None:
        return _keychain_backend_label()

    @staticmethod
    def store(service: str, token: str) -> tuple[bool, str]:
        return _store_in_keychain(service, token)

    @staticmethod
    def has_token(service: str) -> bool:
        return _keychain_has_token(service)

    @staticmethod
    def managed_service(login: str, repo_name_with_owner: str | None) -> str:
        return _managed_keychain_service(login, repo_name_with_owner)

    @staticmethod
    def is_agentshore_managed(service: str) -> bool:
        return _agentshore_managed_service(service)


class IdentityWizard:
    """Two-pass interactive wizard for binding GitHub identities to agents.

    Pre-computes shared state (accounts, existing identities, login lists)
    in ``__init__`` and exposes ``run()`` which orchestrates two passes:
    Pass 1 maps each agent to a login, Pass 2 collects identity details.
    Prompt functions remain module-level for monkeypatch compatibility.
    """

    def __init__(
        self,
        agent_keys: list[str],
        *,
        accounts: list[GhAccount] | None = None,
        defaults: dict[str, str] | None = None,
        existing_identities: dict[str, IdentityBinding] | None = None,
        repo_name_with_owner: str | None = None,
    ) -> None:
        self._agent_keys = agent_keys
        self._accounts = accounts if accounts is not None else detect_gh_accounts()
        self._defaults = defaults or {}
        self._repo_name_with_owner = repo_name_with_owner

        self._existing_identities: dict[str, IdentityBinding] = {
            canonical_identity_name(login): IdentityBinding(
                name=canonical_identity_name(binding.name),
                git_user_name=binding.git_user_name,
                git_user_email=binding.git_user_email,
                gh_token_login=binding.gh_token_login,
                gh_token_env=binding.gh_token_env,
                gh_token_keychain=(
                    canonical_keychain_service(binding.gh_token_keychain)
                    if binding.gh_token_keychain
                    else None
                ),
            )
            for login, binding in (existing_identities or {}).items()
        }

        self._gh_login_keys: set[str] = {canonical_identity_name(a.login) for a in self._accounts}
        self._configured_only: list[str] = [
            login
            for login in self._existing_identities
            if canonical_identity_name(login) not in self._gh_login_keys
        ]
        self._logins: list[str] = [acc.login for acc in self._accounts] + self._configured_only
        self._new_logins: set[str] = set()
        self._active_idx = next((i for i, a in enumerate(self._accounts) if a.active), 0)

    # -- two-pass orchestration ----------------------------------------------

    def _pass1_bind_agents(self) -> dict[str, str]:
        """Map each agent to a login via interactive prompts."""
        agent_to_login: dict[str, str] = {}
        for agent_key in self._agent_keys:
            label = f"  {agent_key:<14}"
            per_agent_default = self._active_idx
            existing_login = self._defaults.get(agent_key)
            if existing_login:
                login_index_by_key = {
                    canonical_identity_name(login): i for i, login in enumerate(self._logins)
                }
                existing_idx = login_index_by_key.get(canonical_identity_name(existing_login))
                if existing_idx is not None:
                    per_agent_default = existing_idx
                    label = f"  {agent_key:<14} (current: {existing_login})"
                else:
                    label = f"  {agent_key:<14} (was {existing_login} — re-pick)"
            chosen = _prompt_choice(
                label,
                self._logins,
                per_agent_default if self._logins else 0,
                extra_keys={"n": "new account"},
            )
            if chosen is None:
                continue
            if chosen == f"{_EXTRA_PREFIX}n":
                new_login = _prompt_new_login(set(self._logins))
                if new_login is None:
                    continue
                known_keys = {canonical_identity_name(login) for login in self._logins}
                if canonical_identity_name(new_login) not in known_keys:
                    self._logins.append(new_login)
                    self._new_logins.add(new_login)
                agent_to_login[agent_key] = new_login
            else:
                agent_to_login[agent_key] = chosen
        return agent_to_login

    def _pass2_collect_details(self, agent_to_login: dict[str, str]) -> dict[str, IdentityBinding]:
        """Collect identity details once per unique login."""
        seen_order: list[str] = []
        seen: set[str] = set()
        for login in agent_to_login.values():
            if login not in seen:
                seen.add(login)
                seen_order.append(login)

        used_by_lookup: dict[str, list[str]] = {login: [] for login in seen_order}
        for agent_key, login in agent_to_login.items():
            used_by_lookup[login].append(agent_key)

        bindings: dict[str, IdentityBinding] = {}
        click.echo("\nStep 2/2 — Confirm details for each identity.")
        for login in seen_order:
            existing = self._existing_identities.get(login) or self._existing_identities.get(
                canonical_identity_name(login)
            )
            bindings[login] = _collect_identity_details(
                login,
                used_by=used_by_lookup[login],
                is_new_account=(
                    login in self._new_logins
                    or (
                        canonical_identity_name(login) not in self._gh_login_keys
                        and existing is None
                    )
                ),
                repo_name_with_owner=self._repo_name_with_owner,
                existing=existing,
            )
        return bindings

    def run(self) -> WizardResult:
        """Execute the full two-pass wizard and return the result."""
        click.echo("\nGitHub identity setup")
        click.echo("─────────────────────")
        click.echo(f"Coding agents detected:  {', '.join(self._agent_keys)}")
        if self._accounts or self._configured_only:
            click.echo("GitHub accounts available:")
            idx = 1
            for acc in self._accounts:
                marker = "  (active)" if acc.active else ""
                click.echo(f"  {idx}) {acc.login}{marker}  [gh auth]")
                idx += 1
            for login in self._configured_only:
                source = _summarize_existing_binding(self._existing_identities[login])
                click.echo(f"  {idx}) {login}  [configured: {source}]")
                idx += 1
        else:
            click.echo(
                "  No gh-authenticated accounts detected — bind by hand "
                "via (n) new account, or skip."
            )
        click.echo("")
        click.echo(
            "Step 1/2 — Bind each agent to an identity.\n"
            "            Press Enter for the default; (n) new account; (s) skip."
        )

        try:
            agent_to_login = self._pass1_bind_agents()
        except _WizardAbortError as exc:
            click.echo(f"\n  ({exc}; skipping identity setup.)")
            return WizardResult(identities={}, agent_to_identity={})

        if not agent_to_login:
            return WizardResult(identities={}, agent_to_identity={})

        try:
            bindings = self._pass2_collect_details(agent_to_login)
        except _WizardAbortError as exc:
            click.echo(f"\n  ({exc}; skipping identity setup.)")
            return WizardResult(identities={}, agent_to_identity={})

        canonical_bindings = {
            canonical_identity_name(name): IdentityBinding(
                name=canonical_identity_name(binding.name),
                git_user_name=binding.git_user_name,
                git_user_email=binding.git_user_email,
                gh_token_login=binding.gh_token_login,
                gh_token_env=binding.gh_token_env,
                gh_token_keychain=(
                    canonical_keychain_service(binding.gh_token_keychain)
                    if binding.gh_token_keychain
                    else None
                ),
            )
            for name, binding in bindings.items()
        }
        canonical_agent_to_identity = {
            agent_key: canonical_identity_name(login) for agent_key, login in agent_to_login.items()
        }
        return WizardResult(
            identities=canonical_bindings,
            agent_to_identity=canonical_agent_to_identity,
        )


# ---------------------------------------------------------------------------
# Prompt helpers (module-level for monkeypatch compatibility)
# ---------------------------------------------------------------------------


def _prompt_env_var_name(login: str, *, max_attempts: int = 5) -> str:
    default_name = login.upper().replace("-", "_") + "_GH_TOKEN"
    for _ in range(max_attempts):
        raw: str = click.prompt(
            f"    Env var NAME for {login}'s PAT "
            f"(label only, e.g. {default_name} — NOT the PAT itself)",
            default=default_name,
            show_default=True,
        )
        value: str = raw.strip()
        if not value:
            click.echo("    Variable name cannot be empty.")
            continue
        if looks_like_pat(value):
            click.echo(
                "    That looks like a PAT, not a variable name. The wizard "
                "wants the NAME of the env var (a label, e.g. "
                f"{default_name}); you'll `export <NAME>=<your-PAT>` later. "
                "If you'd rather paste the PAT now, type 's' to back out and "
                "answer 'y' at the previous prompt."
            )
            continue
        if value.lower() in {"s", "skip"}:
            raise _WizardAbortError("user backed out of env var name prompt")
        if not _ENV_VAR_NAME_RE.match(value):
            click.echo(
                "    Invalid env var name. Use letters, digits, and underscore "
                "only; must not start with a digit."
            )
            continue
        return value
    raise _WizardAbortError("Too many invalid attempts at env var name prompt")


def _prompt_token_strategy(
    login: str,
    *,
    is_new_account: bool = False,
    repo_name_with_owner: str | None = None,
) -> tuple[str, dict[str, str]]:
    backend = _keychain_backend_label()
    service = _managed_keychain_service(login, repo_name_with_owner)
    repo_label = f" for {repo_name_with_owner}" if repo_name_with_owner else ""

    if is_new_account and backend is not None and _keychain_has_token(service):
        click.echo(
            f"  Found an existing PAT for {login}{repo_label} in {backend} service {service!r}."
        )
        if click.confirm(
            f"  Use the existing keychain token for {login}{repo_label}?",
            default=True,
        ):
            return "keychain", {"gh_token_keychain": service}
        click.echo("    Leaving the existing keychain entry untouched.")

    if backend is not None and click.confirm(
        f"  Do you have a PAT for {login}{repo_label} to paste now? "
        f"(stored in {backend}, never written to agentshore.yaml)",
        default=is_new_account,
    ):
        token = click.prompt(
            "    Paste PAT (input hidden; press Enter to back out)",
            hide_input=True,
            default="",
            show_default=False,
        )
        token_str = token.strip()
        if token_str:
            if not looks_like_pat(token_str):
                click.echo(
                    "    Warning: input doesn't look like a GitHub PAT "
                    "(expected ghp_ / github_pat_ prefix). Storing anyway."
                )
            ok, msg = _store_in_keychain(service, token_str)
            click.echo(f"    {msg}")
            if ok:
                return "keychain", {"gh_token_keychain": service}
            click.echo("    Falling through to other strategies.")
        else:
            click.echo("    No token entered. Falling through to other strategies.")

    options = [
        f"`gh auth token -u {login}` at runtime",
        (
            "Read from a named env var you'll `export` before `agentshore start`"
            " (NAME only — wizard never writes a PAT to agentshore.yaml)"
        ),
    ]
    default_index = 1 if is_new_account else 0

    idx = default_index
    for _ in range(5):
        click.echo(f"  Token strategy for {login}:")
        for i, opt in enumerate(options, 1):
            click.echo(f"    {i}) {opt}")
        raw = click.prompt("    Pick", default=str(default_index + 1), show_default=True).strip()
        if looks_like_pat(raw):
            click.echo(
                "    That looks like a PAT. The wizard isn't asking for the "
                "PAT here — pick 1 or 2, or back out and answer 'y' at the "
                "earlier paste-PAT prompt."
            )
            continue
        try:
            idx = int(raw) - 1
        except ValueError:
            click.echo(f"    Invalid choice {raw!r}. Pick 1 or 2.")
            continue
        if not 0 <= idx < len(options):
            click.echo(f"    Out of range: {idx + 1}. Pick 1 or 2.")
            continue
        if (
            idx == 0
            and is_new_account
            and not click.confirm(
                f"    `gh auth token -u {login}` will fail until you run "
                f"`gh auth login -u {login}`. Use this strategy anyway?",
                default=False,
            )
        ):
            click.echo("    Pick another strategy.")
            continue
        break

    if idx == 0:
        return "gh_login", {"gh_token_login": login}
    return "env", {"gh_token_env": _prompt_env_var_name(login)}


def _prompt_choice(
    label: str,
    options: list[str],
    default_index: int,
    *,
    extra_keys: dict[str, str] | None = None,
    max_attempts: int = 5,
) -> str | None:
    extra_keys = extra_keys or {}
    rendered_opts = "  ".join(f"({i + 1}) {opt}" for i, opt in enumerate(options))
    rendered_extras = "  ".join(f"({k}) {desc}" for k, desc in extra_keys.items())
    sections = [s for s in (rendered_opts, rendered_extras, "(s) skip") if s]
    prompt = f"{label}  " + "  ".join(sections)
    extra_summary = (
        ", ".join(f"'{k}' for {desc}" for k, desc in extra_keys.items()) if extra_keys else ""
    )

    for _ in range(max_attempts):
        if options:
            raw = click.prompt(prompt, default=str(default_index + 1), show_default=True)
        else:
            raw = click.prompt(prompt, default="", show_default=False)
        raw_norm = raw.strip().lower()
        if raw_norm == "" and options:
            return options[default_index]
        if raw_norm in {"s", "skip"}:
            return None
        if raw_norm in extra_keys:
            return f"{_EXTRA_PREFIX}{raw_norm}"
        if not options:
            click.echo(
                f"    Invalid choice {raw!r}. "
                + (f"Type {extra_summary}, " if extra_summary else "")
                + "or 's' to skip."
            )
            continue
        try:
            idx = int(raw_norm) - 1
        except ValueError:
            click.echo(
                f"    Invalid choice {raw!r}. Type a number 1-{len(options)}, "
                + (f"{extra_summary}, " if extra_summary else "")
                + "or 's' to skip."
            )
            continue
        if 0 <= idx < len(options):
            return options[idx]
        click.echo(f"    Out of range: {idx + 1}. Pick 1-{len(options)}.")
    raise _WizardAbortError(f"Too many invalid attempts at {label.strip()!r}")


def _prompt_new_login(existing_logins: set[str], *, max_attempts: int = 5) -> str | None:
    folded = {x.casefold(): x for x in existing_logins}
    for _ in range(max_attempts):
        raw: str = click.prompt(
            "    GitHub login for the new account (or 's' to back out)",
            default="",
            show_default=False,
        )
        value: str = raw.strip()
        if value.lower() in {"s", "skip"}:
            return None
        if not value:
            click.echo("    Login cannot be empty.")
            continue
        if not _GH_LOGIN_RE.match(value):
            click.echo(
                "    Invalid GitHub login. Use 1-39 chars, alphanumeric "
                "or non-leading/non-trailing/non-consecutive hyphen."
            )
            continue
        canonical = folded.get(value.casefold())
        if canonical is not None:
            click.echo(f"    {value!r} matches existing account {canonical!r}; using that.")
            return canonical
        return value
    raise _WizardAbortError("Too many invalid attempts at new-login prompt")


def _collect_identity_details(
    login: str,
    *,
    used_by: list[str],
    is_new_account: bool,
    repo_name_with_owner: str | None = None,
    existing: IdentityBinding | None = None,
) -> IdentityBinding:
    used = ", ".join(used_by)
    click.echo(f"\n  Identity {login!r}  (used by: {used})")
    expected_keychain_service = (
        _managed_keychain_service(login, repo_name_with_owner) if repo_name_with_owner else None
    )
    configured_keychain_service = (
        canonical_keychain_service(existing.gh_token_keychain)
        if existing and existing.gh_token_keychain
        else None
    )
    repo_keychain_mismatch = (
        existing is not None
        and expected_keychain_service is not None
        and configured_keychain_service is not None
        and configured_keychain_service != expected_keychain_service
        and _agentshore_managed_service(configured_keychain_service)
    )

    if existing is not None and not repo_keychain_mismatch:
        existing_summary = _summarize_existing_binding(existing)
        if click.confirm(
            f"    Keep existing settings ({existing_summary})?",
            default=True,
        ):
            return IdentityBinding(
                name=canonical_identity_name(existing.name),
                git_user_name=existing.git_user_name,
                git_user_email=existing.git_user_email,
                gh_token_login=existing.gh_token_login,
                gh_token_env=existing.gh_token_env,
                gh_token_keychain=(
                    canonical_keychain_service(existing.gh_token_keychain)
                    if existing.gh_token_keychain
                    else None
                ),
            )
    elif repo_keychain_mismatch:
        migrated = _migrate_keychain_token(
            configured_keychain_service, expected_keychain_service  # type: ignore[arg-type]
        )
        if migrated and existing is not None:
            click.echo(
                f"    Migrated keychain token from {configured_keychain_service!r} "
                f"to repo-scoped {expected_keychain_service!r}."
            )
            return IdentityBinding(
                name=canonical_identity_name(existing.name),
                git_user_name=existing.git_user_name,
                git_user_email=existing.git_user_email,
                gh_token_keychain=expected_keychain_service,
            )
        click.echo(
            "    Existing keychain service "
            f"{configured_keychain_service!r} is not scoped to "
            f"{repo_name_with_owner}; this project will use "
            f"{expected_keychain_service!r}."
        )
    elif is_new_account:
        click.echo(
            f"    Note: {login!r} is not in `gh auth status`. The "
            "`gh auth token` strategy will fail until you run "
            f"`gh auth login -u {login}`. The wizard offers to store a PAT "
            "directly in your OS keychain instead — recommended for new "
            "accounts."
        )

    email_default = existing.git_user_email if existing else _default_email_for(login)
    name_default = existing.git_user_name if existing else login
    email = click.prompt(
        f"    Email for {login}",
        default=email_default,
        show_default=True,
    )
    name = click.prompt(
        f"    Display name for {login}",
        default=name_default,
        show_default=True,
    )
    _, token_fields = _prompt_token_strategy(
        login,
        is_new_account=is_new_account or repo_keychain_mismatch,
        repo_name_with_owner=repo_name_with_owner,
    )
    return IdentityBinding(
        name=login,
        git_user_name=name,
        git_user_email=email,
        **token_fields,
    )


def _summarize_existing_binding(b: IdentityBinding) -> str:
    if b.gh_token_keychain:
        return f"keychain service {b.gh_token_keychain!r}"
    if b.gh_token_login:
        return f"gh auth token -u {b.gh_token_login}"
    if b.gh_token_env:
        return f"env var ${b.gh_token_env}"
    return "no token configured"


def run_wizard(
    agent_keys: list[str],
    *,
    accounts: list[GhAccount] | None = None,
    defaults: dict[str, str] | None = None,
    existing_identities: dict[str, IdentityBinding] | None = None,
    repo_name_with_owner: str | None = None,
    prompter: object | None = None,
) -> WizardResult:
    """Drive the interactive prompts and return the chosen bindings."""
    del prompter  # reserved
    return IdentityWizard(
        agent_keys,
        accounts=accounts,
        defaults=defaults,
        existing_identities=existing_identities,
        repo_name_with_owner=repo_name_with_owner,
    ).run()


# ---------------------------------------------------------------------------
# YAML patcher
# ---------------------------------------------------------------------------


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


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


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
            gh_token_login=_str_or_none(ident.get("gh_token_login")),
            gh_token_env=_str_or_none(ident.get("gh_token_env")),
            gh_token_keychain=_str_or_none(ident.get("gh_token_keychain")),
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


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
        from agentshore.agents.identity import report_identities, report_identity_repo_access
        from agentshore.config import load_config
        from agentshore.errors import ConfigError

        cfg = load_config(config_path)
        rows = report_identities(cfg)
    except (ConfigError, OSError, subprocess.SubprocessError, RuntimeError) as exc:
        click.echo(f"\n  (Could not verify bindings — re-run `agentshore identity`: {exc})")
        return

    click.echo("")
    echo_identity_report(rows)
    bad = [
        r
        for r in rows
        if r.identity_name is not None
        and r.token_source not in {"ambient", "none"}
        and not r.token_valid
    ]
    missing = [r for r in bad if not r.token_resolved and r.token_source in {"env", "gh_login"}]
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
