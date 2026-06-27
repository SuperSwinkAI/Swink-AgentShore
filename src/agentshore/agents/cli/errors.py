"""Error classification helpers for the CLI agent adapter.

Marker tables and classification functions extracted from ``cli_agent`` so
``agents/cli/errors.py`` can serve as the authoritative home for the
``error-cooldown-unification`` plan (PLAN-error-cooldown-unification.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.error_markers import (
    AUTH_MARKERS,
)
from agentshore.error_markers import (
    CACHE_RENEWAL_MARKERS as _CACHE_RENEWAL_MARKERS,
)
from agentshore.error_markers import (
    CODEX_ROLLOUT_MARKERS as _CODEX_ROLLOUT_PATTERNS,
)
from agentshore.error_markers import (
    ENOSPC_MARKERS as _ENOSPC_PATTERNS,
)
from agentshore.error_markers import (
    INVALID_MODEL_STDERR_PATTERNS as _INVALID_MODEL_PATTERNS,
)
from agentshore.error_markers import (
    INVALID_MODEL_STDOUT_MARKERS as _INVALID_MODEL_STDOUT,
)
from agentshore.error_markers import (
    OOM_MARKERS as _OOM_PATTERNS,
)
from agentshore.error_markers import (
    RATE_LIMIT_STDERR_PATTERNS as _RATE_LIMIT_PATTERNS,
)
from agentshore.error_markers import (
    RATE_LIMIT_STDOUT_MARKERS as _RATE_LIMIT_STDOUT,
)
from agentshore.error_markers import (
    STDERR_AUTH_PATTERNS as _AUTH_PATTERNS,
)
from agentshore.error_markers import (
    STDOUT_AUTH_MARKERS as _AUTH_STDOUT,
)
from agentshore.error_markers import (
    TIMEOUT_STDERR_PATTERNS as _TIMEOUT_PATTERNS,
)
from agentshore.error_markers import (
    TIMEOUT_STDOUT_MARKERS as _TIMEOUT_STDOUT,
)
from agentshore.error_markers import (
    TRANSIENT_NETWORK_MARKERS as _TRANSIENT_NETWORK_PATTERNS,
)
from agentshore.errors import ErrorClass

if TYPE_CHECKING:
    from agentshore.state import AgentType

# Explicit re-export surface (mypy strict ``implicit_reexport=False``): the
# marker names imported-and-aliased from ``error_markers`` above, plus the local
# classifiers, are pulled by ``agents/cli/watchdogs.py`` and the ``cli_agent``
# shim from this module, so they must be declared exported here.
__all__ = [
    "_AUTH_PATTERNS",
    "_AUTH_STDOUT",
    "_CACHE_RENEWAL_MARKERS",
    "_CODEX_ROLLOUT_PATTERNS",
    "_ENOSPC_PATTERNS",
    "_INVALID_MODEL_PATTERNS",
    "_INVALID_MODEL_STDOUT",
    "_OOM_PATTERNS",
    "_PARSE_EOF_MARKERS",
    "_RATE_LIMIT_PATTERNS",
    "_RATE_LIMIT_STDOUT",
    "_STDIN_CLOSED_AFTER_CACHE_RENEWAL_MARKERS",
    "_TIMEOUT_PATTERNS",
    "_TIMEOUT_STDOUT",
    "_TRANSIENT_NETWORK_PATTERNS",
    "_classify_error",
    "_clean_stderr",
    "_extract_cli_report_path",
    "_is_cache_renewal_stdin_hang",
    "_is_transient_cache_blip",
    "_process_error_detail",
]

# ---------------------------------------------------------------------------
# Marker tables
# ---------------------------------------------------------------------------
#
# Error-class vocabularies live in the shared ``error_markers`` registry
# (imported above). Each has two views: the *stderr* set matches broadly (a
# CLI's stderr is pure diagnostics); the *stdout-safe* subset keeps only phrases
# that never appear in legitimate agent work product (#19).
#
# These two stay CLI-transport local — they detect a Codex cache-renewal
# EOF/stdin-hang blip's *shape*, not an error class, so they don't belong in the
# shared registry.
_PARSE_EOF_MARKERS = ("eof while parsing", "parsing a value")
_STDIN_CLOSED_AFTER_CACHE_RENEWAL_MARKERS = ("write_stdin failed", "stdin closed")

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
    # stderr matches the broad canonical AUTH_MARKERS superset (Phase 4: all auth
    # spellings share one table — adds the phrased "http 401/403" / GitHub-table
    # strings, on top of the bare 401/403/forbidden tokens already present). stdout
    # stays on the narrow high-precision subset so an agent's work product (code it
    # edits mentioning 401/403/forbidden) is never misclassified (#19).
    if hit(tuple(AUTH_MARKERS), _AUTH_STDOUT):
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


#: Substring marking a Claude Code SessionEnd-hook line in stderr. SessionEnd
#: hooks run at teardown — strictly AFTER the model's final response is printed
#: to stdout in headless ``claude -p`` — so a non-zero exit caused solely by a
#: hook failure/cancellation does not invalidate the already-emitted response.
_SESSION_END_HOOK_MARKER = "sessionend hook"


def is_post_response_hook_failure(stderr: str) -> bool:
    """True when a non-zero exit is attributable *only* to SessionEnd hook failures.

    Claude Code runs ``SessionEnd`` hooks at teardown, after the model's final
    response (including its JSON result block) has already been written to
    stdout. When such a hook fails or is cancelled the CLI exits non-zero even
    though the dispatch's actual work completed — discarding it as
    ``error_class=unknown`` burns minutes of finished work and a dispatch (#253).

    Recognise the case conservatively: every non-empty stderr line must be a
    SessionEnd-hook line. Any other stderr content (a real crash, auth error,
    tool failure) leaves a non-hook line and falls through to normal failure
    classification, so this never masks a genuine error.
    """
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return False
    return all(_SESSION_END_HOOK_MARKER in line.lower() for line in lines)


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
