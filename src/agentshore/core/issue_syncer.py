"""GitHub issue/PR sync and worktree sweep collaborator for CompletionProcessor."""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING

import aiosqlite

from agentshore.core.helpers import _logger

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.core.session_runtime import SessionRuntime
    from agentshore.data.store import DataStore, PullRequestRecord
    from agentshore.state import PlayType


_DUPLICATE_BEAD_TITLE_RE = re.compile(r"^Duplicate bead", re.IGNORECASE)
_PR_LIMIT = 50

# Signatures meaning issue_pickup found the issue already CLOSED on GH while our
# cache still listed it open. Incremental ``since=`` has been seen missing
# close-state transitions for 30+ cycles ($0.10–0.20 per phantom pickup); this
# forces a full sync next refresh so the cache self-heals (#966, 2026-05-28).
_ALREADY_CLOSED_SIGNATURES: tuple[str, ...] = (
    "is already closed",
    "already CLOSED",
    "already closed",
)


class IssueSyncer:
    """Owns GitHub issue/PR refresh and worktree sweep logic.

    Extracted from ``CompletionProcessor`` — all behaviour is verbatim.
    Constructed inside ``CompletionProcessor.__init__`` from the already-
    injected deps; ``CompletionProcessor.refresh_issues`` delegates here.
    """

    def __init__(
        self,
        *,
        store: DataStore,
        session_id: str,
        repo_root: Path,
        runtime: SessionRuntime,
    ) -> None:
        self._store = store
        self._session_id = session_id
        self._repo_root = repo_root
        self._runtime = runtime

    async def refresh_issues(
        self,
        completing_play: PlayType | None = None,
        *,
        force_full_sync: bool = False,
        full_issue_sync_plays: frozenset[PlayType],
    ) -> None:
        """Re-fetch GitHub issues and update the cache.

        Two modes (desktop-rla8):

        - **Full sync**: a complete paginated sweep of all issues. Triggered
          when the completing play is in ``_FULL_ISSUE_SYNC_PLAYS``
          (``seed_project``, ``cleanup``, ``reconcile_state``, ``prune``),
          when ``force_full_sync`` is True (caller has out-of-band evidence
          the incremental cursor is missing a state transition), or when no
          ``last_issue_sync_at`` cursor exists yet. Catches deletions and
          repo transfers, which don't bump ``updated_at`` and so are
          invisible to incremental sync.
        - **Incremental sync**: a ``since=<last_sync_at>`` query that
          typically returns 0–5 changed issues per call. The default.

        For pull requests, the open-only fetch is followed by a "missing PR"
        sweep: any locally-cached open PR that did not appear in the fresh
        open-list has likely transitioned to MERGED or CLOSED on GitHub.
        Re-fetching those by number via ``state="all"`` lets the cache pick
        up the new state.
        """
        from agentshore.core.github_syncer import GitHubSyncer, sync_cursor_now

        try:
            from agentshore.github.adapter import GitHubAdapter

            gh = GitHubAdapter(
                store=self._store, session_id=self._session_id, cfg=self._runtime.cfg
            )
            await gh.probe()
            syncer = GitHubSyncer(
                gh=gh, store=self._store, cfg=self._runtime.cfg, session_id=self._session_id
            )
            if gh.available:
                last_sync = await self._store.get_last_issue_sync_at(self._session_id)
                full_sync = (
                    force_full_sync or completing_play in full_issue_sync_plays or last_sync is None
                )
                since = None if full_sync else last_sync

                # Capture the cutoff *before* the fetch so mid-fetch updates are
                # picked up next time; lookback absorbs gh/local clock skew.
                new_cutoff = sync_cursor_now()

                # ``state="all"`` so close/reopen transitions surface — the
                # cache_github_issues upsert flips local state to match.
                issues = await syncer.fetch_issues(state="all", since=since)
                if issues is None:
                    _logger.warning(
                        "github_issues_refresh_failed",
                        full_sync=full_sync,
                        since=since,
                    )
                else:
                    await syncer.cache_issues(issues, cursor=new_cutoff)
                    _logger.info(
                        "github_issues_refreshed",
                        changed_count=len(issues),
                        full_sync=full_sync,
                        cursor=new_cutoff,
                    )

                # Duplicate-bead close sweep runs only on full sync — needs the
                # complete open-issue set to find issues whose only beads are closed dups.
                if full_sync and issues is not None:
                    open_issues = [iss for iss in issues if iss.state == "open"]
                    from agentshore.beads import (  # noqa: PLC0415
                        BeadStatus,
                        GraphReadError,
                        GraphTask,
                        load_graph,
                    )

                    try:
                        graph = await load_graph(self._repo_root)
                    except GraphReadError:
                        graph = None
                    if graph is not None:
                        tasks_by_issue: dict[int, list[GraphTask]] = {}
                        for task in graph.tasks:
                            issue_number = task.issue_number
                            if issue_number is None:
                                continue
                            tasks_by_issue.setdefault(issue_number, []).append(task)
                        for issue in open_issues:
                            related = tasks_by_issue.get(issue.issue_number, [])
                            if not related:
                                continue
                            has_live = any(task.status != BeadStatus.CLOSED for task in related)
                            if has_live:
                                continue
                            if not any(
                                _DUPLICATE_BEAD_TITLE_RE.match(task.title) for task in related
                            ):
                                continue
                            key = f"{self._session_id}:duplicate-close:{issue.issue_number}"
                            closed = await gh.close_issue(issue.issue_number, idempotency_key=key)
                            if closed:
                                await self._store.update_issue_state(
                                    issue.issue_number,
                                    self._session_id,
                                    "closed",
                                )
                                _logger.info(
                                    "github_issue_duplicate_bead_closed",
                                    issue_number=issue.issue_number,
                                    bead_count=len(related),
                                )
                trusted_pr_authors = syncer.trusted_authors()
                pull_requests = await syncer.fetch_trusted_open_pull_requests(
                    limit=_PR_LIMIT,
                    trusted_authors=trusted_pr_authors,
                    context="refresh_open",
                )
                resync = await syncer.resync_missing_pull_requests(
                    fetched_open=pull_requests,
                    limit=_PR_LIMIT,
                    trusted_authors=trusted_pr_authors,
                )
                refetched = resync.resolved
                if refetched:
                    pull_requests.extend(refetched)
                    _logger.info("github_pull_requests_state_resync", count=len(refetched))
                if pull_requests:
                    await syncer.cache_pull_requests(pull_requests)
                    _logger.info("github_pull_requests_refreshed", changed_count=len(pull_requests))
                # Absence reconciliation (#279): mark locally-open PRs GitHub has no
                # object for (phantoms/hallucinated numbers) ``absent`` — drops them
                # from code_review eligibility and drains their review-queue rows.
                for pr_number in resync.absent:
                    await self._store.mark_pull_request_absent(self._session_id, pr_number)
                if resync.absent:
                    _logger.info(
                        "pr_absence_reconciled",
                        count=len(resync.absent),
                        pr_numbers=resync.absent,
                    )
                # Review-queue reconciliation (reaper + sweep), against the
                # freshly-cached store state so it sees the latest labels/SHAs.
                await self._reconcile_review_queue()
                # Mark worktree rows ``stale`` for PRs that went MERGED/CLOSED (from
                # ``refetched``), then run the TTL reaper (``stale`` rows older than
                # ``reap_ttl_seconds``).
                await self._mark_worktrees_stale_for_closed_prs(refetched)
                await self._sweep_closed_pr_worktrees()
                await self._sweep_disk_pressure_worktrees()
        except (FileNotFoundError, TimeoutError, OSError, aiosqlite.Error) as exc:
            _logger.warning("github_refresh_failed", error=str(exc))
        finally:
            self._runtime.last_refresh_time = time.monotonic()
            await self._ensure_ssh_key_fresh()

    async def _reconcile_review_queue(self) -> None:
        """Reaper + sweep for the review queue, run on each GitHub refresh.

        Reaper: drain queue rows for PRs parked ``manual-required`` — a human
        owns them, not an AgentShore reviewer, so they should not occupy the
        queue. Sweep: enqueue every open, reviewable, trusted PR that lacks a
        live queue row, so PRs from ANY source (cleanup, a prior session, an
        external trusted identity) flow through code_review → merge — not only
        PRs an AgentShore play authored via the artifact path. ``enqueue_review``
        is INSERT OR IGNORE, so PRs already queued (pending or claimed) are
        skipped; ``pr_reviewable`` already excludes drafts, manual-required, and
        PRs already reviewed at the current head, so a reviewed PR is not
        re-enqueued and the queue does not churn.
        """
        from agentshore.data.models import ReviewQueueRecord  # noqa: PLC0415
        from agentshore.github.labels import MANUAL_REQUIRED_LABEL  # noqa: PLC0415
        from agentshore.plays.candidates.predicates import pr_reviewable  # noqa: PLC0415
        from agentshore.utils import now_iso  # noqa: PLC0415

        open_prs = await self._store.list_open_pull_requests(self._session_id)
        if not open_prs:
            return
        manual_required = [
            pr.pr_number for pr in open_prs if MANUAL_REQUIRED_LABEL in (pr.labels or [])
        ]
        drained = await self._store.drain_review_queue_for_prs(self._session_id, manual_required)
        enqueued = 0
        for pr in open_prs:
            if not pr_reviewable(pr):
                continue
            queue_id = await self._store.enqueue_review(
                ReviewQueueRecord(
                    pr_number=pr.pr_number,
                    session_id=self._session_id,
                    enqueued_at=now_iso(),
                    author_label=pr.github_author or "external",
                )
            )
            if queue_id:
                enqueued += 1
        if drained or enqueued:
            _logger.info("review_queue_swept", drained=drained, enqueued=enqueued)

    async def _ensure_ssh_key_fresh(self) -> None:
        """Re-check the SSH signing key periodically so merge_pr doesn't fail."""
        try:
            from agentshore.core.git_safety import ensure_ssh_signing_key_loaded  # noqa: PLC0415

            loaded, detail = await asyncio.to_thread(ensure_ssh_signing_key_loaded)
            if not loaded:
                _logger.debug("ssh_signing_key_refresh_failed", detail=detail)
        except Exception:
            pass

    async def _mark_worktrees_stale_for_closed_prs(
        self,
        refetched_prs: list[PullRequestRecord],
    ) -> None:
        """Transition worktree rows to ``stale`` for PRs that just closed/merged.

        Called from ``refresh_issues`` with the PRs we re-pulled at
        ``state='all'`` to confirm their new state. A PR whose new state is
        anything other than ``"open"`` no longer needs an active worktree;
        the closed-PR TTL reaper will sweep it after the grace period.
        """
        if self._runtime.worktrees is None or not refetched_prs:
            return
        from agentshore.agents.worktree.registry import (  # noqa: PLC0415
            lookup_by_branch,
            mark_status,
        )

        for pr in refetched_prs:
            if pr.state == "open" or not pr.branch:
                continue
            try:
                row = await lookup_by_branch(
                    self._store, session_id=self._session_id, branch_name=pr.branch
                )
            except (OSError, aiosqlite.Error) as exc:
                _logger.warning(
                    "worktree_stale_lookup_failed",
                    branch=pr.branch,
                    error=str(exc),
                )
                continue
            if row is None or row.status != "active":
                continue
            try:
                await mark_status(
                    self._store,
                    worktree_id=row.worktree_id,
                    status="stale",
                    failure_reason=f"pr_closed_state_{pr.state}",
                )
                _logger.info(
                    "worktree_marked_stale_for_closed_pr",
                    worktree_id=row.worktree_id,
                    branch=pr.branch,
                    pr_state=pr.state,
                )
            except (OSError, aiosqlite.Error) as exc:
                _logger.warning(
                    "worktree_stale_mark_failed",
                    worktree_id=row.worktree_id,
                    branch=pr.branch,
                    error=str(exc),
                )

    async def _sweep_closed_pr_worktrees(self) -> None:
        """Run the TTL reaper for ``stale`` worktree rows in the current session.

        In-flight worktrees are protected: a PR can close (marking its row
        ``stale``) while the worktree is mid-dispatch, and reaping it out from
        under the running play is the "worktree reclaimed mid-play" failure
        (#189). The protected set is derived the same way the disk-pressure
        sweep derives it — from the live dispatch contexts.
        """
        if self._runtime.worktrees is None:
            return
        try:
            report = await self._runtime.worktrees.reap_closed_prs(
                ttl_seconds=self._runtime.cfg.worktrees.reap_ttl_seconds,
            )
        except (OSError, aiosqlite.Error, ValueError) as exc:
            _logger.warning("worktree_pr_ttl_reap_failed", error=str(exc))
            return
        if report.total > 0:
            _logger.info(
                "worktree_pr_ttl_reap",
                reaped=len(report.removed),
                failed=len(report.failed),
                ttl_seconds=self._runtime.cfg.worktrees.reap_ttl_seconds,
            )

    async def _sweep_disk_pressure_worktrees(self) -> None:
        """Reap idle worktrees LRU when free disk is below the high-water mark.

        The periodic arm of the build-agnostic disk governor (#180): runs on the
        GitHub-poll cadence alongside the TTL reaper. No-op when
        ``disk_high_water_mb == 0`` (disabled) or disk is already above target.
        In-flight worktrees are protected.
        """
        if self._runtime.worktrees is None:
            return
        target_mb = self._runtime.cfg.worktrees.disk_high_water_mb
        if target_mb <= 0:
            return
        try:
            report = await self._runtime.worktrees.reap_for_disk_pressure(
                target_free_mb=target_mb,
            )
        except (OSError, aiosqlite.Error, ValueError) as exc:
            _logger.warning("worktree_disk_pressure_reap_failed", error=str(exc))
            return
        if report.total > 0:
            _logger.info(
                "worktree_disk_pressure_reap",
                reaped=len(report.removed),
                failed=len(report.failed),
                target_mb=target_mb,
            )
