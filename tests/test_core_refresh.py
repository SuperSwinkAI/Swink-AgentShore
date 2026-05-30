"""Tests for Orchestrator._refresh_issues — the missing-PR re-fetch path that
detects PRs that have transitioned from OPEN to MERGED/CLOSED between refresh
cycles, and the parallel missing-issue sweep that handles externally closed
issues."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.beads import BeadStatus, GraphTask, ProjectGraph
from agentshore.config import RuntimeConfig, TrustedIdsConfig
from agentshore.core import Orchestrator
from agentshore.data.store import GitHubIssueRecord, PullRequestRecord


def _ts(offset_seconds: float = 0.0) -> str:
    """Return an ISO-8601 timestamp shifted by ``offset_seconds`` from now."""
    return (datetime.now(UTC) + timedelta(seconds=offset_seconds)).isoformat(timespec="seconds")


def _make_orchestrator() -> Orchestrator:
    """Construct an Orchestrator skeleton with only the attrs _refresh_issues
    touches. Full bootstrap requires DB + manager + executor scaffolding;
    we don't need any of that for this isolated test."""
    orch = Orchestrator.__new__(Orchestrator)
    orch._session_id = "s1"
    orch._cfg = RuntimeConfig(trusted_ids=TrustedIdsConfig(github_logins=("trusted",)))
    orch._store = MagicMock()
    orch._store.cache_github_issues = AsyncMock()
    orch._store.cache_pull_requests = AsyncMock()
    orch._store.list_open_pull_requests = AsyncMock(return_value=[])
    orch._store.get_open_issues = AsyncMock(return_value=[])
    orch._store.update_issue_state = AsyncMock()
    # desktop-rla8: incremental-sync cursor; None triggers a full sweep so
    # existing tests exercise the same code path that ran pre-pagination.
    orch._store.get_last_issue_sync_at = AsyncMock(return_value=None)
    orch._store.set_last_issue_sync_at = AsyncMock()
    orch._repo_root = Path(".")
    return orch


def _pr_record(
    pr_number: int, state: str = "open", github_author: str | None = "trusted"
) -> PullRequestRecord:
    return PullRequestRecord(
        pr_number=pr_number,
        session_id="s1",
        state=state,
        created_at=_ts(),
        head_sha=f"sha{pr_number}",
        github_author=github_author,
    )


def _issue_record(issue_number: int, state: str = "open") -> GitHubIssueRecord:
    return GitHubIssueRecord(
        issue_number=issue_number,
        session_id="s1",
        title=f"Issue {issue_number}",
        state=state,
        created_at=_ts(),
    )


def _graph_task(
    issue_number: int,
    *,
    status: BeadStatus = BeadStatus.OPEN,
    title: str = "Task",
    bead_id: str = "bd-1",
) -> GraphTask:
    return GraphTask(
        bead_id=bead_id,
        title=title,
        status=status,
        issue_number=issue_number,
    )


@pytest.mark.asyncio
async def test_refresh_resyncs_pr_dropped_from_open_fetch() -> None:
    """A PR that was OPEN locally but no longer appears in the open-fetch
    must be re-fetched via state="all" and cached with its new state.

    This is the post-merge stale-cache fix: GitHub's list?state=open omits
    merged PRs entirely, and the existing UPSERT can't update what isn't
    in the fetch. Without this, merged PRs sit forever as state=open in
    the cache, and the resolver keeps targeting them for ARF.
    """
    orch = _make_orchestrator()
    # Local cache thinks PR #15 is still open.
    orch._store.list_open_pull_requests = AsyncMock(return_value=[_pr_record(15)])

    gh = MagicMock()
    gh.probe = AsyncMock()
    gh.available = True
    gh.list_issues = AsyncMock(return_value=[])
    # state="open" returns []  (PR #15 was merged on GitHub)
    # state="all" returns the now-merged PR #15
    gh.list_pull_requests = AsyncMock(
        side_effect=[
            [],  # first call: state="open"
            [_pr_record(15, state="MERGED")],  # second call: state="all"
        ]
    )

    with patch("agentshore.github.adapter.GitHubAdapter", return_value=gh):
        await orch._refresh_issues()

    # cache_pull_requests was invoked with the re-fetched MERGED record.
    orch._store.cache_pull_requests.assert_awaited_once()
    cached_args = orch._store.cache_pull_requests.await_args
    assert cached_args is not None  # type-checker guard
    # First positional arg is the session_id; second is the list of records.
    assert cached_args.args[0] == "s1"
    cached_prs = cached_args.args[1]
    assert len(cached_prs) == 1
    assert cached_prs[0].pr_number == 15
    assert cached_prs[0].state == "MERGED"
    assert cached_prs[0].head_sha == "sha15"


