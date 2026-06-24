"""Single home for the error-classification marker tables.

The same auth/rate-limit/timeout/etc. substring vocabularies were previously
defined several times across ``agents/cli/errors.py``, ``agents/auth_probe.py``,
``agents/git_auth_probe.py``, ``plays/_publish_reconciler.py`` and
``errors.py`` — and had silently drifted (the Critical "error / cooldown / retry
consistency" finding in ``tmp/TNQA.md``). This module is the union home: every
scoped view a consumer reads is defined here verbatim, and the canonical
``*_MARKERS`` supersets are *derived* from those views, so a future edit that
widens one view without the others is caught by ``tests/test_error_markers.py``.

Phase 1 of ``tmp/PLAN-error-cooldown-unification.md`` relocated these tables here
without changing any consumer's matched set; Phase 4 then pointed the broad
auth-classification sites (the executor failure inferer, the publish reconciler,
and the CLI stderr classifier) at the canonical ``AUTH_MARKERS`` superset. The
high-precision views (``STDOUT_AUTH_MARKERS`` for agent work product,
``GITHUB_AUTH_ERROR_MARKERS`` for skill error strings) stay narrow by design.
"""

from __future__ import annotations

from agentshore.errors import GITHUB_AUTH_ERROR_MARKERS

# ===========================================================================
# Auth family
# ===========================================================================
#
# Four scoped views detect "this is a GitHub/backend auth failure" against
# different surfaces, each with deliberately different precision:
#   * GITHUB_AUTH_ERROR_MARKERS — free-form skill/command error strings
#     (lives in ``errors.py``; the long-standing public name).
#   * PUBLISH_AUTH_MARKERS      — issue-pickup publish-failure recovery (narrow).
#   * STDERR_AUTH_PATTERNS      — CLI stderr (diagnostics; safe to match broadly).
#   * STDOUT_AUTH_MARKERS       — CLI stdout (the work PRODUCT; high-precision
#                                 subset that never appears in legit agent output).
# ``AUTH_MARKERS`` is the union of all four — the canonical superset.

# Issue-pickup publish-failure recovery. Was ``_publish_reconciler._AUTH_ERROR_MARKERS``.
PUBLISH_AUTH_MARKERS: tuple[str, ...] = (
    "bad credentials",
    "http 401",
    "401 unauthorized",
    "http 403",
    "403 forbidden",
    "irrecoverable github access failure",
    "could not resolve to a repository",
    "repository/pr is not accessible",
    "repository is not resolvable to this token",
    "not resolvable to this token/session",
)

# CLI stderr auth classification (full set). Was ``cli/errors._AUTH_PATTERNS``.
# The trailing two are Codex backend-session TTL-expiry signatures — stderr-only,
# deliberately NOT mirrored into the stdout subset (see CACHE_RENEWAL_MARKERS).
STDERR_AUTH_PATTERNS: tuple[str, ...] = (
    "unauthorized",
    "401",
    "authentication",
    "invalid api key",
    "bad credentials",
    "forbidden",
    "403",
    "irrecoverable github access failure",
    "github connector returned 404",
    "connector repo 404",
    "could not resolve to a repository with the name",
    "could not resolve to a repository",
    "repository/pr is not accessible",
    "not found/could not resolve repository",
    "repository is not resolvable to this token",
    "not resolvable to this token/session",
    "lacks access to repository",
    "cannot access repository metadata",
    "failed to renew cache ttl",
    "failed to refresh available models",
)

# CLI stdout-safe auth subset (high-precision). Drops the short generic tokens
# ("401"/"403"/"unauthorized"/"forbidden"/"authentication") that appear in code;
# keeps the distinctive gh/git access strings. Was ``cli/errors._AUTH_STDOUT``.
STDOUT_AUTH_MARKERS: tuple[str, ...] = (
    "invalid api key",
    "bad credentials",
    "irrecoverable github access failure",
    "github connector returned 404",
    "connector repo 404",
    "could not resolve to a repository with the name",
    "could not resolve to a repository",
    "repository/pr is not accessible",
    "not found/could not resolve repository",
    "repository is not resolvable to this token",
    "not resolvable to this token/session",
    "lacks access to repository",
    "cannot access repository metadata",
)

# Codex CLI model-cache TTL-renewal blips. A subset of the stderr auth markers
# that the Codex CLI also prints during a *transient* renewal hiccup (not a real
# auth rejection); the watchdog subtracts these to separate "hard auth" from a
# cache-renewal blip. Was ``cli/errors._CACHE_RENEWAL_MARKERS``.
CACHE_RENEWAL_MARKERS: frozenset[str] = frozenset(
    {"failed to renew cache ttl", "failed to refresh available models"}
)

# Canonical superset: every auth spelling across all four scoped views.
AUTH_MARKERS: frozenset[str] = frozenset(GITHUB_AUTH_ERROR_MARKERS).union(
    PUBLISH_AUTH_MARKERS, STDERR_AUTH_PATTERNS, STDOUT_AUTH_MARKERS
)

# ---------------------------------------------------------------------------
# Auth-adjacent probe vocabularies (siblings, NOT subsets of AUTH_MARKERS).
# ---------------------------------------------------------------------------
# These detect the same *category* — "this agent cannot authenticate" — but
# against a disjoint string set: CLI *login-status* output and *git transport*
# errors, not GitHub-API auth strings. Homed here for single-sourcing; they are
# intentionally not asserted ⊆ AUTH_MARKERS.

