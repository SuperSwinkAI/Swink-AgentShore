"""Error classification helpers for the CLI agent adapter.

Marker tables and classification functions extracted from ``cli_agent`` so
``agents/cli/errors.py`` can serve as the authoritative home for the
``error-cooldown-unification`` plan (PLAN-error-cooldown-unification.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.errors import ErrorClass

if TYPE_CHECKING:
    from agentshore.state import AgentType

# ---------------------------------------------------------------------------
# Stderr / stdout marker tables
# ---------------------------------------------------------------------------

# YOLO permission flags applied by default per agent type. AgentShore is an
# autonomous orchestrator — agents can't pause for human approval on each
# `gh` call, so we bypass the per-tool permission gates the CLIs ship with.
# The user can opt out by explicitly setting any non-empty extra_flags in
# agentshore.yaml; that signals "I'm managing flags myself."
# Each category carries two pattern sets (#19). The *stderr* set is the full
# list: a CLI's own stderr is pure diagnostics, so matching anything there is
# safe. The *stdout-safe* set is the subset of high-precision phrases that
# effectively never appear in legitimate agent output. For a CLI **coding**
# agent, stdout is the work PRODUCT — code, diffs, tool output, model
# reasoning — and genuine quota/auth signals from `gh`/`git` tools are embedded
# there too (tool_result JSONL), so we cannot ignore stdout entirely. But the
# generic tokens ("429", "403", "forbidden", "timeout", "overloaded",
# "capacity", "model not found", ...) routinely occur in code an agent edits;
# matching those in stdout misclassified ordinary task failures as
# rate_limit/auth, corrupting the RL reward signal, firing spurious take_break
# recovery, and (for auth/invalid_model) tearing down a working agent. So the
# stdout-safe sets drop every generic token and keep only distinctive phrases.
_RATE_LIMIT_PATTERNS = (
    "rate limit",
    "rate_limit",
    "429",
    "too many requests",
    "overloaded",
    "capacity",
    "retry after",
    "throttl",
)
_RATE_LIMIT_STDOUT = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "retry after",
    "hit your session limit",
)
_AUTH_PATTERNS = (
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
    # Codex backend-session TTL-expiry signatures (#zeke auth-hang): when the
    # ChatGPT-backed session token expires mid-run the Codex CLI prints these to
    # stderr then hangs reading stdin instead of exiting non-zero. stderr-only —
    # deliberately NOT mirrored into _AUTH_STDOUT so they never match an agent's
    # own work product.
    "failed to renew cache ttl",
    "failed to refresh available models",
)
# Drops the short generic tokens ("401"/"403"/"unauthorized"/"forbidden"/
# "authentication") that appear in code; keeps the distinctive gh/git access
# strings an agent echoes from a real `gh` tool failure.
_AUTH_STDOUT = (
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
# #190: cache-renewal markers within _AUTH_PATTERNS that the Codex CLI also
# prints during a *transient* model-cache TTL blip (not a real auth rejection).
# When one of these coincides with an EOF-parse marker (below) on the same
# stderr tail it is the transient EOF-renewal shape, NOT a session-token expiry,
# and must NOT abort an in-flight dispatch (observed 415s of work killed). A
# bare cache-renewal line with no parse-EOF suffix is still a genuine token
# expiry and keeps tripping via _AUTH_PATTERNS.
_CACHE_RENEWAL_MARKERS = ("failed to renew cache ttl", "failed to refresh available models")
_PARSE_EOF_MARKERS = ("eof while parsing", "parsing a value")
_STDIN_CLOSED_AFTER_CACHE_RENEWAL_MARKERS = ("write_stdin failed", "stdin closed")


_TIMEOUT_PATTERNS = ("timeout", "timed out", "deadline exceeded", "context deadline")
# All timeout tokens are common in source code/test names — none are safe to
# match against the work product.
_TIMEOUT_STDOUT: tuple[str, ...] = ()
_INVALID_MODEL_PATTERNS = (
    "modelnotfounderror",
    "model not found",
    "requested entity was not found",
    "not found for api version",
    "not found or is not supported",
    "not supported when using codex with a chatgpt account",
    "invalid_request_error",
)
# Keep only the distinctive Codex CLI phrasings it prints to stdout; drop the
# generic "model not found"/"requested entity..."/"invalid_request_error" that
# can appear in code an agent writes (invalid_model triggers agent teardown).
_INVALID_MODEL_STDOUT = (
    "not found or is not supported",
    "not supported when using codex with a chatgpt account",
)
# Codex CLI internal error: its rollout-recording layer references a session
# thread id it can't find on disk. desktop-yxlj observed one occurrence in
# 4600+ plays. The error is permanent for the current codex process but a
# fresh `codex exec` lands a new thread id, so spawning again recovers.
# Pulling this out of the "unknown" bucket gives operators a queryable signal
# for recurrence rate and lets the existing take_break recovery path fire
# under a typed name instead of the generic catch-all.
_CODEX_ROLLOUT_PATTERNS = ("failed to record rollout items",)
# Out-of-memory signatures. An OS OOM kill usually arrives as SIGKILL (rc -9)
# with little/no agent output, but some runtimes log a signature too. Matching
# either routes the exit to ``crash_oom`` (#7) so it is NOT treated as a
# rate-limit.
_OOM_PATTERNS = (
    "out of memory",
    "oomkilled",
    "enomem",
    "cannot allocate memory",
    "memory exhausted",
)
# Host disk exhaustion surfaced by the agent subprocess (a build writing into a
# full worktree, git, npm, cargo, etc.). An environment condition, not the
# agent's fault — pulling it out of "unknown" lets operators see the real cause
# instead of blaming a code/test failure (#180). Distinctive enough to match in
# either stream.
_ENOSPC_PATTERNS = (
    "no space left on device",
    "enospc",
    "errno 28",
    "disk quota exceeded",
)
# Transient network/socket failures. claude_code has been observed to exit with
# "API Error: The socket connection was closed unexpectedly" falling into the
# generic "unknown" bucket (#23). These are distinctive enough to match
# in either stream and are genuinely transient (a retry/take_break recovers), so
# pulling them out of "unknown" gives operators an accurate signal instead of a
# catch-all while keeping the same recovery treatment.
_TRANSIENT_NETWORK_PATTERNS = (
    "socket connection was closed unexpectedly",
    "connection reset by peer",
    "econnreset",
    "socket hang up",
)

# ---------------------------------------------------------------------------
# Classification predicates
# ---------------------------------------------------------------------------


def _is_transient_cache_blip(lowered: str) -> bool:
    """#190: True iff the stderr tail is the transient cache-renewal EOF-parse blip.

    Suppresses an auth abort only for the cache-renewal-EOF shape (e.g.
    ``failed to renew cache TTL: EOF while parsing a value at line 1 column 0``).
    A real backend-auth rejection (401/403/unauthorized/invalid api key/etc.)
    is unaffected: those markers carry no cache-renewal marker, so this returns
    False and the auth hit trips normally — even if a 401 happens to coexist
    with a cache-renewal line, the presence of the genuine auth marker is what
    keeps ``feed`` matching while this guard only inspects the renewal+EOF pair.
    """
    return any(m in lowered for m in _CACHE_RENEWAL_MARKERS) and any(
        m in lowered for m in _PARSE_EOF_MARKERS
    )


def _is_cache_renewal_stdin_hang(lowered: str) -> bool:
    """#231: True when the cache-renewal EOF blip has become a stdin hang."""
    return _is_transient_cache_blip(lowered) and all(
        m in lowered for m in _STDIN_CLOSED_AFTER_CACHE_RENEWAL_MARKERS
    )


