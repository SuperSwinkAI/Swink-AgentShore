"""Tests for MergePRPlay's post-merge issue-close write-through.

After a successful merge the play parses ``issues_closed`` from the skill
result and calls ``store.update_issue_state(n, session_id, "closed")`` for
all issues in one batch write-through. Without this the SQLite cache stays at
state='open' for the just-closed
issues and the dashboard's DONE column (driven by list_recently_closed_issues,
which filters on state='closed' AND closed_at within 24h) never populates —
because the periodic GitHub refresh only fetches state='open' and therefore
can't update closed rows.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.plays.base import PlayParams
from agentshore.plays.skill_backed.merge_pr import MergePRPlay, _fetch_pr_links
from agentshore.state import AgentStatus, PlayOutcome, PlayType, SkillResult


def _ctx() -> Any:
    ctx = MagicMock()
    ctx.session_id = "test-session"
    ctx.play_id = 1
    ctx.store = AsyncMock()
    ctx.store.mark_pr_merged = AsyncMock()
    ctx.store.complete_reviews_for_pr = AsyncMock()
    ctx.store.update_issue_state = AsyncMock()
    ctx.store.update_issues_state_batch = AsyncMock()
    ctx.cfg = MagicMock()
    # Real empty identities so the post-merge ff-sync's fetch-overlay resolver
    # (resolve_ff_fetch_overlay → select_default_git_identity) returns None
    # cleanly instead of choking on a MagicMock's empty-iterable .items() (#178).
    ctx.cfg.identities = {}
    ctx.manager = MagicMock()
    ctx.project_path = MagicMock()
    return ctx


def _state() -> Any:
    state = MagicMock()
    agent = MagicMock()
    agent.agent_id = "agent-1"
    agent.agent_type = "claude_code"
    agent.status = AgentStatus.IDLE
    state.agents = [agent]
    return state


def _params(pr_number: int = 42) -> PlayParams:
    return PlayParams(agent_id="agent-1", pr_number=pr_number)


def _success_outcome(pr_number: int = 42) -> PlayOutcome:
    return PlayOutcome(
        play_type=PlayType.MERGE_PR,
        agent_id="agent-1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=100,
        dollar_cost=0.05,
        artifacts=[{"type": "merge", "pr": pr_number, "merge_method": "squash"}],
        alignment_delta=0.1,
    )


@pytest.mark.asyncio
async def test_merge_marks_referenced_issues_closed() -> None:
    """A successful merge with two referenced issues writes one batched close."""
    play = MergePRPlay()
    ctx = _ctx()

    async def _super_execute(*args: object, **kwargs: object) -> PlayOutcome:
        # Reproduce what SkillBackedPlay.execute does: populate
        # _last_skill_result on the play instance, then return an outcome.
        play._last_skill_result = SkillResult(success=True, issues_closed=[17, 23])
        return _success_outcome()

    with patch(
        "agentshore.plays.skill_backed.base.SkillBackedPlay.execute",
        new=_super_execute,
    ):
        outcome = await play.execute(_state(), _params(), ctx=ctx)

    assert outcome.success is True
    ctx.store.mark_pr_merged.assert_awaited_once_with(42, "test-session")
    ctx.store.update_issues_state_batch.assert_awaited_once_with([17, 23], "test-session", "closed")


@pytest.mark.asyncio
async def test_merge_with_no_closed_issues_skips_update() -> None:
    """A successful merge that closed no issues (e.g. doc-only PR) makes
    no update call."""
    play = MergePRPlay()
    ctx = _ctx()

    async def _super_execute(*args: object, **kwargs: object) -> PlayOutcome:
        play._last_skill_result = SkillResult(success=True, issues_closed=[])
        return _success_outcome()

    with patch(
        "agentshore.plays.skill_backed.base.SkillBackedPlay.execute",
        new=_super_execute,
    ):
        await play.execute(_state(), _params(), ctx=ctx)

    ctx.store.update_issues_state_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_merge_failure_does_not_update_issue_state() -> None:
    """If the merge skill fails, no issue-state writes happen — the issues
    weren't actually closed."""
    play = MergePRPlay()
    ctx = _ctx()

    failed_outcome = PlayOutcome(
        play_type=PlayType.MERGE_PR,
        agent_id="agent-1",
        success=False,
        partial=False,
        duration_seconds=1.0,
        token_cost=100,
        dollar_cost=0.05,
        artifacts=[],
        alignment_delta=0.0,
        error="ci_failure",
    )

    async def _super_execute(*args: object, **kwargs: object) -> PlayOutcome:
        play._last_skill_result = SkillResult(success=False, issues_closed=[17])
        return failed_outcome

    with patch(
        "agentshore.plays.skill_backed.base.SkillBackedPlay.execute",
        new=_super_execute,
    ):
        await play.execute(_state(), _params(), ctx=ctx)

    ctx.store.mark_pr_merged.assert_not_awaited()
    ctx.store.update_issues_state_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_merge_continues_when_batch_update_fails() -> None:
    """If the batched update raises, the merge outcome is still preserved."""
    play = MergePRPlay()
    ctx = _ctx()

    ctx.store.update_issues_state_batch = AsyncMock(side_effect=RuntimeError("boom"))

    async def _super_execute(*args: object, **kwargs: object) -> PlayOutcome:
        play._last_skill_result = SkillResult(success=True, issues_closed=[17, 23])
        return _success_outcome()

    with patch(
        "agentshore.plays.skill_backed.base.SkillBackedPlay.execute",
        new=_super_execute,
    ):
        outcome = await play.execute(_state(), _params(), ctx=ctx)

    # The merge outcome itself stays success=True; the issue-cache write is best-effort.
    assert outcome.success is True
    ctx.store.update_issues_state_batch.assert_awaited_once_with([17, 23], "test-session", "closed")


