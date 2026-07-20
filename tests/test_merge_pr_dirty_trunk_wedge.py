"""merge_pr dirty_trunk wedge → END_SESSION unmask + escalation (#330).

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

On top of that non-forcing unmask, once the guard wedges,
``_handle_merge_pr_outcome`` also calls ``_escalate_trunk_wedge``: it
force-quarantines the exact same-cause root path(s) (bypassing the
conservative mtime-window attribution ``reconcile_state``'s sweep requires,
since three same-cause failures on the identical file is strong evidence of
abandoned debris, not active WIP) and always emits a ``trunk_wedge_needs_human``
warning so the operator gets a clear signal naming the offending path(s).
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
    """Minimal CompletionProcessor stand-in for testing _handle_merge_pr_outcome.

    No ``_store`` attribute — mirrors the sparse harnesses elsewhere in this
    suite (e.g. ``test_write_plan_unplannable_backoff.py``) and doubles as
    coverage that the escalation path degrades gracefully (best-effort
    external-mutation recording) when a caller has no store wired up.
    """

    def __init__(self, *, repo_root: Path | None = None) -> None:
        self._session_id = "s1"
        self._main_repo = MainRepoGuard()
        self._repo_root = repo_root if repo_root is not None else Path("/repo")


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


# --- escalation on wedge: force-quarantine + needs-human (#330) --------------


@pytest.mark.asyncio
async def test_wedge_force_quarantines_the_offending_file(tmp_path: Path) -> None:
    """Once the 3rd same-cause failure wedges the trunk, the exact offending
    root file is moved into .agentshore/reclaimed/wedge/ — bypassing the
    normal mtime-window attribution requirement — so git status goes clean
    and the next merge_pr attempt can succeed unaided.
    """
    (tmp_path / "scratch_plan.md").write_text("agent scratch notes")
    h = _Harness(repo_root=tmp_path)
    entries = [DirtyTrunkEntry(path="scratch_plan.md", status="??")]

    with _patch_dirty_paths(entries):
        await h._handle_merge_pr_outcome(_outcome(success=False, error="dirty_trunk: blocked"))
        await h._handle_merge_pr_outcome(_outcome(success=False, error="dirty_trunk: blocked"))
        assert (tmp_path / "scratch_plan.md").exists()  # not yet wedged
        await h._handle_merge_pr_outcome(_outcome(success=False, error="dirty_trunk: blocked"))

    assert h._main_repo.is_trunk_wedged() is True
    assert not (tmp_path / "scratch_plan.md").exists()
    quarantined = tmp_path / ".agentshore" / "reclaimed" / "wedge" / "scratch_plan.md"
    assert quarantined.read_text() == "agent scratch notes"


@pytest.mark.asyncio
async def test_wedge_escalation_emits_needs_human_signal(tmp_path: Path) -> None:
    """The needs-human log signal fires on wedge, naming the blocking path(s),
    independent of the (also-existing) END_SESSION unmask — the operator gets
    a proactive signal rather than only discovering the wedge if/when the PPO
    chooses to end the session.
    """
    (tmp_path / "scratch_plan.md").write_text("x")
    h = _Harness(repo_root=tmp_path)
    entries = [DirtyTrunkEntry(path="scratch_plan.md", status="??")]

    with (
        _patch_dirty_paths(entries),
        patch("agentshore.core.mixins.completion._logger") as mock_logger,
    ):
        for _ in range(3):
            await h._handle_merge_pr_outcome(_outcome(success=False, error="dirty_trunk"))

    events = [call.args[0] for call in mock_logger.warning.call_args_list]
    assert "trunk_wedge_needs_human" in events
    needs_human_call = next(
        call
        for call in mock_logger.warning.call_args_list
        if call.args[0] == "trunk_wedge_needs_human"
    )
    assert needs_human_call.kwargs["blocking_paths"] == ["scratch_plan.md"]
    assert needs_human_call.kwargs["quarantined_paths"] == ["scratch_plan.md"]
    assert needs_human_call.kwargs["unresolved_paths"] == []


@pytest.mark.asyncio
async def test_wedge_escalation_still_reports_needs_human_when_quarantine_fails() -> None:
    """Quarantine is best-effort: a non-existent repo root means the file
    move fails, but the needs-human signal must still fire naming the path
    as unresolved — the operator must never be left silent just because the
    filesystem action didn't pan out.
    """
    h = _Harness(repo_root=Path("/definitely/does/not/exist"))
    entries = [DirtyTrunkEntry(path="scratch_plan.md", status="??")]

    with (
        _patch_dirty_paths(entries),
        patch("agentshore.core.mixins.completion._logger") as mock_logger,
    ):
        for _ in range(3):
            await h._handle_merge_pr_outcome(_outcome(success=False, error="dirty_trunk"))

    assert h._main_repo.is_trunk_wedged() is True
    needs_human_call = next(
        call
        for call in mock_logger.warning.call_args_list
        if call.args[0] == "trunk_wedge_needs_human"
    )
    assert needs_human_call.kwargs["blocking_paths"] == ["scratch_plan.md"]
    assert needs_human_call.kwargs["quarantined_paths"] == []
    assert needs_human_call.kwargs["unresolved_paths"] == ["scratch_plan.md"]


@pytest.mark.asyncio
async def test_no_escalation_before_wedge_threshold(tmp_path: Path) -> None:
    """The first two same-cause failures must not quarantine anything — only
    the guard's own wedge threshold (3) triggers escalation.
    """
    (tmp_path / "scratch_plan.md").write_text("x")
    h = _Harness(repo_root=tmp_path)
    entries = [DirtyTrunkEntry(path="scratch_plan.md", status="??")]

    with _patch_dirty_paths(entries):
        await h._handle_merge_pr_outcome(_outcome(success=False, error="dirty_trunk"))
        await h._handle_merge_pr_outcome(_outcome(success=False, error="dirty_trunk"))

    assert h._main_repo.is_trunk_wedged() is False
    assert (tmp_path / "scratch_plan.md").exists()