def _classify_error(rc: int, stderr: str, stdout: str) -> ErrorClass:
    """Classify a non-zero CLI exit into a semantic error bucket.

    Returns one of ``ErrorClass.RATE_LIMIT``, ``ErrorClass.AUTH``,
    ``ErrorClass.TIMEOUT``, ``ErrorClass.INVALID_MODEL``,
    ``ErrorClass.CODEX_ROLLOUT``, ``ErrorClass.TRANSIENT_NETWORK``,
    ``ErrorClass.CRASH_OOM``, ``ErrorClass.CRASH_SIGNAL``, or
    ``ErrorClass.UNKNOWN`` (each a ``str`` subclass, so callers comparing
    against the bare strings keep working).

    *stderr* is matched against the full pattern set; the trailing 1 000 chars
    of *stdout* are matched only against each category's high-precision
    stdout-safe subset (#19). stdout is inspected at all because some CLIs
    (notably Claude Code) report quota exhaustion on stdout with nothing on
    stderr, and `gh`/`git` tool failures surface in the agent's stdout JSONL —
    but stdout is also the coding agent's work product, so matching generic
    tokens there misclassified ordinary task failures (e.g. a failed file edit)
    as rate_limit/auth. ``rc`` is inspected for signal deaths last, so an
    explicit content message still wins over the raw return code.
    """
    err = stderr.lower()
    out = stdout[-1000:].lower()

    def hit(stderr_patterns: tuple[str, ...], stdout_patterns: tuple[str, ...]) -> bool:
        return any(p in err for p in stderr_patterns) or any(p in out for p in stdout_patterns)

    if hit(_RATE_LIMIT_PATTERNS, _RATE_LIMIT_STDOUT):
        return ErrorClass.RATE_LIMIT
    if hit(_AUTH_PATTERNS, _AUTH_STDOUT):
        return ErrorClass.AUTH
    if hit(_TIMEOUT_PATTERNS, _TIMEOUT_STDOUT):
        return ErrorClass.TIMEOUT
    if hit(_INVALID_MODEL_PATTERNS, _INVALID_MODEL_STDOUT):
        return ErrorClass.INVALID_MODEL
    # codex_rollout + OOM signatures are distinctive enough to match in either
    # stream (an OOM "Out of memory" notice legitimately lands on stdout).
    combined = err + out
    if any(p in combined for p in _CODEX_ROLLOUT_PATTERNS):
        return ErrorClass.CODEX_ROLLOUT
    if any(p in combined for p in _TRANSIENT_NETWORK_PATTERNS):
        return ErrorClass.TRANSIENT_NETWORK
    if any(p in combined for p in _ENOSPC_PATTERNS):
        return ErrorClass.CRASH_ENOSPC
    if any(p in combined for p in _OOM_PATTERNS):
        return ErrorClass.CRASH_OOM
    # Negative return codes are POSIX signal deaths. SIGKILL (-9) from the OS
    # OOM killer or an external kill is a crash, NOT a rate limit — bucketing it
    # as "unknown" routed it into rate-limit take_break recovery (#7). SIGTERM
    # (-15) and SIGINT (-2) are graceful AgentShore/OS-initiated stops and keep
    # falling through to "unknown".
    if rc < 0 and rc not in (-2, -15):
        return ErrorClass.CRASH_SIGNAL
    return ErrorClass.UNKNOWN