@pytest.mark.asyncio
async def test_merge_records_pr_merged_issue_numbers_artifact() -> None:
    """A successful merge records a ``pr_merged_issue_numbers`` artifact with
    the validated issue list so downstream metrics can distinguish linked from
    unlinked merges."""
    play = MergePRPlay()
    ctx = _ctx()

    async def _super_execute(*args: object, **kwargs: object) -> PlayOutcome:
        play._last_skill_result = SkillResult(success=True, issues_closed=[17, 23])
        return _success_outcome()

    with (
        patch(
            "agentshore.plays.skill_backed.base.SkillBackedPlay.execute",
            new=_super_execute,
        ),
        patch(
            "agentshore.plays.skill_backed.merge_pr._fetch_pr_body",
            new=AsyncMock(return_value="Closes #17 and #23"),
        ),
    ):
        outcome = await play.execute(_state(), _params(), ctx=ctx)

    closure_artifacts = [
        a
        for a in outcome.artifacts
        if isinstance(a, dict) and a.get("type") == "pr_merged_issue_numbers"
    ]
    assert len(closure_artifacts) == 1
    assert closure_artifacts[0]["pr"] == 42
    assert closure_artifacts[0]["issue_numbers"] == [17, 23]
    # Pre-existing artifacts from the underlying outcome are preserved.
    assert any(isinstance(a, dict) and a.get("type") == "merge" for a in outcome.artifacts)