@pytest.mark.asyncio
async def test_refresh_no_resync_when_open_fetch_is_complete() -> None:
    """When every locally-cached open PR appears in the open-fetch,
    state="all" must NOT be queried (saves an unnecessary API call)."""
    orch = _make_orchestrator()
    orch._store.list_open_pull_requests = AsyncMock(return_value=[_pr_record(20)])

    gh = MagicMock()
    gh.probe = AsyncMock()
    gh.available = True
    gh.list_issues = AsyncMock(return_value=[])
    gh.list_pull_requests = AsyncMock(return_value=[_pr_record(20)])

    with patch("agentshore.github.adapter.GitHubAdapter", return_value=gh):
        await orch._refresh_issues()

    # Only one list_pull_requests call (state="open"); no state="all" follow-up.
    assert gh.list_pull_requests.await_count == 1
    call = gh.list_pull_requests.await_args
    assert call.kwargs.get("state") == "open"


@pytest.mark.asyncio
async def test_refresh_filters_untrusted_prs_before_cache() -> None:
    orch = _make_orchestrator()

    gh = MagicMock()
    gh.probe = AsyncMock()
    gh.available = True
    gh.list_issues = AsyncMock(return_value=[])
    gh.list_pull_requests = AsyncMock(
        return_value=[
            _pr_record(20, github_author="trusted"),
            _pr_record(21, github_author="stranger"),
        ]
    )

    with patch("agentshore.github.adapter.GitHubAdapter", return_value=gh):
        await orch._refresh_issues()

    orch._store.cache_pull_requests.assert_awaited_once()
    cached_prs = orch._store.cache_pull_requests.await_args.args[1]
    assert [pr.pr_number for pr in cached_prs] == [20]


@pytest.mark.asyncio
async def test_refresh_handles_pr_dropped_and_not_in_state_all() -> None:
    """If a PR is missing from state="open" AND state="all" (e.g., deleted
    or visibility lost), don't crash and don't fabricate a record. Skip it
    silently — the next refresh will retry."""
    orch = _make_orchestrator()
    orch._store.list_open_pull_requests = AsyncMock(return_value=[_pr_record(99)])

    gh = MagicMock()
    gh.probe = AsyncMock()
    gh.available = True
    gh.list_issues = AsyncMock(return_value=[])
    gh.list_pull_requests = AsyncMock(side_effect=[[], []])  # both empty

    with patch("agentshore.github.adapter.GitHubAdapter", return_value=gh):
        await orch._refresh_issues()

    # cache_pull_requests not called (nothing to cache).
    orch._store.cache_pull_requests.assert_not_awaited()


# ---------------------------------------------------------------------------
# Incremental sync (desktop-rla8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_closes_externally_closed_issue_via_state_all() -> None:
    """The single ``state='all'`` fetch surfaces close transitions; the upsert
    in ``cache_github_issues`` flips the local row's state. No second fetch is
    issued — the open→all resync that lived here historically is gone."""
    orch = _make_orchestrator()
    # Local cache says issue #7 is open; GH now reports it closed.
    orch._store.get_open_issues = AsyncMock(return_value=[_issue_record(7, state="open")])

    gh = MagicMock()
    gh.probe = AsyncMock()
    gh.available = True
    gh.list_issues = AsyncMock(return_value=[_issue_record(7, state="closed")])
    gh.list_pull_requests = AsyncMock(return_value=[])

    with patch("agentshore.github.adapter.GitHubAdapter", return_value=gh):
        await orch._refresh_issues()

    # Exactly one list_issues call with state="all"; cache_github_issues
    # receives the closed record and the upsert handles state transition.
    assert gh.list_issues.await_count == 1
    assert gh.list_issues.await_args.kwargs.get("state") == "all"
    orch._store.cache_github_issues.assert_awaited_once()
    cached = orch._store.cache_github_issues.await_args.args[1]
    assert [iss.issue_number for iss in cached] == [7]
    assert cached[0].state == "closed"


