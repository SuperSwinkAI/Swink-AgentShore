"""Error taxonomy, recovery strategies, and escalation."""

from __future__ import annotations

import errno
from enum import StrEnum

GITHUB_AUTH_ERROR_MARKERS: tuple[str, ...] = (
    "bad credentials",
    "http 401",
    "401 unauthorized",
    "http 403",
    "403 forbidden",
    "irrecoverable github access failure",
    "github connector returned 404",
    "connector repo 404",
    "repository not found",
    "could not resolve to a repository with the name",
    "could not resolve to a repository",
    "repository/pr is not accessible",
    "not found/could not resolve repository",
    "repository is not resolvable to this token",
    "not resolvable to this token/session",
    "lacks access to repository",
    "cannot access repository metadata",
    "active gh_token account lacks",
)


class FailureCategory(StrEnum):
    code_error = "code_error"
    test_failure = "test_failure"
    alignment_drift = "alignment_drift"
    agent_error = "agent_error"
    gate_rejection = "gate_rejection"


class FailureKind(StrEnum):
    """Typed cause a play sets at the failure site, where the cause is known.

    Distinct from :class:`FailureCategory`, the wire/string taxonomy persisted
    to the plays table and consumed by reward filtering, dashboard styling, and
    ESR rollups. ``failure_kind`` is the structured signal; ``failure_category``
    is derived from it via :meth:`to_category` so those consumers keep working.
    The substring inferer remains the fallback for legacy/uncaught paths that
    never set a kind.
    """

    AUTH = "auth"
    TEST = "test"
    GATE = "gate"
    SCOPE = "scope"
    AGENT_ERROR = "agent_error"
    CODE_ERROR = "code_error"

    def to_category(self) -> FailureCategory:
        """Map a typed failure kind to its persisted ``FailureCategory`` string."""
        return _FAILURE_KIND_TO_CATEGORY[self]


_FAILURE_KIND_TO_CATEGORY: dict[FailureKind, FailureCategory] = {
    FailureKind.AUTH: FailureCategory.agent_error,
    FailureKind.TEST: FailureCategory.test_failure,
    FailureKind.GATE: FailureCategory.gate_rejection,
    FailureKind.SCOPE: FailureCategory.alignment_drift,
    FailureKind.AGENT_ERROR: FailureCategory.agent_error,
    FailureKind.CODE_ERROR: FailureCategory.code_error,
}


class ErrorClass(StrEnum):
    """Canonical agent error classifications (was stringly-typed).

    A ``StrEnum`` (``str`` subclass), so existing ``frozenset[str]`` membership
    tests and ``== "..."`` comparisons keep working unchanged. Every value ever
    assigned to ``AgentHandle.last_error_class`` MUST be a member here, or a
    coercion at the manager boundary would silently collapse it to ``UNKNOWN``.
    """

    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    TIMEOUT = "timeout"
    INVALID_MODEL = "invalid_model"
    CODEX_ROLLOUT = "codex_rollout"
    TRANSIENT_NETWORK = "transient_network"
    CRASH_OOM = "crash_oom"
    CRASH_ENOSPC = "crash_enospc"
    CRASH_SIGNAL = "crash_signal"
    TIMEOUT_TRANSIENT = "timeout_transient"
    TIMEOUT_WALLCLOCK = "timeout_wallclock"
    TIMEOUT_POST_RESPONSE = "timeout_post_response"
    TIMEOUT_STREAM_IDLE = "timeout_stream_idle"
    OUTPUT_INVALID = "output_invalid"
    NO_OP = "no_op"
    UNKNOWN = "unknown"

    @classmethod
    def coerce(cls, value: object) -> ErrorClass:
        """Coerce an arbitrary value to an ``ErrorClass``, collapsing unknowns.

        Accepts an existing :class:`ErrorClass` (returned as-is) or a string that
        names a member; anything else — an unrecognised string, ``None``, or a
        non-string — becomes :attr:`UNKNOWN` rather than persisting an
        unclassified value to ``last_error_class``. This is the single home for
        the manager-boundary guard that previously inlined
        ``ErrorClass(x) if x in ErrorClass._value2member_map_ else UNKNOWN``.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str) and value in cls._value2member_map_:
            return cls(value)
        return cls.UNKNOWN


def is_disk_full(exc: BaseException) -> bool:
    """True if *exc* (or its cause/context chain) is an ENOSPC ``OSError``.

    Build-agnostic detection of host disk exhaustion. Worktree allocation and
    other I/O wrap the underlying ``OSError`` (errno 28) at varying depths, so
    walk the ``__cause__`` / ``__context__`` chain rather than matching message
    strings. Used to route disk-full into the fatal-environment path instead of
    a recoverable per-play retry (#180).
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, OSError) and cur.errno == errno.ENOSPC:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


class OrchestratorError(Exception):
    error_type: str = "agentshore_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConfigError(OrchestratorError):
    error_type = "config_error"


class AgentProcessCrashed(OrchestratorError):
    error_type = "agent_process_crashed"


class AgentTimeout(OrchestratorError):
    error_type = "agent_timeout"


class PlayTimeoutError(AgentTimeout):
    """Raised when a play-level timeout fires (per-agent `timeout` config)."""

    def __init__(
        self,
        message: str,
        *,
        error_class: ErrorClass | str = ErrorClass.TIMEOUT,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class


AgentProcessError = AgentProcessCrashed


class AgentOutputInvalid(OrchestratorError):
    error_type = "agent_output_invalid"


class AgentAPIError(OrchestratorError):
    error_type = "agent_api_error"


class AgentRateLimitError(AgentAPIError):
    error_type = "agent_rate_limit"


class AgentAuthError(OrchestratorError):
    error_type = "agent_auth_error"


class PreconditionFailed(OrchestratorError):
    error_type = "precondition_failed"


class PlayExecutionFailed(OrchestratorError):
    error_type = "play_execution_failed"


class AntiConfirmationViolation(OrchestratorError):
    error_type = "anti_confirmation_violation"


class InstantiationDenied(OrchestratorError):
    error_type = "instantiation_denied"


class FreshStartFailed(OrchestratorError):
    error_type = "fresh_start_failed"


class RevertFailed(OrchestratorError):
    error_type = "revert_failed"


class LearningExtractionFailed(OrchestratorError):
    error_type = "learning_extraction_failed"


class IntakeParseError(OrchestratorError):
    error_type = "intake_parse_error"


class IssueInflationDetected(OrchestratorError):
    error_type = "issue_inflation_detected"


class PolicyNaN(OrchestratorError):
    error_type = "policy_nan"


class NoValidActions(OrchestratorError):
    error_type = "no_valid_actions"


class RewardComputationFailed(OrchestratorError):
    error_type = "reward_computation_failed"


class DatabaseError(OrchestratorError):
    error_type = "database_error"


class DatabaseLockedError(DatabaseError):
    """Store init exhausted its lock-retry budget (#283).

    Raised when ``DataStore.initialize`` could not acquire the SQLite
    writer lock within the bounded retry window — typically because a
    previous session's store ``close()`` (snapshot + ``os.replace`` of the
    DB file) was still in flight. Distinct from the generic
    :class:`DatabaseError` so callers can render an actionable "database
    busy, retry" message instead of a bare ``OperationalError`` string.
    """

    error_type = "database_locked"


class SocketError(OrchestratorError):
    error_type = "socket_error"
