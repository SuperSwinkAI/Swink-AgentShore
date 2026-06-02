"""Shared GitHub fetch/cache primitive for bootstrap and post-play refresh.

Both the bootstrap phase (``phases._phase_fetch_github``) and the post-play
refresh (``mixins.completion.CompletionProcessor.refresh_issues``) used to inline
the same GitHub-cache write path: construct a ``GitHubAdapter``, probe, fetch
issues, ``cache_github_issues`` + ``set_last_issue_sync_at``, fetch open PRs,
``filter_trusted_pull_requests``, ``cache_pull_requests``. Two copies of that
plumbing drifted apart over time.

:class:`GitHubSyncer` owns the adapter + store + cfg + session_id and exposes
the shared fetch/cache operations as cohesive methods. The two call sites keep
their own distinguishing concerns (startup-only beads mirror + branch-activity
rebuild; refresh-only duplicate-bead sweep + missing-PR resync + worktree
reaping) but delegate the duplicated fetch/cache path here.

The syncer holds no mutable session state of its own — it is a thin collaborator
constructed where the adapter is already in hand. Log events are emitted by the
syncer for the shared operations so both call sites observe identical wire shapes
for the cached/refreshed primitives; call-site-specific events stay at the call
sites.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from agentshore.github.trust import filter_trusted_pull_requests, trusted_pr_author_logins

if TYPE_CHECKING:
    from agentshore.config import RuntimeConfig
    from agentshore.data.store import DataStore, GitHubIssueRecord, PullRequestRecord
    from agentshore.github.adapter import GitHubAdapter


# Lookback applied to the sync cursor so an issue updated mid-fetch is still
# caught on the next call. Cheaper than racing against gh's wall clock. Shared
# by the startup cutoff and the incremental refresh cutoff.
SYNC_CURSOR_LOOKBACK_SECONDS = 60


def sync_cursor_now() -> str:
    """ISO cutoff stamped *before* a fetch, with lookback for clock skew."""
    return (datetime.now(UTC) - timedelta(seconds=SYNC_CURSOR_LOOKBACK_SECONDS)).isoformat()


class GitHubSyncer:
    """Collaborator owning the shared GitHub fetch/cache path."""

    def __init__(
        self,
        *,
        gh: GitHubAdapter,
        store: DataStore,
        cfg: RuntimeConfig,
        session_id: str,
    ) -> None:
        self._gh = gh
        self._store = store
        self._cfg = cfg
        self._session_id = session_id

    @property
    def available(self) -> bool:
        return self._gh.available

    async def fetch_issues(
        self,
        *,
        state: str,
        since: str | None,
    ) -> list[GitHubIssueRecord] | None:
        """Fetch issues at ``state`` (optionally incremental ``since``).

        Returns the records, ``None`` on fetch failure, or ``[]`` for an empty
        result. Does not cache — caching + cursor advance live in
        :meth:`cache_issues` so callers control ordering relative to their own
        post-fetch work (beads mirror, duplicate-bead sweep).
        """
        return await self._gh.list_issues(state=state, since=since)

    async def cache_issues(
        self,
        issues: list[GitHubIssueRecord],
        *,
        cursor: str,
    ) -> None:
        """Upsert issues (when non-empty) and advance the sync cursor."""
        if issues:
            await self._store.cache_github_issues(self._session_id, issues)
        await self._store.set_last_issue_sync_at(self._session_id, cursor)

    async def fetch_trusted_open_pull_requests(
        self,
        *,
        limit: int,
        trusted_authors: frozenset[str] | None = None,
        context: str,
    ) -> list[PullRequestRecord]:
        """Fetch open PRs and filter to trusted authors."""
        pull_requests = await self._gh.list_pull_requests(state="open", limit=limit)
        return filter_trusted_pull_requests(
            pull_requests,
            self._cfg,
            trusted_authors=trusted_authors,
            context=context,
        )

    async def resync_missing_pull_requests(
        self,
        *,
        fetched_open: list[PullRequestRecord],
        limit: int,
        trusted_authors: frozenset[str] | None,
    ) -> list[PullRequestRecord]:
        """Re-fetch locally-open PRs absent from the fresh open-list.

        Any locally-cached open PR that did not appear in ``fetched_open`` has
        likely transitioned to MERGED or CLOSED on GitHub. Re-pulling those by
        number via ``state="all"`` lets the cache pick up the new state. Returns
        the resolved records (possibly empty); the caller decides how to merge
        them into the open set and how to log.
        """
        fetched_numbers = {pr.pr_number for pr in fetched_open}
        locally_open = await self._store.list_open_pull_requests(self._session_id)
        missing = [pr for pr in locally_open if pr.pr_number not in fetched_numbers]
        if not missing:
            return []
        all_prs = await self._gh.list_pull_requests(state="all", limit=limit)
        all_prs = filter_trusted_pull_requests(
            all_prs,
            self._cfg,
            trusted_authors=trusted_authors,
            context="refresh_resync",
        )
        by_number = {pr.pr_number: pr for pr in all_prs}
        return [by_number[stale.pr_number] for stale in missing if stale.pr_number in by_number]

    async def cache_pull_requests(self, pull_requests: list[PullRequestRecord]) -> None:
        await self._store.cache_pull_requests(self._session_id, pull_requests)

    def trusted_authors(self) -> frozenset[str]:
        return trusted_pr_author_logins(self._cfg)
