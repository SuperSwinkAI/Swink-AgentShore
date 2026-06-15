"""Piece A: per-resource worktree-allocation failure park (issue #60).

``Dispatcher.register_worktree_allocation_failure`` tallies allocation failures
per resource key and parks a key once it crosses ``_WORKTREE_PARK_THRESHOLD``,
so a structurally-unallocatable PR stops being re-selected every tick (the
``unblock_pr`` hot-loop). The method only touches ``self._host`` state, so we
exercise it on a bare ``Dispatcher`` instance with a minimal fake host.
"""

from __future__ import annotations

from agentshore.core.mixins.dispatch import _WORKTREE_PARK_THRESHOLD, Dispatcher
from agentshore.core.session_runtime import SessionRuntime
from agentshore.plays.base import PlayParams


def _dispatcher() -> Dispatcher:
    disp = Dispatcher.__new__(Dispatcher)
    disp._runtime = SessionRuntime()
    disp._session_id = "s1"
    return disp


def test_parks_resource_after_threshold_failures() -> None:
    disp = _dispatcher()
    params = PlayParams(pr_number=103, extras={"resource_keys": ["pr:103"]})

    # Below threshold: still transient (retrying), not parked yet.
    for _ in range(_WORKTREE_PARK_THRESHOLD - 1):
        assert disp.register_worktree_allocation_failure(params) is False
    assert "pr:103" not in disp._runtime.parked_resource_keys

    # Crossing the threshold parks the key (structurally stuck).
    assert disp.register_worktree_allocation_failure(params) is True
    assert "pr:103" in disp._runtime.parked_resource_keys
    # Already-parked: still reports parked, no double-counting churn.
    assert disp.register_worktree_allocation_failure(params) is True


def test_distinct_resources_are_counted_independently() -> None:
    disp = _dispatcher()
    p103 = PlayParams(pr_number=103, extras={"resource_keys": ["pr:103"]})
    p104 = PlayParams(pr_number=104, extras={"resource_keys": ["pr:104"]})

    for _ in range(_WORKTREE_PARK_THRESHOLD):
        disp.register_worktree_allocation_failure(p103)
    # #104 has only failed once — not parked, #103 is.
    disp.register_worktree_allocation_failure(p104)
    assert disp._runtime.parked_resource_keys == {"pr:103"}


def test_no_resource_keys_never_parks() -> None:
    disp = _dispatcher()
    params = PlayParams(pr_number=None, extras={})
    for _ in range(_WORKTREE_PARK_THRESHOLD + 2):
        assert disp.register_worktree_allocation_failure(params) is False
    assert disp._runtime.parked_resource_keys == set()
