"""Error-classification marker tables and derived canonical supersets.

Consumer-specific views retain their matched sets. High-precision stdout
and GitHub skill-error views remain narrower than the canonical AUTH_MARKERS."""

from __future__ import annotations

from agentshore.errors import GITHUB_AUTH_ERROR_MARKERS

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

STDERR_AUTH_PATTERNS: tuple[str, ...] = (
    "unauthorized",
    "http 401",
    "401 unauthorized",
    "authentication",
    "invalid api key",
    "bad credentials",
    "forbidden",
    "http 403",
    "403 forbidden",
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
    "not signed in",
)

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

CACHE_RENEWAL_MARKERS: frozenset[str] = frozenset(
    {"failed to renew cache ttl", "failed to refresh available models"}
)

AUTH_MARKERS: frozenset[str] = frozenset(GITHUB_AUTH_ERROR_MARKERS).union(
    PUBLISH_AUTH_MARKERS, STDERR_AUTH_PATTERNS, STDOUT_AUTH_MARKERS
)


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

RATE_LIMIT_STDOUT_MARKERS: tuple[str, ...] = (
    "rate limit",
    "too many requests",
    "retry after",
    "hit your session limit",
    "hit your usage limit",
)

RATE_LIMIT_MARKERS: frozenset[str] = frozenset(RATE_LIMIT_STDERR_PATTERNS).union(
    RATE_LIMIT_STDOUT_MARKERS
)


TIMEOUT_STDERR_PATTERNS: tuple[str, ...] = (
    "timeout",
    "timed out",
    "deadline exceeded",
    "context deadline",
)
TIMEOUT_STDOUT_MARKERS: tuple[str, ...] = ()

TIMEOUT_MARKERS: frozenset[str] = frozenset(TIMEOUT_STDERR_PATTERNS).union(TIMEOUT_STDOUT_MARKERS)


INVALID_MODEL_STDERR_PATTERNS: tuple[str, ...] = (
    "modelnotfounderror",
    "model not found",
    "requested entity was not found",
    "not found for api version",
    "not found or is not supported",
    "not supported when using codex with a chatgpt account",
    "invalid_request_error",
    "does not exist or you do not have access",
    "unrecognized_model",
    "issue with the selected model",
    "invalid model selection",
    "is not recognized as a known model",
    "unknown tier",
    "from --model flag is not available",
)
INVALID_MODEL_STDOUT_MARKERS: tuple[str, ...] = (
    "not found or is not supported",
    "not supported when using codex with a chatgpt account",
)

INVALID_MODEL_MARKERS: frozenset[str] = frozenset(INVALID_MODEL_STDERR_PATTERNS).union(
    INVALID_MODEL_STDOUT_MARKERS
)


CODEX_ROLLOUT_MARKERS: frozenset[str] = frozenset({"failed to record rollout items"})

TRANSIENT_NETWORK_MARKERS: frozenset[str] = frozenset(
    {
        "socket connection was closed unexpectedly",
        "connection reset by peer",
        "econnreset",
        "socket hang up",
    }
)

OOM_MARKERS: frozenset[str] = frozenset(
    {
        "out of memory",
        "oomkilled",
        "enomem",
        "cannot allocate memory",
        "memory exhausted",
    }
)

ENOSPC_MARKERS: frozenset[str] = frozenset(
    {
        "no space left on device",
        "enospc",
        "errno 28",
        "os error 28",
        "disk quota exceeded",
    }
)
