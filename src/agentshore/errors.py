"""Error taxonomy, recovery strategies, and escalation."""

from __future__ import annotations

from enum import StrEnum


class FailureCategory(StrEnum):
    code_error = "code_error"
    test_failure = "test_failure"
    alignment_drift = "alignment_drift"
    agent_error = "agent_error"
    gate_rejection = "gate_rejection"


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
        error_class: str = "timeout",
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


class BrowserVerificationFailed(OrchestratorError):
    error_type = "browser_verification_failed"
    recoverable = True
    recovery_action = "log failure, RL adjusts"


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
