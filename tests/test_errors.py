"""Regression coverage for the public error taxonomy."""

from __future__ import annotations

import pytest

from agentshore.errors import FailureCategory, OrchestratorError, PlayTimeoutError


def test_failure_category_matches_persisted_play_categories() -> None:
    assert {category.value for category in FailureCategory} == {
        "agent_error",
        "alignment_drift",
        "code_error",
        "gate_rejection",
        "test_failure",
    }


@pytest.mark.parametrize(
    ("recoverable", "recovery_action"),
    [
        (None, None),
        (False, "surface to human"),
    ],
)
def test_orchestrator_error_overrides_are_instance_scoped(
    recoverable: bool | None,
    recovery_action: str | None,
) -> None:
    exc = OrchestratorError(
        "failed",
        recoverable=recoverable,
        recovery_action=recovery_action,
    )
    assert exc.message == "failed"
    assert exc.recoverable is (True if recoverable is None else recoverable)
    assert exc.recovery_action == (recovery_action or "none")


def test_play_timeout_error_preserves_error_class() -> None:
    exc = PlayTimeoutError("timed out", error_class="stalled")
    assert exc.error_type == "agent_timeout"
    assert exc.error_class == "stalled"
