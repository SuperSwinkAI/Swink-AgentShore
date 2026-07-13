"""Unit tests for the MainRepoGuard collaborator (TNQA 03 C2, 1a.4).

The guard owns the desktop-kqo5 main-repo state lifted off the orchestrator
field bag. These tests pin the load-bearing contracts the orchestrator relies
on — especially the pop-default-``None`` semantics of the per-dispatch pre-play
handshake, which back the never-snapshotted and double-pop code paths.
"""

from __future__ import annotations

from agentshore.core.main_repo_guard import MainRepoGuard


def test_defaults() -> None:
    guard = MainRepoGuard()
    assert guard.default_branch == "main"
    assert guard.dispatch_paused is False
    # No pre-play ref recorded yet: pop returns the None default, not KeyError.
    assert guard.pop_pre_play_branch("never-recorded") is None


def test_custom_default_branch() -> None:
    assert MainRepoGuard(default_branch="develop").default_branch == "develop"


def test_pre_play_handshake_record_then_pop() -> None:
    guard = MainRepoGuard()
    guard.record_pre_play_branch("d-1", "refs/heads/main")
    assert guard.pop_pre_play_branch("d-1") == "refs/heads/main"
    # Popped entries are removed: a second pop returns the None default.
    assert guard.pop_pre_play_branch("d-1") is None


def test_pre_play_handshake_records_none_ref() -> None:
    """A detached/failed pre-play snapshot stores None and pops back as None."""
    guard = MainRepoGuard()
    guard.record_pre_play_branch("d-2", None)
    # The entry exists (distinct from "never recorded") but its value is None.
    assert guard.pop_pre_play_branch("d-2") is None
    assert guard.pop_pre_play_branch("d-2") is None


def test_dispatch_pause_latch() -> None:
    guard = MainRepoGuard()
    guard.dispatch_paused = True
    assert guard.dispatch_paused is True
    guard.dispatch_paused = not True  # mirrors `= not restored` clear path
    assert guard.dispatch_paused is False


# ---------------------------------------------------------------------------
# dirty_trunk wedge counter (#330)
# ---------------------------------------------------------------------------


def test_fresh_guard_is_never_trunk_wedged() -> None:
    guard = MainRepoGuard()
    assert guard.is_trunk_wedged() is False


def test_three_same_key_dirty_trunk_failures_wedge_the_trunk() -> None:
    guard = MainRepoGuard()
    guard.record_dirty_trunk_failure("scratch.txt")
    assert guard.is_trunk_wedged() is False
    guard.record_dirty_trunk_failure("scratch.txt")
    assert guard.is_trunk_wedged() is False
    guard.record_dirty_trunk_failure("scratch.txt")
    assert guard.is_trunk_wedged() is True


def test_a_different_blocking_key_resets_the_streak() -> None:
    guard = MainRepoGuard()
    guard.record_dirty_trunk_failure("scratch.txt")
    guard.record_dirty_trunk_failure("scratch.txt")
    guard.record_dirty_trunk_failure("other.txt")
    # Streak reset to 1 on the new key — not yet wedged.
    assert guard.is_trunk_wedged() is False


def test_clear_dirty_trunk_failures_resets_fully() -> None:
    guard = MainRepoGuard()
    guard.record_dirty_trunk_failure("scratch.txt")
    guard.record_dirty_trunk_failure("scratch.txt")
    guard.record_dirty_trunk_failure("scratch.txt")
    assert guard.is_trunk_wedged() is True

    guard.clear_dirty_trunk_failures()

    assert guard.is_trunk_wedged() is False
    # A fresh failure on the same key starts the streak over from 1.
    guard.record_dirty_trunk_failure("scratch.txt")
    guard.record_dirty_trunk_failure("scratch.txt")
    assert guard.is_trunk_wedged() is False
