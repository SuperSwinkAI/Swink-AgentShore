"""Drift-pin tying take_break recovery routing to the recoverable-class set.

Phase 3 of the error/cooldown unification collapsed three separate recovery
frozensets into one ``_RECOVERY_OVERRIDE_KIND`` map and reconciled the one
verified discrepancy (codex_rollout was routed for a take_break but absent from
``RECOVERABLE_ERROR_CLASSES``, so eligibility treated it as terminal). These
tests keep the two in lockstep so that drift can't silently return.
"""

from __future__ import annotations

from agentshore.core.recovery_tracker import _RECOVERY_OVERRIDE_KIND
from agentshore.errors import ErrorClass
from agentshore.plays.override import OverrideKind
from agentshore.state import RECOVERABLE_ERROR_CLASSES


def test_routing_domain_equals_recoverable_set() -> None:
    # A class is recoverable for eligibility iff it routes to a take_break kind.
    assert set(_RECOVERY_OVERRIDE_KIND) == RECOVERABLE_ERROR_CLASSES


def test_recoverable_set_is_exactly_the_expected_classes() -> None:
    assert (
        frozenset(
            {
                ErrorClass.RATE_LIMIT,
                ErrorClass.UNKNOWN,
                ErrorClass.TRANSIENT_NETWORK,
                ErrorClass.CODEX_ROLLOUT,
                ErrorClass.NO_OP,
            }
        )
        == RECOVERABLE_ERROR_CLASSES
    )


def test_routing_kinds_match_the_pre_collapse_frozensets() -> None:
    # Reproduces the three original frozensets' routing exactly (plus codex_rollout
    # now consistently recoverable).
    assert _RECOVERY_OVERRIDE_KIND[ErrorClass.RATE_LIMIT] is OverrideKind.RATE_LIMIT_RECOVERY
    assert _RECOVERY_OVERRIDE_KIND[ErrorClass.NO_OP] is OverrideKind.NOOP_RECOVERY
    for ec in (ErrorClass.UNKNOWN, ErrorClass.CODEX_ROLLOUT, ErrorClass.TRANSIENT_NETWORK):
        assert _RECOVERY_OVERRIDE_KIND[ec] is OverrideKind.UNKNOWN_ERROR_RECOVERY


def test_non_recoverable_classes_are_unrouted() -> None:
    for ec in (
        ErrorClass.AUTH,
        ErrorClass.INVALID_MODEL,
        ErrorClass.CRASH_OOM,
        ErrorClass.CRASH_ENOSPC,
        ErrorClass.CRASH_SIGNAL,
        ErrorClass.TIMEOUT,
        ErrorClass.OUTPUT_INVALID,
    ):
        assert ec not in _RECOVERY_OVERRIDE_KIND
        assert ec not in RECOVERABLE_ERROR_CLASSES
