"""Interactive wizard engine for binding GitHub identities to agent types.

Holds the two-pass :class:`IdentityWizard` and its module-level prompt
helpers. Cross-bucket helpers (gh-account detection, keychain I/O) are
imported by *name* and called as module globals so tests can monkeypatch
them at ``agentshore.identity_wizard.wizard.<name>``.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import click

from agentshore.identity_names import (
    canonical_identity_name,
    canonical_keychain_service,
)
from agentshore.identity_wizard.gh_accounts import (
    _GH_LOGIN_RE,
    detect_gh_accounts,
    looks_like_pat,
)
from agentshore.identity_wizard.keychain import (
    _agentshore_managed_service,
    _keychain_backend_label,
    _keychain_has_token,
    _managed_keychain_service,
    _migrate_keychain_token,
    _store_in_keychain,
)

if TYPE_CHECKING:
    from agentshore.identity_wizard.gh_accounts import GhAccount


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

# POSIX shell variable name shape — what `gh_token_env:` MUST hold.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _stdin_is_tty() -> bool:
    """Whether stdin looks interactive (TTY and not gated by env)."""
    if os.environ.get("AGENTSHORE_NONINTERACTIVE"):
        return False
    return sys.stdin.isatty()


def _default_email_for(login: str) -> str:
    return f"{login}@users.noreply.github.com"


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
        assert configured_keychain_service is not None
        assert expected_keychain_service is not None
        migrated = _migrate_keychain_token(
            configured_keychain_service,
            expected_keychain_service,
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