@pytest.mark.asyncio
async def test_refresh_incremental_uses_since_when_cursor_set() -> None:
    """With a cursor present and a non-full-sync trigger, ``since=`` is used."""
    from agentshore.state import PlayType

    orch = _make_orchestrator()
    orch._store.get_last_issue_sync_at = AsyncMock(return_value="2026-05-23T22:00:00+00:00")

    gh = MagicMock()
    gh.probe = AsyncMock()
    gh.available = True
    gh.list_issues = AsyncMock(return_value=[])
    gh.list_pull_requests = AsyncMock(return_value=[])

    with patch("agentshore.github.adapter.GitHubAdapter", return_value=gh):
        await orch._refresh_issues(completing_play=PlayType.ISSUE_PICKUP)

    assert gh.list_issues.await_count == 1
    assert gh.list_issues.await_args.kwargs.get("since") == "2026-05-23T22:00:00+00:00"
    # Cursor advanced on success even though no issues changed.
    orch._store.set_last_issue_sync_at.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_full_sync_on_cleanup_ignores_cursor() -> None:
    """``cleanup`` (and ``reconcile_state``) force a full sweep regardless of cursor."""
    from agentshore.state import PlayType

    orch = _make_orchestrator()
    orch._store.get_last_issue_sync_at = AsyncMock(return_value="2026-05-23T22:00:00+00:00")

    gh = MagicMock()
    gh.probe = AsyncMock()
    gh.available = True
    gh.list_issues = AsyncMock(return_value=[])
    gh.list_pull_requests = AsyncMock(return_value=[])

    with patch("agentshore.github.adapter.GitHubAdapter", return_value=gh):
        await orch._refresh_issues(completing_play=PlayType.CLEANUP)

    assert gh.list_issues.await_args.kwargs.get("since") is None


@pytest.mark.asyncio
async def test_refresh_force_full_sync_overrides_cursor() -> None:
    """`force_full_sync=True` forces a paginated sweep even with a valid cursor.

    Used when issue_pickup detects an already-CLOSED issue that the
    incremental ``since=`` query missed (observed 2026-05-28 session 08a948ed,
    30+ refreshes returned changed_count=0 while #966 was closed).
    """
    from agentshore.state import PlayType

    orch = _make_orchestrator()
    orch._store.get_last_issue_sync_at = AsyncMock(return_value="2026-05-23T22:00:00+00:00")

    gh = MagicMock()
    gh.probe = AsyncMock()
    gh.available = True
    gh.list_issues = AsyncMock(return_value=[])
    gh.list_pull_requests = AsyncMock(return_value=[])

    with patch("agentshore.github.adapter.GitHubAdapter", return_value=gh):
        await orch._refresh_issues(
            completing_play=PlayType.ISSUE_PICKUP,
            force_full_sync=True,
        )

    # Despite the cursor being set and the play being ISSUE_PICKUP (normally
    # incremental), the force flag dropped since= to None for a full sweep.
    assert gh.list_issues.await_args.kwargs.get("since") is None


def test_outcome_signals_already_closed_detects_agent_evidence() -> None:
    """`_outcome_signals_already_closed` matches the agent's evidence strings."""
    from agentshore.core.mixins.completion import _outcome_signals_already_closed
    from agentshore.state import PlayOutcome, PlayType

    matching = PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=100,
        dollar_cost=0.1,
        artifacts=[
            {
                "type": "verification_evidence",
                "summary": "Issue #966 is already CLOSED",
            }
        ],
        alignment_delta=0.0,
    )
    assert _outcome_signals_already_closed(matching) is True

    nonmatching = PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=100,
        dollar_cost=0.1,
        artifacts=[{"type": "implementation", "files_changed": 3}],
        alignment_delta=0.0,
    )
    assert _outcome_signals_already_closed(nonmatching) is False


