"""Secret redaction for the structlog -> NDJSON pipeline.

The processor here runs immediately before ``JSONRenderer`` and scrubs the
event dict in place-ish (copy-on-write) using two independent signals:

* **key name** — a case-insensitive substring match against
  :data:`_SECRET_KEY_SUBSTRINGS` (``token``, ``secret``, ``api_key``, …).
  The whole value is replaced.
* **value shape** — a regex over known credential prefixes
  (``gho_``, ``ghp_``, ``github_pat_``, ``sk-ant-``, ``xoxb-``, …).  Because
  ``dict_tracebacks`` renders frame locals as *reprs*, a secret is usually
  embedded inside a much larger string (e.g. the repr of an env dict or of an
  ``AgentHandle``), so this is a substring substitution rather than a
  whole-value swap.

Cost control: the walk is depth-capped (:data:`_MAX_DEPTH`) and only descends
into ``dict`` / ``list`` / ``tuple``.  Non-string scalars are returned
untouched without any regex work, and strings that contain no ``_``/``-`` fast
path out before the regex runs."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from structlog.types import EventDict, WrappedLogger

REDACTED = "***REDACTED***"

_MAX_DEPTH = 8

_SECRET_KEY_SUBSTRINGS: tuple[str, ...] = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "auth_header",
    "credential",
    "bearer",
    "private_key",
    "session_key",
    "client_secret",
)

_SECRET_KEY_ALLOWLIST: frozenset[str] = frozenset(
    {
        "token_source",
        "token_resolved",
        "token_valid",
        "gh_token_env",
        "gh_token_keychain",
        "gh_token_login",
        "has_token",
        "token_present",
        "token_length",
    }
)

_CREDENTIAL_VALUE_RE = re.compile(
    r"(?:"
    r"github_pat_[A-Za-z0-9_]{16,}"
    r"|gh[opusr]_[A-Za-z0-9]{16,}"
    r"|sk-ant-[A-Za-z0-9_\-]{16,}"
    r"|sk-proj-[A-Za-z0-9_\-]{16,}"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|xai-[A-Za-z0-9]{20,}"
    r"|AIza[A-Za-z0-9_\-]{30,}"
    r"|ya29\.[A-Za-z0-9_\-]{20,}"
    r"|glpat-[A-Za-z0-9_\-]{16,}"
    r"|Bearer\s+[A-Za-z0-9_\-.=]{20,}"
    r")"
)


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _SECRET_KEY_ALLOWLIST:
        return False
    return any(marker in lowered for marker in _SECRET_KEY_SUBSTRINGS)


def _scrub_text(value: str) -> str:
    """Replace any credential-shaped substring inside *value*."""
    if "_" not in value and "-" not in value and "." not in value and " " not in value:
        return value
    return _CREDENTIAL_VALUE_RE.sub(REDACTED, value)


def _redact(value: Any, depth: int) -> Any:
    if isinstance(value, str):
        return _scrub_text(value)
    if depth >= _MAX_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and _is_secret_key(key)
                else _redact(item, depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item, depth + 1) for item in value)
    return value


def redact_secrets(
    logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """structlog processor: strip credentials from the event dict.

    Must run *after* ``structlog.processors.dict_tracebacks`` (so frame locals
    are already materialised as plain data) and *before* ``JSONRenderer``.
    """
    return {
        key: (REDACTED if isinstance(key, str) and _is_secret_key(key) else _redact(value, 1))
        for key, value in event_dict.items()
    }
