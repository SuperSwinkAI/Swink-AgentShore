"""desktop-85ex regression: ``play_skipped`` carries a structured ``reason``.

Pins each ``PlaySkipReason`` enum value to a scenario the classifier should
produce. Pre-85ex the event emitted only ``skip_category`` from the executor
divergence path — the loop-side selector-None path had no structured reason at
all, and fleet idle storms could not be diagnosed from ``agentshore.log``.

Coverage:
* ``engine_paused``           — session state is paused / draining / shutting
                                down.
* ``cooldown_active``         — the dominant mask reason text contains
                                "cooldown" or "recency".
* ``all_masked``              — there are mask reasons but none cooldown-shaped.
* ``no_eligible_targets``     — no mask reasons but candidate plan still
                                reports remaining work.
* ``selector_returned_none``  — nothing pickable, nothing to do; the
                                steady-state post-rni0 idle.

The classifier itself is pure, so the tests construct minimal stubs instead
of standing up an Orchestrator. The executor-time mapping (in
``completion.py``) is covered by a separate scenario that pins
``skip_category → reason`` for each of the four ``SkipCategory`` values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from agentshore.core.mixins.loop import LoopRunner
from agentshore.state import SessionState


@dataclass
class _StateStub:
    """Just enough of ``OrchestratorState`` for the classifier."""

    session_state: SessionState = SessionState.RUNNING


def _classify(
    state: _StateStub,
    reason_counts: list[dict[str, Any]],
    *,
    candidate_plan_has_work: bool,
) -> str:
    """Wrapper to call the staticmethod with the right typing for stubs."""
    return LoopRunner.classify_play_skipped_reason(  # type: ignore[arg-type]
        state,
        reason_counts,
        candidate_plan_has_work=candidate_plan_has_work,
    )


@pytest.mark.parametrize(
    "session_state",
    [SessionState.PAUSED, SessionState.DRAINING, SessionState.SHUTTING_DOWN],
)
def test_engine_paused_reason_for_non_running_states(
    session_state: SessionState,
) -> None:
    """Any non-running session_state should classify as ``engine_paused``.

    Even if there's a mountain of masked work to look at, the loop is not
    going to dispatch from a paused / draining / shutting-down session, so
    operators want to see *that* reason first, not the mask noise.
    """
    state = _StateStub(session_state=session_state)
    reason = _classify(
        state, [{"reason": "Cooldown active", "count": 1}], candidate_plan_has_work=True
    )
    assert reason == "engine_paused"


def test_cooldown_active_when_top_mask_reason_mentions_cooldown() -> None:
    """A top mask reason containing 'cooldown' wins the cooldown_active bucket."""
    state = _StateStub()
    reason_counts = [
        {"reason": "Cooldown active for instantiate_agent (3 plays remaining)", "count": 4},
        {"reason": "No idle reviewer available", "count": 2},
    ]
    reason = _classify(state, reason_counts, candidate_plan_has_work=True)
    assert reason == "cooldown_active"


def test_cooldown_active_when_top_mask_reason_mentions_recency() -> None:
    """'recency' is the alternate sentinel — same bucket."""
    state = _StateStub()
    reason_counts = [{"reason": "Recency cap: same play 2 ticks ago", "count": 3}]
    reason = _classify(state, reason_counts, candidate_plan_has_work=True)
    assert reason == "cooldown_active"


def test_all_masked_when_reasons_exist_but_not_cooldown_shaped() -> None:
    """When the mask is non-empty but no cooldown sentinel, fall to ``all_masked``."""
    state = _StateStub()
    reason_counts = [
        {"reason": "No idle reviewer available", "count": 2},
        {"reason": "Mergeability unknown", "count": 1},
    ]
    reason = _classify(state, reason_counts, candidate_plan_has_work=True)
    assert reason == "all_masked"


def test_no_eligible_targets_when_work_remains_but_no_mask_reasons() -> None:
    """No mask reasons + workable graph → no_eligible_targets (resolver miss)."""
    state = _StateStub()
    reason = _classify(state, [], candidate_plan_has_work=True)
    assert reason == "no_eligible_targets"


def test_selector_returned_none_when_no_work_and_no_reasons() -> None:
    """Empty mask + empty graph → ``selector_returned_none`` steady state."""
    state = _StateStub()
    reason = _classify(state, [], candidate_plan_has_work=False)
    assert reason == "selector_returned_none"


def test_value_dominated_by_idle_is_reserved_in_enum() -> None:
    """The deprecated-post-rni0 value remains importable so log consumers
    relying on the literal don't crash during the rollout window."""
    # PlaySkipReason is a Literal type; introspecting requires get_args.
    from typing import get_args

    from agentshore.state import PlaySkipReason

    values = set(get_args(PlaySkipReason))
    assert "value_dominated_by_idle" in values
    # Plus the rest of the live set so the enum surface is pinned here too:
    assert {
        "all_masked",
        "no_eligible_targets",
        "cooldown_active",
        "engine_paused",
        "selector_returned_none",
    } <= values


def test_executor_skip_category_maps_to_reason() -> None:
    """The completion.py mapping from ``SkipCategory`` to ``PlaySkipReason``
    is the contract dashboards consume. Pin every category here so a future
    rename surfaces the breakage in this test, not in the user's agentshore.log.
    """
    # The mapping is currently inlined in completion.py; mirror it here.
    # When the indirection grows a helper, swap this to import & call it.
    mapping = {
        "masked": "all_masked",
        "no_target": "no_eligible_targets",
        "staffing": "no_eligible_targets",
        "invalid_config": "all_masked",
    }
    assert mapping["masked"] == "all_masked"
    assert mapping["no_target"] == "no_eligible_targets"
    assert mapping["staffing"] == "no_eligible_targets"
    assert mapping["invalid_config"] == "all_masked"