def _process_error_detail(
    *,
    agent_type: AgentType,
    model: str | None,
    error_class: ErrorClass,
    stderr: str,
    stdout: str,
) -> str:
    """Return a concise user-facing subprocess error detail."""
    if error_class == ErrorClass.INVALID_MODEL:
        model_text = f" model {model!r}" if model else ""
        report = _extract_cli_report_path(stderr)
        suffix = f" Full report: {report}" if report else ""
        return (
            f"{agent_type.value}{model_text} is not available to the CLI/API "
            f"(invalid or unsupported model). "
            f"Check agents.{agent_type.value}.model_tiers in agentshore.yaml.{suffix}"
        )

    cleaned = _clean_stderr(stderr)
    if cleaned:
        return cleaned[:500]
    return stdout[-200:] if stdout else "(no output)"


def _extract_cli_report_path(stderr: str) -> str | None:
    marker = "Full report available at:"
    if marker not in stderr:
        return None
    tail = stderr.split(marker, 1)[1].strip()
    return tail.split(None, 1)[0] if tail else None


def _clean_stderr(stderr: str) -> str:
    noisy_prefixes = (
        "YOLO mode is enabled.",
        "Ripgrep is not available.",
        "Falling back to GrepTool.",
    )
    lines = [
        line.strip()
        for line in stderr.splitlines()
        if line.strip() and not line.strip().startswith(noisy_prefixes)
    ]
    return "\n".join(lines)
