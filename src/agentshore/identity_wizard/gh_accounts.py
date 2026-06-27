"""GitHub account detection and PAT/login parsing for the identity wizard.

Detection of gh-authenticated accounts (``gh auth status``), the login/PAT
regexes, and the PAT heuristic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agentshore import command


@dataclass(frozen=True)
class GhAccount:
    login: str
    active: bool


_LOGIN_LINE = re.compile(
    r"Logged in to (?P<host>\S+) account (?P<login>[A-Za-z0-9_.\-]+)(?P<active>\s*\(active\))?"
)

# GitHub login: 1-39 chars, alphanumeric with non-leading/trailing/consecutive hyphens.
_GH_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")

# GitHub PAT prefixes — detect a PAT pasted into a label/name slot.
# Covers classic, fine-grained, and OAuth/server token shapes.
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
    result = command.gh_sync("auth", "status", "-a", timeout_seconds=10.0)
    if result.tool_missing:
        return []
    # gh auth status writes to stderr (old) or stdout (new); combine both.
    return parse_gh_auth_status(result.stdout + "\n" + result.stderr)
