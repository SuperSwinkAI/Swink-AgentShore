"""Error taxonomy, recovery strategies, and escalation."""

from __future__ import annotations

import errno
from enum import StrEnum

# Canonical substrings that mark a GitHub authentication / access failure in a
# free-form skill or command error string. Single home for the auth-marker table
# that ``plays/skill_backed/base.py`` matches against when deciding whether a
# skill failure is an auth failure (``FailureKind.AUTH``) and should mark the
# agent in error. The narrower publish-scoped subset in
# ``plays/_publish_reconciler.py`` is intentionally distinct (it scopes
# issue-pickup publish-failure recovery, not general auth detection).
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
    # Host disk exhaustion (ENOSPC / "no space left on device"). An *environment*
    # condition, not the agent's fault: retrying into a full disk just burns
    # spend, so callers treat it as fatal-environment rather than a recoverable
    # per-play failure (#180).
    CRASH_ENOSPC = "crash_enospc"
    CRASH_SIGNAL = "crash_signal"
    TIMEOUT_TRANSIENT = "timeout_transient"
    # Timeout sub-classes carried on PlayTimeoutError.error_class and threaded
    # onto last_error_class via the manager dispatch handler. Distinct strings
    # asserted by tests (test_agent_manager / test_cli_agent), so they must be
    # first-class members rather than collapsing to TIMEOUT/UNKNOWN.
    TIMEOUT_WALLCLOCK = "timeout_wallclock"
    TIMEOUT_POST_RESPONSE = "timeout_post_response"
    TIMEOUT_STREAM_IDLE = "timeout_stream_idle"
    OUTPUT_INVALID = "output_invalid"
    # A clean-exit empty no-op: the agent's process exited 0 but produced no
    # output at all (no JSON result block, no text). Antigravity (`agy`) does
    # this non-deterministically — it returns an empty task envelope (5–14 s,
    # 0 tokens, 0 turns) instead of doing the work. Recoverable and treated like
    # a transient quota/throttle: 3 consecutive no-ops route the agent into the
    # standard take_break (desktop no-op resilience). Kept distinct from UNKNOWN
    # so no-ops are separable from genuine unknown errors in telemetry.
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
    recoverable: bool = True
    recovery_action: str = "none"

    def __init__(
        self,
        message: str,
        *,
        recoverable: bool | None = None,
        recovery_action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if recoverable is not None:
            self.recoverable = recoverable
        if recovery_action is not None:
            self.recovery_action = recovery_action


# --- Config ---


class ConfigError(OrchestratorError):
    error_type = "config_error"
    recoverable = False
    recovery_action = "fix configuration and restart"


# --- Agent errors ---


class AgentProcessCrashed(OrchestratorError):
    error_type = "agent_process_crashed"
    recoverable = True
    recovery_action = "mark agent as error, re-instantiate if budget allows"


class AgentTimeout(OrchestratorError):
    error_type = "agent_timeout"
    recoverable = True
    recovery_action = "kill subprocess, record as failed play"


class PlayTimeoutError(AgentTimeout):
    """Raised when a play-level timeout fires (per-agent `timeout` config)."""

    def __init__(
        self,
        message: str,
        *,
        error_class: ErrorClass | str = ErrorClass.TIMEOUT,
        recoverable: bool | None = None,
        recovery_action: str | None = None,
    ) -> None:
        super().__init__(message, recoverable=recoverable, recovery_action=recovery_action)
        self.error_class = error_class


# Alias used in planning docs — maps to the canonical class name.
AgentProcessError = AgentProcessCrashed


class AgentOutputInvalid(OrchestratorError):
    error_type = "agent_output_invalid"
    recoverable = True
    recovery_action = "log raw output, mark play as failed"


class AgentAPIError(OrchestratorError):
    error_type = "agent_api_error"
    recoverable = True
    recovery_action = "retry with exponential backoff"


class AgentRateLimitError(AgentAPIError):
    error_type = "agent_rate_limit"
    recoverable = True
    recovery_action = "back off before retrying this agent"


class AgentAuthError(OrchestratorError):
    error_type = "agent_auth_error"
    recoverable = False
    recovery_action = "halt agent, surface to human"


# --- Play errors ---


class PreconditionFailed(OrchestratorError):
    error_type = "precondition_failed"
    recoverable = True
    recovery_action = "skip play, select again"


class PlayExecutionFailed(OrchestratorError):
    error_type = "play_execution_failed"
    recoverable = True
    recovery_action = "record as failed play, RL adjusts"


class AntiConfirmationViolation(OrchestratorError):
    error_type = "anti_confirmation_violation"
    recoverable = False
    recovery_action = "hard block, diagnose root cause"


class InstantiationDenied(OrchestratorError):
    error_type = "instantiation_denied"
    recoverable = False
    recovery_action = "surface to human, suggest end session or adjust budget"


class FreshStartFailed(OrchestratorError):
    error_type = "fresh_start_failed"
    recoverable = True
    recovery_action = "attempt clean agent instantiation without context"


class RevertFailed(OrchestratorError):
    error_type = "revert_failed"
    recoverable = False
    recovery_action = "surface to human for manual intervention"


class LearningExtractionFailed(OrchestratorError):
    error_type = "learning_extraction_failed"
    recoverable = True
    recovery_action = "record as partial success, retry later"


class IntakeParseError(OrchestratorError):
    error_type = "intake_parse_error"
    recoverable = False
    recovery_action = "escalate to human with description of what failed"


class IssueInflationDetected(OrchestratorError):
    error_type = "issue_inflation_detected"
    recoverable = True
    recovery_action = "RL receives inflation penalty"


# --- RL errors ---


class PolicyNaN(OrchestratorError):
    error_type = "policy_nan"
    recoverable = True
    recovery_action = "rollback to last checkpoint"


class NoValidActions(OrchestratorError):
    error_type = "no_valid_actions"
    recoverable = False
    recovery_action = "force end session"


class RewardComputationFailed(OrchestratorError):
    error_type = "reward_computation_failed"
    recoverable = True
    recovery_action = "use zero reward for this step"


# --- System errors ---


class DatabaseError(OrchestratorError):
    error_type = "database_error"
    recoverable = False
    recovery_action = "surface to human, pause orchestrator"


class SocketError(OrchestratorError):
    error_type = "socket_error"
    recoverable = True
    recovery_action = "continue headless, re-listen on socket"
