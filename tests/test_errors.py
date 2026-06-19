"""Regression coverage for the public error taxonomy."""

from __future__ import annotations

import errno

import pytest

from agentshore.errors import (
    ErrorClass,
    FailureCategory,
    OrchestratorError,
    PlayTimeoutError,
    is_disk_full,
)


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


def test_crash_enospc_is_a_member() -> None:
    assert ErrorClass.CRASH_ENOSPC == "crash_enospc"


def test_errorclass_coerce_passes_through_enum() -> None:
    assert ErrorClass.coerce(ErrorClass.AUTH) is ErrorClass.AUTH


def test_errorclass_coerce_maps_known_string() -> None:
    assert ErrorClass.coerce("rate_limit") is ErrorClass.RATE_LIMIT
    assert ErrorClass.coerce("timeout_stream_idle") is ErrorClass.TIMEOUT_STREAM_IDLE


@pytest.mark.parametrize("garbage", ["author", "nonsense", "", None, 42, object()])
def test_errorclass_coerce_collapses_unknown_to_unknown(garbage: object) -> None:
    assert ErrorClass.coerce(garbage) is ErrorClass.UNKNOWN


def test_is_disk_full_direct_enospc() -> None:
    exc = OSError(errno.ENOSPC, "No space left on device")
    assert is_disk_full(exc) is True


def test_is_disk_full_walks_cause_chain() -> None:
    inner = OSError(errno.ENOSPC, "No space left on device")
    try:
        try:
            raise inner
        except OSError as e:
            raise RuntimeError("worktree allocation failed") from e
    except RuntimeError as wrapped:
        assert is_disk_full(wrapped) is True


def test_is_disk_full_false_for_other_oserror() -> None:
    assert is_disk_full(OSError(errno.EACCES, "permission denied")) is False
    assert is_disk_full(ValueError("nope")) is False
