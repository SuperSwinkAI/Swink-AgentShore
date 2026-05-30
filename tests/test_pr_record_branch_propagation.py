"""Branch-propagation tests for ``PullRequestRecord`` construction sites (#567).

AgentShore's RL loop has repeatedly hit ``worktree_allocate_failed: missing_branch``
when ``code_review`` is dispatched against a PR whose snapshot has
``branch=None``.  The original fix (commit ad9bce4c) closed the
``github_fallback`` code paths in ``candidates.py``.  This file regresses the
*record* construction layer: every site that builds a ``PullRequestRecord``
must propagate the branch all the way to the on-disk row, the in-memory
record, and the projected snapshot.

The three construction sites audited here are:

1. ``agentshore.github.adapter.GitHubAdapter.list_pull_requests`` — wraps the
   ``gh pr list --json headRefName,...`` response.
2. ``agentshore.data.store.rows._row_to_pull_request`` — DB row -> record
   (exercised end-to-end via ``DataStore.record_pull_request`` +
   ``DataStore.get_pull_request``).
3. ``agentshore.core.mixins.snapshots._project_pull_requests`` — record ->
   snapshot, the path that feeds ``state.pull_requests``.

A fourth case covers the defensive ``pr_snapshot_missing_branch`` log emitted
when projection sees a non-merged record without a branch — the safety net
that surfaces future construction-site leaks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.config import RuntimeConfig
from agentshore.core import Orchestrator
from agentshore.data.models import PullRequestRecord
from agentshore.data.store import DataStore, SessionRecord
from agentshore.github.adapter import GitHubAdapter

if TYPE_CHECKING:
    from pathlib import Path


_BRANCH = "agentshore/567-known-branch"


# ---------------------------------------------------------------------------
# Path 1: gh pr list -> PullRequestRecord
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_adapter_list_pull_requests_propagates_branch() -> None:
    """``GitHubAdapter.list_pull_requests`` must copy ``headRefName`` to record.branch."""
    cfg = RuntimeConfig()
    store = AsyncMock()  # never accessed by list_pull_requests
    adapter = GitHubAdapter(store, session_id="s1", cfg=cfg)
    adapter._available = True  # bypass the probe gate for the unit test
    adapter._gh_json_list = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            {
                "number": 502,
                "title": "PR title",
                "url": "https://github.com/org/repo/pull/502",
                "state": "OPEN",
                "headRefName": _BRANCH,
                "headRefOid": "deadbeef",
                "labels": [],
                "reviewDecision": None,
                "statusCheckRollup": [],
                "isDraft": False,
                "author": {"login": "alice"},
                "createdAt": "2026-05-22T00:00:00Z",
                "mergeable": "MERGEABLE",
                "body": "",
                "closingIssuesReferences": [],
            }
        ]
    )
    records = await adapter.list_pull_requests()
    assert len(records) == 1
    assert records[0].branch == _BRANCH
    assert records[0].pr_number == 502


@pytest.mark.asyncio
async def test_github_adapter_list_pull_requests_branch_none_when_headref_missing() -> None:
    """Defensive None fallback survives if gh JSON omits ``headRefName``.

    The ``GitHubAdapter`` path legitimately can't guarantee a non-null branch
    when the gh CLI returns a malformed/partial PR object (closed-and-deleted
    refs, transient gh errors).  This is the explicit reason
    ``PullRequestRecord.branch`` remains ``str | None`` — see commit body for
    the str-vs-str|None decision rationale.
    """
    cfg = RuntimeConfig()
    store = AsyncMock()
    adapter = GitHubAdapter(store, session_id="s1", cfg=cfg)
    adapter._available = True
    adapter._gh_json_list = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            {
                "number": 999,
                "title": "PR with deleted ref",
                "url": "https://github.com/org/repo/pull/999",
                "state": "CLOSED",
                # headRefName intentionally omitted
                "labels": [],
                "isDraft": False,
                "author": {"login": "alice"},
                "createdAt": "2026-05-22T00:00:00Z",
                "body": "",
                "closingIssuesReferences": [],
            }
        ]
    )
    records = await adapter.list_pull_requests()
    assert len(records) == 1
    assert records[0].branch is None  # fallback honoured


# ---------------------------------------------------------------------------
# Path 2: DB row -> PullRequestRecord (round trip)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_store_round_trip_preserves_branch(tmp_path: Path) -> None:
    """Persist a PR with a known branch, then read it back and assert."""
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await store.create_session(
            SessionRecord(
                session_id="s1",
                project_path=str(tmp_path),
                started_at="2026-05-22T00:00:00+00:00",
            )
        )
        await store.record_pull_request(
            PullRequestRecord(
                pr_number=505,
                session_id="s1",
                state="open",
                created_at="2026-05-22T00:00:01+00:00",
                branch=_BRANCH,
            )
        )
        pr = await store.get_pull_request("s1", 505)
        assert pr is not None
        assert pr.branch == _BRANCH
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_data_store_upsert_preserves_existing_branch_on_null_refresh(
    tmp_path: Path,
) -> None:
    """A later ``record_pull_request`` with branch=None must not blank the row.

    The ``_PULL_REQUEST_UPSERT_SQL`` uses ``COALESCE(excluded.branch, ...)``
    so a refresh that doesn't know the branch (e.g. a partial gh response)
    cannot overwrite a previously-populated value.
    """
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await store.create_session(
            SessionRecord(
                session_id="s1",
                project_path=str(tmp_path),
                started_at="2026-05-22T00:00:00+00:00",
            )
        )
        await store.record_pull_request(
            PullRequestRecord(
                pr_number=506,
                session_id="s1",
                state="open",
                created_at="2026-05-22T00:00:01+00:00",
                branch=_BRANCH,
            )
        )
        # Second write simulates a partial refresh with branch=None.
        await store.record_pull_request(
            PullRequestRecord(
                pr_number=506,
                session_id="s1",
                state="open",
                created_at="2026-05-22T00:00:02+00:00",
                branch=None,
            )
        )
        pr = await store.get_pull_request("s1", 506)
        assert pr is not None
        assert pr.branch == _BRANCH
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Path 3: PullRequestRecord -> PullRequestSnapshot
# ---------------------------------------------------------------------------


def test_project_pull_requests_propagates_branch() -> None:
    """``_project_pull_requests`` must copy ``record.branch`` to ``snapshot.branch``."""
    records = [
        PullRequestRecord(
            pr_number=507,
            session_id="s1",
            state="open",
            created_at="2026-05-22T00:00:00+00:00",
            branch=_BRANCH,
        )
    ]
    snapshots = Orchestrator._project_pull_requests(records)
    assert len(snapshots) == 1
    assert snapshots[0].branch == _BRANCH
    assert snapshots[0].pr_number == 507


def test_project_pull_requests_emits_warning_on_missing_branch() -> None:
    """Safety net: projecting a non-merged record without a branch must warn.

    Surfaces future construction-site leaks before they manifest as the
    cryptic ``worktree_allocate_failed: missing_branch`` downstream.

    Patches the ``agentshore.core._logger`` proxy so the structured event name
    is asserted directly instead of relying on stdlib caplog (structlog
    routes through a separate path).
    """
    records = [
        PullRequestRecord(
            pr_number=508,
            session_id="s1",
            state="open",
            created_at="2026-05-22T00:00:00+00:00",
            branch=None,  # the leak we want to surface
            url="https://github.com/org/repo/pull/508",
            author_agent_id="agent-x",
        )
    ]
    mock_logger = MagicMock()
    with patch("agentshore.core._logger", mock_logger):
        snapshots = Orchestrator._project_pull_requests(records)
    assert len(snapshots) == 1
    assert snapshots[0].branch is None
    # Walk every warning() call and confirm one matches our event name +
    # carries the PR number for triage.
    warning_calls = mock_logger.warning.call_args_list
    matching = [
        call
        for call in warning_calls
        if call.args and call.args[0] == "pr_snapshot_missing_branch"
    ]
    assert len(matching) == 1
    assert matching[0].kwargs.get("pr_number") == 508
    assert matching[0].kwargs.get("state") == "open"


def test_project_pull_requests_no_warning_for_merged_branch_none() -> None:
    """Merged PRs with branch=None are expected (branch is often deleted).

    Avoid noise: the defensive log only fires for ACTIVE PRs where the
    missing branch will break the next code_review/merge_pr dispatch.
    """
    records = [
        PullRequestRecord(
            pr_number=509,
            session_id="s1",
            state="merged",
            created_at="2026-05-22T00:00:00+00:00",
            branch=None,
        )
    ]
    mock_logger = MagicMock()
    with patch("agentshore.core._logger", mock_logger):
        Orchestrator._project_pull_requests(records)
    matching = [
        call
        for call in mock_logger.warning.call_args_list
        if call.args and call.args[0] == "pr_snapshot_missing_branch"
    ]
    assert matching == []
