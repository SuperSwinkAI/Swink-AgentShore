"""merge_pr dirty_trunk wedge → END_SESSION unmask (#330).

A shared trunk checkout can pick up an untracked file that a deterministic
reclaim sweep correctly and deliberately leaves alone (real user WIP, or a
file that predates every known play window). That is correct, safe behavior
— but it means ``merge_pr`` can fail with ``dirty_trunk`` on the exact same
file forever, with no escape. The fix must not pause or force-stop the
session (PPO stays the sole driver of session end): instead,
``CompletionProcessor._handle_merge_pr_outcome`` feeds a same-cause failure
counter on ``MainRepoGuard``, and once it wedges,
``state.trunk_wedged`` unmasks END_SESSION as an option for the PPO
(``rl/eligibility.py``) — nothing is forced.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agentshore.core.main_repo_guard import MainRepoGuard
from agentshore.core.mixins.completion import CompletionProcessor
from agentshore.core.wedge_signals import DirtyTrunkEntry
from agentshore.state import PlayOutcome, PlayType


class _Harness(CompletionProcessor):
    """Minimal CompletionProcessor stand-in for testing _handle_merge_pr_outcome."""

    def __init__(self) -> None:
        self._session_id = "s1"
        self._main_repo = MainRepoGuard()
        self._repo_root = Path("/repo")


def _outcome(*, success: bool, error: str | None = None) -> PlayOutcome:
    return PlayOutcome(
        play_type=PlayType.MERGE_PR,
        agent_id="a1",
        success=success,
        partial=False,
        duration_seconds=0.0,
        token_cost=0,
        dollar_cost=0.0,
        artifacts=[],
        alignment_delta=0.0,
        error=error,
    )


def _patch_dirty_paths(entries: list[DirtyTrunkEntry]):
    return patch(
        "agentshore.core.mixins.completion.collect_dirty_trunk_paths",
        return_value=entries,
    )


@pytest.mark.asyncio
async def test_successful_merge_pr_clears_the_counter() -> None:
    h = _Harness()
    h._main_repo.record_dirty_trunk_failure("scratch.txt")

    await h._handle_merge_pr_outcome(_outcome(success=True))

    assert h._main_repo.is_trunk_wedged() is False
    assert h._main_repo._dirty_trunk_failure_key is None


@pytest.mark.asyncio
async def test_non_dirty_trunk_failure_does_not_increment() -> None:
    h = _Harness()

    with _patch_dirty_paths([DirtyTrunkEntry(path="scratch.txt", status="??")]) as mocked:
        await h._handle_merge_pr_outcome(_outcome(success=False, error="ci_failing"))

    mocked.assert_not_called()
    assert h._main_repo._dirty_trunk_failure_count == 0


@pytest.mark.asyncio
async def test_root_untracked_dirty_trunk_failure_increments_and_wedges_at_threshold() -> None:
    h = _Harness()
    entries = [DirtyTrunkEntry(path="scratch.txt", status="??")]

    with _patch_dirty_paths(entries):
        await h._handle_merge_pr_outcome(_outcome(success=False, error="dirty_trunk: blocked"))
        assert h._main_repo.is_trunk_wedged() is False
        await h._handle_merge_pr_outcome(_outcome(success=False, error="dirty_trunk: blocked"))
        assert h._main_repo.is_trunk_wedged() is False
        await h._handle_merge_pr_outcome(_outcome(success=False, error="dirty_trunk: blocked"))
        assert h._main_repo.is_trunk_wedged() is True


@pytest.mark.asyncio
async def test_a_different_blocking_path_resets_the_streak() -> None:
    h = _Harness()

    with _patch_dirty_paths([DirtyTrunkEntry(path="scratch.txt", status="??")]):
        await h._handle_merge_pr_outcome(_outcome(success=False, error="dirty_trunk"))
        await h._handle_merge_pr_outcome(_outcome(success=False, error="dirty_trunk"))

    with _patch_dirty_paths([DirtyTrunkEntry(path="other.txt", status="??")]):
        await h._handle_merge_pr_outcome(_outcome(success=False, error="dirty_trunk"))

    assert h._main_repo.is_trunk_wedged() is False
    assert h._main_repo._dirty_trunk_failure_count == 1


@pytest.mark.asyncio
async def test_subdirectory_untracked_entry_does_not_increment() -> None:
    """A subtree untracked artifact is a different pathology — not root debris."""
    h = _Harness()

    with _patch_dirty_paths([DirtyTrunkEntry(path="foo/bar", status="??")]):
        await h._handle_merge_pr_outcome(_outcome(success=False, error="dirty_trunk"))

    assert h._main_repo._dirty_trunk_failure_count == 0


@pytest.mark.asyncio
async def test_tracked_modification_only_does_not_increment() -> None:
    """A tracked-file collision (" M") is not the unattributable-untracked case."""
    h = _Harness()

    with _patch_dirty_paths([DirtyTrunkEntry(path="README.md", status=" M")]):
        await h._handle_merge_pr_outcome(_outcome(success=False, error="dirty_trunk"))

    assert h._main_repo._dirty_trunk_failure_count == 0