@pytest.mark.asyncio
async def test_merge_records_empty_pr_merged_issue_numbers_artifact_for_unlinked_pr() -> None:
    """A successful merge for an unlinked/doc-only PR still records the
    artifact, with an empty ``issue_numbers`` list, so metrics can refuse to
    count it as issue-throughput."""
    play = MergePRPlay()
    ctx = _ctx()

    async def _super_execute(*args: object, **kwargs: object) -> PlayOutcome:
        play._last_skill_result = SkillResult(success=True, issues_closed=[])
        return _success_outcome()

    with (
        patch(
            "agentshore.plays.skill_backed.base.SkillBackedPlay.execute",
            new=_super_execute,
        ),
        patch(
            "agentshore.plays.skill_backed.merge_pr._fetch_pr_body",
            new=AsyncMock(return_value="Docs-only PR; no issue reference."),
        ),
    ):
        outcome = await play.execute(_state(), _params(), ctx=ctx)

    closure_artifacts = [
        a
        for a in outcome.artifacts
        if isinstance(a, dict) and a.get("type") == "pr_merged_issue_numbers"
    ]
    assert len(closure_artifacts) == 1
    assert closure_artifacts[0]["pr"] == 42
    assert closure_artifacts[0]["issue_numbers"] == []


@pytest.mark.asyncio
async def test_merge_failure_does_not_record_closure_artifact() -> None:
    """A failed merge must not append a ``pr_merged_issue_numbers`` artifact;
    the issues were not actually closed."""
    play = MergePRPlay()
    ctx = _ctx()

    failed_outcome = PlayOutcome(
        play_type=PlayType.MERGE_PR,
        agent_id="agent-1",
        success=False,
        partial=False,
        duration_seconds=1.0,
        token_cost=100,
        dollar_cost=0.05,
        artifacts=[],
        alignment_delta=0.0,
        error="ci_failure",
    )

    async def _super_execute(*args: object, **kwargs: object) -> PlayOutcome:
        play._last_skill_result = SkillResult(success=False, issues_closed=[])
        return failed_outcome

    with patch(
        "agentshore.plays.skill_backed.base.SkillBackedPlay.execute",
        new=_super_execute,
    ):
        outcome = await play.execute(_state(), _params(), ctx=ctx)

    assert outcome.success is False
    assert not any(
        isinstance(a, dict) and a.get("type") == "pr_merged_issue_numbers"
        for a in outcome.artifacts
    )


# desktop-8otp: infer_pr_issue_links integration + issues_closed artifact key


@pytest.mark.asyncio
async def test_merge_uses_pr_links_when_fetch_succeeds() -> None:
    """When _fetch_pr_links returns numbers, they are unioned with skill result.

    This covers the non-default-branch case (desktop-8otp) where GitHub does
    not auto-close issues: the branch name ``agentshore/17-...`` is sufficient to
    infer the linked issue even if the PR body has no closing keyword.
    """
    play = MergePRPlay()
    ctx = _ctx()

    async def _super_execute(*args: object, **kwargs: object) -> PlayOutcome:
        # Skill reports nothing closed (PR body lacked closing keyword).
        play._last_skill_result = SkillResult(success=True, issues_closed=[])
        return _success_outcome()

    with (
        patch(
            "agentshore.plays.skill_backed.base.SkillBackedPlay.execute",
            new=_super_execute,
        ),
        patch(
            "agentshore.plays.skill_backed.merge_pr._fetch_pr_links",
            new=AsyncMock(return_value=(17,)),
        ),
    ):
        outcome = await play.execute(_state(), _params(), ctx=ctx)

    assert outcome.success is True
    # Issue 17 discovered via branch/link inference must appear in the DB write.
    ctx.store.update_issues_state_batch.assert_awaited_once_with([17], "test-session", "closed")


@pytest.mark.asyncio
async def test_merge_unions_skill_and_pr_links() -> None:
    """Skill-reported issues and link-inferred issues are unioned, not replaced.

    If the skill reports issue 23 (from a closing keyword) and _fetch_pr_links
    additionally finds issue 17 (from the AgentShore branch prefix), both appear
    in the final issues_closed list.
    """
    play = MergePRPlay()
    ctx = _ctx()

    async def _super_execute(*args: object, **kwargs: object) -> PlayOutcome:
        play._last_skill_result = SkillResult(success=True, issues_closed=[23])
        return _success_outcome()

    with (
        patch(
            "agentshore.plays.skill_backed.base.SkillBackedPlay.execute",
            new=_super_execute,
        ),
        patch(
            "agentshore.plays.skill_backed.merge_pr._fetch_pr_links",
            new=AsyncMock(return_value=(17, 23)),
        ),
    ):
        outcome = await play.execute(_state(), _params(), ctx=ctx)

    assert outcome.success is True
    ctx.store.update_issues_state_batch.assert_awaited_once_with([17, 23], "test-session", "closed")


