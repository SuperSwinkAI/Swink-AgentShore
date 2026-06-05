"""DataStore mixin for the ``pull_requests`` table."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from agentshore.data.store.helpers import (
    _PR_SELECT,
    _PULL_REQUEST_UPSERT_SQL,
    _pull_request_upsert_row,
)
from agentshore.data.store.rows import _row_to_pull_request
from agentshore.github.pr_links import issue_numbers_for_pr
from agentshore.utils import now_iso

if TYPE_CHECKING:
    import aiosqlite

    from agentshore.data.models import PullRequestRecord


class _PullRequestsMixin:
    """Methods that operate on the ``pull_requests`` and ``branch_activity`` tables."""

    _db: aiosqlite.Connection | None
    _conn: aiosqlite.Connection

    if TYPE_CHECKING:
        # Cross-mixin call: actual impl is in _WorkClaimsMixin.
        async def supersede_work_claims(
            self,
            session_id: str,
            resource_keys: list[str] | tuple[str, ...],
        ) -> None: ...

    async def record_pull_request(self, pr: PullRequestRecord) -> None:
        """Upsert a PR record, preserving existing authorship when metadata refreshes."""
        await self._conn.execute(_PULL_REQUEST_UPSERT_SQL, _pull_request_upsert_row(pr))
        await self._conn.commit()

    async def cache_pull_requests(
        self, session_id: str, pull_requests: list[PullRequestRecord]
    ) -> None:
        """Cache GitHub PR metadata for environment/UI state."""
        if not pull_requests:
            return
        for pr in pull_requests:
            if pr.session_id != session_id:
                pr.session_id = session_id
        # For pre-existing PRs that are already APPROVED on GitHub at the time
        # we first see them, seed last_reviewed_sha=head_sha so the policy
        # routes them straight to merge_pr instead of redundantly
        # re-reviewing work an authorised reviewer has already approved. The
        # ON CONFLICT clause below preserves any existing DB value via
        # COALESCE, so this only takes effect on the very first insert for a
        # given (pr_number, session_id) — AgentShore-authored PRs are
        # untouched because we set last_reviewed_sha explicitly via
        # update_pr_last_reviewed_sha after a successful code_review.
        rows = [_pull_request_upsert_row(pr) for pr in pull_requests]
        await self._conn.executemany(_PULL_REQUEST_UPSERT_SQL, rows)
        await self._conn.commit()

    async def update_pr_last_reviewed_sha(
        self,
        pr_number: int,
        session_id: str,
        head_sha: str,
        *,
        status: str | None = None,
    ) -> None:
        """Record the HEAD SHA that was successfully reviewed for *pr_number*.

        Called by CodeReviewPlay after a successful review so that subsequent
        policy steps can mask code_review for PRs with no new commits. When
        ``status`` is provided ("PASS" or "BLOCK"), persist AgentShore's verdict
        atomically with the SHA so merge_pr can gate on internal approval
        when GitHub reviewDecision is unavailable. When omitted, leave
        last_review_status unchanged so SKIP/dedup paths don't overwrite a
        prior verdict.
        """
        if status is None:
            await self._conn.execute(
                "UPDATE pull_requests SET last_reviewed_sha = ? "
                "WHERE pr_number = ? AND session_id = ?",
                (head_sha, pr_number, session_id),
            )
        else:
            await self._conn.execute(
                "UPDATE pull_requests SET last_reviewed_sha = ?, last_review_status = ? "
                "WHERE pr_number = ? AND session_id = ?",
                (head_sha, status, pr_number, session_id),
            )
        await self._conn.commit()

    async def add_pull_request_labels(
        self,
        session_id: str,
        pr_number: int,
        labels: list[str] | tuple[str, ...],
    ) -> None:
        """Add labels to the cached PR row without waiting for GitHub refresh."""
        if not labels:
            return
        pr = await self.get_pull_request(session_id, pr_number)
        if pr is None:
            return
        merged = list(dict.fromkeys([*pr.labels, *[str(label) for label in labels]]))
        await self._conn.execute(
            """
            UPDATE pull_requests
               SET labels = ?
             WHERE session_id = ? AND pr_number = ?
            """,
            (json.dumps(merged), session_id, pr_number),
        )
        await self._conn.commit()

    async def mark_pr_merged(
        self,
        pr_number: int,
        session_id: str,
        *,
        merged_at: str | None = None,
    ) -> None:
        """Mark a PR as MERGED in the cache (post-merge write-through).

        Called from MergePRPlay's success path so the next state snapshot
        reflects the merge immediately, without waiting for the next
        ``_refresh_issues`` cycle. Without this, the resolver keeps picking
        the just-merged PR for ``unblock_pr`` until the GitHub
        refresh detects the state change — wasting agent time confirming
        what GitHub already knows.

        ``merged_at`` is optional; when omitted, defaults to ``now_iso()``.
        Existing non-null ``merged_at`` is preserved (a refresh from GitHub
        may have already populated the precise timestamp).
        """
        await self._conn.execute(
            """
            UPDATE pull_requests
            SET state = 'MERGED',
                merged_at = COALESCE(merged_at, ?)
            WHERE pr_number = ? AND session_id = ?
            """,
            (merged_at or now_iso(), pr_number, session_id),
        )
        await self._conn.commit()
        keys = [f"pr:{pr_number}"]
        pr = await self.get_pull_request(session_id, pr_number)
        if pr is not None:
            keys.extend(f"issue:{issue_number}" for issue_number in issue_numbers_for_pr(pr))
        await self.supersede_work_claims(session_id, keys)

    async def get_pr_author(self, pr_number: int, session_id: str) -> str | None:
        """Return the agent_id that authored *pr_number*, or None if unknown."""
        async with self._conn.execute(
            "SELECT author_agent_id FROM pull_requests WHERE pr_number = ? AND session_id = ?",
            (pr_number, session_id),
        ) as cursor:
            row = await cursor.fetchone()
            return row["author_agent_id"] if row else None

    async def get_pr_author_type(self, pr_number: int, session_id: str) -> str | None:
        """Return the agent_type ("claude_code", "codex", ...) that authored *pr_number*.

        Returns None if the PR is unknown or pre-dates the column.

        Retained for observability / kanban metadata. Identity-based code-review
        deconfliction uses ``get_pr_github_author`` instead.
        """
        async with self._conn.execute(
            "SELECT author_agent_type FROM pull_requests WHERE pr_number = ? AND session_id = ?",
            (pr_number, session_id),
        ) as cursor:
            row = await cursor.fetchone()
            return row["author_agent_type"] if row else None

    async def get_pr_github_author(self, pr_number: int, session_id: str) -> str | None:
        """Return the GitHub login that authored *pr_number*, or None if unknown.

        Source of truth for identity-based code-review anti-confirmation:
        a candidate reviewer is blocked iff its ``github_identity`` matches
        this login.
        """
        async with self._conn.execute(
            "SELECT github_author FROM pull_requests WHERE pr_number = ? AND session_id = ?",
            (pr_number, session_id),
        ) as cursor:
            row = await cursor.fetchone()
            return row["github_author"] if row else None

    async def get_pull_request(self, session_id: str, pr_number: int) -> PullRequestRecord | None:
        """Return one cached PR by number for this session."""
        async with self._conn.execute(
            f"{_PR_SELECT} WHERE session_id = ? AND pr_number = ?",
            (session_id, pr_number),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_pull_request(row) if row is not None else None

    async def list_open_pull_requests(self, session_id: str) -> list[PullRequestRecord]:
        """Return all open/review-blocked PRs for a session."""
        cursor = await self._conn.execute(
            f"""
            {_PR_SELECT}
            WHERE session_id = ?
              AND lower(state) IN (
                  'open', 'review_requested', 'blocked', 'changes_requested', 'ci_failed'
              )
            ORDER BY pr_number ASC
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_pull_request(row) for row in rows]

    async def list_active_pull_requests(self, session_id: str) -> list[PullRequestRecord]:
        """Return PRs still relevant to the active session environment."""
        cursor = await self._conn.execute(
            f"""
            {_PR_SELECT}
            WHERE session_id = ?
              AND lower(state) IN (
                  'open', 'review_requested', 'blocked', 'changes_requested',
                  'ci_failed', 'approved'
              )
            ORDER BY pr_number ASC
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_pull_request(row) for row in rows]

    async def list_recently_merged_pull_requests(
        self, session_id: str, *, hours: int = 24
    ) -> list[PullRequestRecord]:
        """Return merged PRs whose ``merged_at`` is within the last *hours*.

        Recent merged PRs feed the dashboard's Done column using the same PR
        projection as open review work. The issue-close mirror can arrive on a
        later refresh, so this keeps merge completions visible immediately.
        """
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        cursor = await self._conn.execute(
            f"""
            {_PR_SELECT}
            WHERE session_id = ?
              AND lower(state) = 'merged'
              AND merged_at IS NOT NULL
              AND merged_at > ?
            ORDER BY merged_at DESC
            """,
            (session_id, cutoff),
        )
        rows = await cursor.fetchall()
        return [_row_to_pull_request(row) for row in rows]

    async def list_approved_pull_requests(self, session_id: str) -> list[PullRequestRecord]:
        """Return all approved (ready-to-merge) PRs for a session, oldest first.

        Approval is accepted from either source:

        - GitHub's review_decision='APPROVED' (set by a non-author reviewer
          on GitHub; surfaced via ``gh pr view --json reviewDecision``).
        - AgentShore's internal verdict: ``last_review_status='PASS'`` AND
          ``last_reviewed_sha = head_sha`` (i.e. AgentShore's own code_review or
          unblock_pr play returned PASS at the current PR head). This covers
          rebases by letting unblock_pr stamp the rebased head as reviewed.

        Stale AgentShore approval (head_sha advanced past last_reviewed_sha)
        is excluded automatically by the SHA equality check. mergeable is
        filtered downstream by the resolver/play preconditions.
        """
        cursor = await self._conn.execute(
            f"""
            {_PR_SELECT}
            WHERE session_id = ?
              AND lower(state) = 'open'
              AND (
                  upper(review_decision) = 'APPROVED'
                  OR (
                      upper(last_review_status) = 'PASS'
                      AND last_reviewed_sha IS NOT NULL
                      AND head_sha IS NOT NULL
                      AND last_reviewed_sha = head_sha
                  )
              )
            ORDER BY pr_number ASC
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_pull_request(row) for row in rows]
