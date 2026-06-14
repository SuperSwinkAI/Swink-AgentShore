"""Error taxonomy, recovery strategies, and escalation."""

from __future__ import annotations

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
    UNKNOWN = "unknown"


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