# Codex pre-launch login-status probe. Was ``auth_probe._NOT_AUTHED_MARKERS``.
PROBE_NOT_AUTHED_MARKERS: tuple[str, ...] = (
    "not logged in",
    "not authenticated",
    "logged out",
    "no credentials",
    "please run",
    "run `codex login`",
    "run 'codex login'",
    "failed to renew cache ttl",
    "failed to refresh available models",
)

# git ls-remote pre-launch probe. Was ``git_auth_probe._AUTH_FAILED_MARKERS``.
GIT_AUTH_FAILED_MARKERS: tuple[str, ...] = (
    "authentication failed",
    "invalid username or password",
    "could not read username",
    "could not read password",
    "permission denied",
    "access denied",
    "403 forbidden",
    "401 unauthorized",
    "remote: invalid credentials",
    "fatal: could not read from remote repository",
    "terminal prompts disabled",
)

# ===========================================================================
# Rate-limit family
# ===========================================================================

# CLI stderr (full set). Was ``cli/errors._RATE_LIMIT_PATTERNS``.
# "usage limit" / "try again at" are Codex usage-limit signatures (#276): Codex
# prints "You've hit your usage limit … or try again at <ts>." on a quota miss.
# "spending-limit" / "out of credits" are Grok's billing-quota signatures: it
# prints "responses API error status=403 Forbidden error_message=
# personal-team-blocked:spending-limit: You have run out of credits …" when the
# account quota is exhausted. The trailing 403/Forbidden previously got it
# misclassified as AUTH (a permanent session bench) when it is really a
# recoverable quota exhaustion — the Grok analogue of Codex's usage limit.
# Folding all of these into the rate-limit family routes the dispatch through the
# exact path Claude's "hit your session limit" already uses — the
# RATE_LIMIT_RECOVERY take_break plus the provider-wide eligibility hold
# (rate_limited_types) that benches every same-type instance sharing the
# exhausted quota. Because ``_classify_error`` checks rate_limit *before* auth,
# the quota markers win over the coexisting 403/Forbidden auth tokens.
RATE_LIMIT_STDERR_PATTERNS: tuple[str, ...] = (
    "rate limit",
    "rate_limit",
    "429",
    "too many requests",
    "overloaded",
    "capacity",
    "retry after",
    "throttl",
    "usage limit",
    "try again at",
    "spending-limit",
    "out of credits",
)

# CLI stdout-safe subset. Was ``cli/errors._RATE_LIMIT_STDOUT``.
# "hit your usage limit" is the Codex stdout quota signature (mirrors Claude's
# "hit your session limit"); kept as the full distinctive phrase so it never
# matches an agent's work product (#276).
RATE_LIMIT_STDOUT_MARKERS: tuple[str, ...] = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "retry after",
    "hit your session limit",
    "hit your usage limit",
)

RATE_LIMIT_MARKERS: frozenset[str] = frozenset(RATE_LIMIT_STDERR_PATTERNS).union(
    RATE_LIMIT_STDOUT_MARKERS
)

# ===========================================================================
# Timeout family
# ===========================================================================

# CLI stderr. Was ``cli/errors._TIMEOUT_PATTERNS``.
TIMEOUT_STDERR_PATTERNS: tuple[str, ...] = (
    "timeout",
    "timed out",
    "deadline exceeded",
    "context deadline",
)
# All timeout tokens are common in source/test names — none are stdout-safe.
TIMEOUT_STDOUT_MARKERS: tuple[str, ...] = ()

TIMEOUT_MARKERS: frozenset[str] = frozenset(TIMEOUT_STDERR_PATTERNS).union(TIMEOUT_STDOUT_MARKERS)

# ===========================================================================
# Invalid-model family
# ===========================================================================

# CLI stderr. Was ``cli/errors._INVALID_MODEL_PATTERNS``.
INVALID_MODEL_STDERR_PATTERNS: tuple[str, ...] = (
    "modelnotfounderror",
    "model not found",
    "requested entity was not found",
    "not found for api version",
    "not found or is not supported",
    "not supported when using codex with a chatgpt account",
    "invalid_request_error",
)
# CLI stdout-safe subset (distinctive Codex phrasings only).
# Was ``cli/errors._INVALID_MODEL_STDOUT``.
INVALID_MODEL_STDOUT_MARKERS: tuple[str, ...] = (
    "not found or is not supported",
    "not supported when using codex with a chatgpt account",
)

INVALID_MODEL_MARKERS: frozenset[str] = frozenset(INVALID_MODEL_STDERR_PATTERNS).union(
    INVALID_MODEL_STDOUT_MARKERS
)

# ===========================================================================
# Single-stream families (match in either stderr or stdout; no precision split)
# ===========================================================================

# Codex rollout-recording internal error. Was ``cli/errors._CODEX_ROLLOUT_PATTERNS``.
CODEX_ROLLOUT_MARKERS: frozenset[str] = frozenset({"failed to record rollout items"})

# Transient network/socket failures. Was ``cli/errors._TRANSIENT_NETWORK_PATTERNS``.
TRANSIENT_NETWORK_MARKERS: frozenset[str] = frozenset(
    {
        "socket connection was closed unexpectedly",
        "connection reset by peer",
        "econnreset",
        "socket hang up",
    }
)

# Out-of-memory signatures. Was ``cli/errors._OOM_PATTERNS``.
OOM_MARKERS: frozenset[str] = frozenset(
    {
        "out of memory",
        "oomkilled",
        "enomem",
        "cannot allocate memory",
        "memory exhausted",
    }
)

# Host disk exhaustion. Was ``cli/errors._ENOSPC_PATTERNS``.
ENOSPC_MARKERS: frozenset[str] = frozenset(
    {
        "no space left on device",
        "enospc",
        "errno 28",
        "disk quota exceeded",
    }
)