@pytest.mark.asyncio
async def test_refresh_cursor_not_advanced_on_fetch_error() -> None:
    """When ``list_issues`` returns None (hard error), the cursor must NOT advance —
    next refresh retries from the same ``since=``."""
    orch = _make_orchestrator()
    orch._store.get_last_issue_sync_at = AsyncMock(return_value="2026-05-23T22:00:00+00:00")

    gh = MagicMock()
    gh.probe = AsyncMock()
    gh.available = True
    gh.list_issues = AsyncMock(return_value=None)
    gh.list_pull_requests = AsyncMock(return_value=[])

    with patch("agentshore.github.adapter.GitHubAdapter", return_value=gh):
        await orch._refresh_issues()

    orch._store.set_last_issue_sync_at.assert_not_awaited()
    orch._store.cache_github_issues.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_closes_open_issue_when_only_duplicate_closed_beads_exist() -> None:
    """Close GH issue when every linked bead is CLOSED and marked as duplicate."""
    orch = _make_orchestrator()

    gh = MagicMock()
    gh.probe = AsyncMock()
    gh.available = True
    gh.list_issues = AsyncMock(return_value=[_issue_record(345, state="open")])
    gh.list_pull_requests = AsyncMock(return_value=[])
    gh.close_issue = AsyncMock(return_value=True)

    graph = ProjectGraph(
        tasks=[
            _graph_task(
                345,
                status=BeadStatus.CLOSED,
                title="Duplicate bead of desktop-foo",
                bead_id="bd-dup",
            )
        ]
    )

    with (
        patch("agentshore.github.adapter.GitHubAdapter", return_value=gh),
        patch("agentshore.beads.load_graph", new=AsyncMock(return_value=graph)),
    ):
        await orch._refresh_issues()

    gh.close_issue.assert_awaited_once()
    orch._store.update_issue_state.assert_any_await(345, "s1", "closed")


@pytest.mark.asyncio
async def test_refresh_does_not_close_issue_when_live_bead_exists() -> None:
    """Do not close GH issue when any linked bead is still actionable."""
    orch = _make_orchestrator()

    gh = MagicMock()
    gh.probe = AsyncMock()
    gh.available = True
    gh.list_issues = AsyncMock(return_value=[_issue_record(345, state="open")])
    gh.list_pull_requests = AsyncMock(return_value=[])
    gh.close_issue = AsyncMock(return_value=True)

    graph = ProjectGraph(
        tasks=[
            _graph_task(
                345,
                status=BeadStatus.CLOSED,
                title="Duplicate bead of desktop-foo",
                bead_id="bd-dup",
            ),
            _graph_task(
                345,
                status=BeadStatus.OPEN,
                title="Real live task",
                bead_id="bd-live",
            ),
        ]
    )

    with (
        patch("agentshore.github.adapter.GitHubAdapter", return_value=gh),
        patch("agentshore.beads.load_graph", new=AsyncMock(return_value=graph)),
    ):
        await orch._refresh_issues()

    gh.close_issue.assert_not_awaited()


# ---------------------------------------------------------------------------
# Reopen clears closed_at (M3) — store-level test via in-memory DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_github_issues_reopen_clears_closed_at() -> None:
    """cache_github_issues must set closed_at=NULL when the incoming state is
    'open', even if the row previously had a closed_at timestamp."""
    import aiosqlite

    from agentshore.data.store import DataStore

    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        store = DataStore.__new__(DataStore)
        store._db = conn  # bypass initialize(); _conn property reads _db
        # Bootstrap schema — only the tables we need.
        await conn.executescript(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                project_path TEXT NOT NULL,
                started_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                total_cost REAL NOT NULL DEFAULT 0.0,
                total_plays INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO sessions(session_id, project_path, started_at)
            VALUES ('s1', '.', '2024-01-01T00:00:00');

            CREATE TABLE github_issues (
                issue_number INTEGER NOT NULL,
                session_id   TEXT NOT NULL,
                title        TEXT NOT NULL,
                state        TEXT NOT NULL,
                priority     INTEGER,
                labels       TEXT,
                source       TEXT,
                url          TEXT,
                created_at   TEXT NOT NULL,
                closed_at    TEXT,
                PRIMARY KEY (issue_number, session_id)
            );
            """
        )

        # Insert a closed issue with a closed_at value.
        closed_issue = GitHubIssueRecord(
            issue_number=5,
            session_id="s1",
            title="Bug",
            state="closed",
            created_at=_ts(-3600),
            closed_at=_ts(-60),
        )
        await store.cache_github_issues("s1", [closed_issue])

        # Re-open the issue on GitHub (state='open', closed_at=None from adapter).
        reopened = GitHubIssueRecord(
            issue_number=5,
            session_id="s1",
            title="Bug",
            state="open",
            created_at=_ts(-3600),
            closed_at=None,
        )
        await store.cache_github_issues("s1", [reopened])

        # Verify closed_at is now NULL.
        cursor = await conn.execute(
            "SELECT state, closed_at FROM github_issues WHERE issue_number=5 AND session_id='s1'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "open"
        assert row[1] is None, "closed_at should be NULL after a reopen"
