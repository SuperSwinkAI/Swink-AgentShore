"""Module-level helpers shared across DataStore mixins."""

from __future__ import annotations

import importlib.resources
import json
from typing import TYPE_CHECKING

from agentshore.data.store.rows import _seed_last_reviewed_sha
from agentshore.github.pr_links import issue_numbers_for_pr

if TYPE_CHECKING:
    from agentshore.data.models import PullRequestRecord

# Canonical ordered column list for the ``pull_requests`` table. This is the
# single source of truth: the upsert below, every SELECT (via ``_PR_SELECT``),
# the value tuple in ``_pull_request_upsert_row``, and the row mapper
# ``_row_to_pull_request`` all derive their column set from this tuple. Adding a
# column is a one-line edit here — the writer/reader disagreement that left
# ``base_ref`` write-only (missing from the SELECTs and the mapper) is
# structurally impossible with a single owner.
_PULL_REQUEST_COLUMNS: tuple[str, ...] = (
    "pr_number",
    "session_id",
    "issue_number",
    "linked_issue_numbers",
    "branch",
    "state",
    "title",
    "url",
    "github_author",
    "labels",
    "review_decision",
    "status_check_summary",
    "is_draft",
    "author_agent_id",
    "author_agent_type",
    "created_at",
    "merged_at",
    "head_sha",
    "mergeable",
    "base_ref",
    "last_reviewed_sha",
    "last_review_status",
)

# SELECT prefix shared by every PR read method — appended with WHERE/ORDER BY.
_PR_SELECT = f"SELECT {', '.join(_PULL_REQUEST_COLUMNS)} FROM pull_requests"

_PULL_REQUEST_UPSERT_SQL = f"""
    INSERT INTO pull_requests
        ({", ".join(_PULL_REQUEST_COLUMNS)})
    VALUES ({", ".join("?" for _ in _PULL_REQUEST_COLUMNS)})
    ON CONFLICT(pr_number, session_id) DO UPDATE SET
        issue_number = COALESCE(excluded.issue_number, pull_requests.issue_number),
        linked_issue_numbers = COALESCE(
            excluded.linked_issue_numbers,
            pull_requests.linked_issue_numbers
        ),
        branch = COALESCE(excluded.branch, pull_requests.branch),
        state = excluded.state,
        title = COALESCE(NULLIF(excluded.title, ''), pull_requests.title),
        url = COALESCE(excluded.url, pull_requests.url),
        github_author = COALESCE(pull_requests.github_author, excluded.github_author),
        labels = COALESCE(excluded.labels, pull_requests.labels),
        -- review_decision is taken authoritatively from GitHub, NOT COALESCE-preserved.
        -- GitHub's reviewDecision is the source of truth: an empty/NULL value means
        -- "no current decision" — e.g. a CHANGES_REQUESTED dismissed by a new commit —
        -- which is real state, not a transient gap. COALESCE-preserving it froze a stale
        -- CHANGES_REQUESTED forever, which kept pr_merge_ready() false and parked
        -- otherwise merge-ready PRs as manual-required (blocky PR #517). The #344 guarantee
        -- (a LIVE CHANGES_REQUESTED blocks and an AgentShore PASS cannot override it) is
        -- enforced in pr_state.blocked_reasons, not here, so it is unaffected.
        review_decision = excluded.review_decision,
        status_check_summary = COALESCE(
            excluded.status_check_summary,
            pull_requests.status_check_summary
        ),
        is_draft = COALESCE(excluded.is_draft, pull_requests.is_draft),
        author_agent_id = COALESCE(pull_requests.author_agent_id, excluded.author_agent_id),
        author_agent_type = COALESCE(
            pull_requests.author_agent_type,
            excluded.author_agent_type
        ),
        created_at = COALESCE(excluded.created_at, pull_requests.created_at),
        -- merged_at is valid only while the PR is actually MERGED. A GitHub
        -- refresh overwrites state above (state = excluded.state); preserving a
        -- prior merged_at via COALESCE when the refreshed state is NOT merged
        -- leaves a phantom (an optimistic mark_pr_merged write-through that the
        -- merge never actually completed — #344). Clear it whenever GitHub no
        -- longer reports the PR as merged so the stale timestamp can't mask the
        -- live blocked state; keep the precise timestamp while it stays merged.
        merged_at = CASE
            WHEN lower(excluded.state) = 'merged'
                THEN COALESCE(excluded.merged_at, pull_requests.merged_at)
            ELSE NULL
        END,
        head_sha = COALESCE(excluded.head_sha, pull_requests.head_sha),
        mergeable = COALESCE(excluded.mergeable, pull_requests.mergeable),
        base_ref = COALESCE(excluded.base_ref, pull_requests.base_ref),
        last_reviewed_sha = COALESCE(
            pull_requests.last_reviewed_sha,
            excluded.last_reviewed_sha
        ),
        last_review_status = COALESCE(
            pull_requests.last_review_status,
            excluded.last_review_status
        )
"""


def _load_schema_sql() -> str:
    """Load the bundled ``schema.sql`` via importlib.resources."""
    ref = importlib.resources.files("agentshore.data").joinpath("schema.sql")
    return ref.read_text(encoding="utf-8")


def _dedupe_resource_keys(resource_keys: list[str] | tuple[str, ...]) -> list[str]:
    """Return stable, non-empty, de-duplicated claim resource keys."""
    return [key for key in dict.fromkeys(resource_keys) if key]


def _dump_linked_issue_numbers(pr: PullRequestRecord) -> str | None:
    issue_numbers = issue_numbers_for_pr(pr)
    return json.dumps(list(issue_numbers)) if issue_numbers else None


def _pull_request_upsert_row(pr: PullRequestRecord) -> tuple[object, ...]:
    return (
        pr.pr_number,
        pr.session_id,
        pr.issue_number,
        _dump_linked_issue_numbers(pr),
        pr.branch,
        pr.state,
        pr.title,
        pr.url,
        pr.github_author,
        json.dumps(pr.labels) if pr.labels else None,
        pr.review_decision,
        pr.status_check_summary,
        int(pr.is_draft) if pr.is_draft is not None else None,
        pr.author_agent_id,
        pr.author_agent_type,
        pr.created_at,
        pr.merged_at,
        pr.head_sha,
        pr.mergeable,
        pr.base_ref,
        _seed_last_reviewed_sha(pr),
        pr.last_review_status,
    )