@pytest.mark.asyncio
async def test_artifact_contains_issues_closed_key() -> None:
    """The pr_merged_issue_numbers artifact must include an ``issues_closed`` key.

    Downstream consumers (run_qa, groom_backlog, dashboard DONE column) use
    this field to reflect closed issues without waiting for a GitHub refresh.
    """
    play = MergePRPlay()
    ctx = _ctx()

    async def _super_execute(*args: object, **kwargs: object) -> PlayOutcome:
        play._last_skill_result = SkillResult(success=True, issues_closed=[17, 23])
        return _success_outcome()

    with (
        patch(
            "agentshore.plays.skill_backed.base.SkillBackedPlay.execute",
            new=_super_execute,
        ),
        patch(
            "agentshore.plays.skill_backed.merge_pr._fetch_pr_links",
            new=AsyncMock(return_value=(17, 23)),
        ),
    ):
        outcome = await play.execute(_state(), _params(), ctx=ctx)

    closure_artifact = next(
        (
            a
            for a in outcome.artifacts
            if isinstance(a, dict) and a.get("type") == "pr_merged_issue_numbers"
        ),
        None,
    )
    assert closure_artifact is not None, "pr_merged_issue_numbers artifact must be present"
    assert "issues_closed" in closure_artifact, "artifact must have issues_closed key"
    assert closure_artifact["issues_closed"] == [17, 23]


@pytest.mark.asyncio
async def test_artifact_issues_closed_key_empty_for_unlinked_pr() -> None:
    """An unlinked PR has ``issues_closed: []`` in its artifact."""
    play = MergePRPlay()
    ctx = _ctx()

    async def _super_execute(*args: object, **kwargs: object) -> PlayOutcome:
        play._last_skill_result = SkillResult(success=True, issues_closed=[])
        return _success_outcome()

    with (
        patch(
            "agentshore.plays.skill_backed.base.SkillBackedPlay.execute",
            new=_super_execute,
        ),
        patch(
            "agentshore.plays.skill_backed.merge_pr._fetch_pr_links",
            new=AsyncMock(return_value=()),
        ),
        patch(
            "agentshore.plays.skill_backed.merge_pr._fetch_pr_body",
            new=AsyncMock(return_value="No issue references."),
        ),
    ):
        outcome = await play.execute(_state(), _params(), ctx=ctx)

    closure_artifact = next(
        (
            a
            for a in outcome.artifacts
            if isinstance(a, dict) and a.get("type") == "pr_merged_issue_numbers"
        ),
        None,
    )
    assert closure_artifact is not None
    assert closure_artifact["issues_closed"] == []


@pytest.mark.asyncio
async def test_merge_falls_back_to_body_when_links_fetch_fails() -> None:
    """When _fetch_pr_links returns empty, body-keyword validation is used."""
    play = MergePRPlay()
    ctx = _ctx()

    async def _super_execute(*args: object, **kwargs: object) -> PlayOutcome:
        play._last_skill_result = SkillResult(success=True, issues_closed=[17])
        return _success_outcome()

    with (
        patch(
            "agentshore.plays.skill_backed.base.SkillBackedPlay.execute",
            new=_super_execute,
        ),
        patch(
            "agentshore.plays.skill_backed.merge_pr._fetch_pr_links",
            new=AsyncMock(return_value=()),
        ),
        patch(
            "agentshore.plays.skill_backed.merge_pr._fetch_pr_body",
            new=AsyncMock(return_value="Closes #17"),
        ),
    ):
        await play.execute(_state(), _params(), ctx=ctx)

    ctx.store.update_issues_state_batch.assert_awaited_once_with([17], "test-session", "closed")


# _fetch_pr_links unit tests (desktop-8otp: infer_pr_issue_links integration)


@pytest.mark.asyncio
async def test_fetch_pr_links_parses_body_and_branch() -> None:
    """_fetch_pr_links calls infer_pr_issue_links with body, headRefName, and
    closingIssuesReferences from the gh JSON response."""
    import json as _json
    from pathlib import Path
    from unittest.mock import MagicMock

    from agentshore.command import CommandResult

    payload = _json.dumps(
        {
            "body": "Closes #17",
            "headRefName": "agentshore/23-some-feature",
            "closingIssuesReferences": [],
        }
    )

    mock_result = MagicMock(spec=CommandResult)
    mock_result.returncode = 0
    mock_result.stdout = payload

    with patch(
        "agentshore.plays.skill_backed.merge_pr.run_command",
        new=AsyncMock(return_value=mock_result),
    ):
        numbers = await _fetch_pr_links(42, Path("/fake/project"))

    # Issue 17 from body keyword; issue 23 from agentshore branch prefix.
    assert 17 in numbers
    assert 23 in numbers


@pytest.mark.asyncio
async def test_fetch_pr_links_returns_empty_on_command_failure() -> None:
    """_fetch_pr_links returns () when gh is unavailable."""
    from pathlib import Path

    with patch(
        "agentshore.plays.skill_backed.merge_pr.run_command",
        new=AsyncMock(side_effect=OSError("gh not found")),
    ):
        numbers = await _fetch_pr_links(42, Path("/fake/project"))

    assert numbers == ()


@pytest.mark.asyncio
async def test_fetch_pr_links_returns_empty_on_nonzero_exit() -> None:
    """_fetch_pr_links returns () when gh exits non-zero."""
    from pathlib import Path
    from unittest.mock import MagicMock

    from agentshore.command import CommandResult

    mock_result = MagicMock(spec=CommandResult)
    mock_result.returncode = 1
    mock_result.stderr = "not found"

    with patch(
        "agentshore.plays.skill_backed.merge_pr.run_command",
        new=AsyncMock(return_value=mock_result),
    ):
        numbers = await _fetch_pr_links(42, Path("/fake/project"))

    assert numbers == ()


@pytest.mark.asyncio
async def test_fetch_pr_links_returns_empty_on_invalid_json() -> None:
    """_fetch_pr_links returns () when gh returns non-JSON output."""
    from pathlib import Path
    from unittest.mock import MagicMock

    from agentshore.command import CommandResult

    mock_result = MagicMock(spec=CommandResult)
    mock_result.returncode = 0
    mock_result.stdout = "not json"

    with patch(
        "agentshore.plays.skill_backed.merge_pr.run_command",
        new=AsyncMock(return_value=mock_result),
    ):
        numbers = await _fetch_pr_links(42, Path("/fake/project"))

    assert numbers == ()


@pytest.mark.asyncio
async def test_fetch_pr_links_uses_github_closing_references() -> None:
    """closingIssuesReferences from the GitHub API are included even when
    the PR body has no closing keyword (non-default-branch merge case)."""
    import json as _json
    from pathlib import Path
    from unittest.mock import MagicMock

    from agentshore.command import CommandResult

    payload = _json.dumps(
        {
            "body": "Fix for the overflow bug.",
            "headRefName": "feature/overflow-fix",
            "closingIssuesReferences": [{"number": 99}],
        }
    )

    mock_result = MagicMock(spec=CommandResult)
    mock_result.returncode = 0
    mock_result.stdout = payload

    with patch(
        "agentshore.plays.skill_backed.merge_pr.run_command",
        new=AsyncMock(return_value=mock_result),
    ):
        numbers = await _fetch_pr_links(42, Path("/fake/project"))

    assert 99 in numbers
