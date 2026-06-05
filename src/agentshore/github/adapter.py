"""GitHub CLI adapter — wraps `gh` subprocess for all GitHub operations."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from typing import TYPE_CHECKING

import aiosqlite
import structlog

from agentshore.data.store import ExternalMutationRecord, GitHubIssueRecord, PullRequestRecord
from agentshore.github.labels import PRIORITY_SCORES
from agentshore.github.pr_links import infer_pr_issue_links
from agentshore.pr_state import label_names, status_rollup_summary
from agentshore.utils import now_iso

if TYPE_CHECKING:
    from agentshore.config import RuntimeConfig
    from agentshore.data.store import DataStore

_logger = structlog.get_logger(__name__)

_GH_TIMEOUT = 30  # seconds — for single-issue mutations + small queries
# List operations (issues, PRs) can fetch up to GITHUB_ISSUE_FETCH_LIMIT=200
# items per call. GitHub's GraphQL list endpoint takes ~0.5s/item, so a full
# 200-issue snapshot can take ~100s. The shorter mutation timeout silently
# returned [] on big repos; lifted ceiling here keeps cold-start correct on
# repos with backlogs while still failing fast on a genuinely hung gh subprocess.
_GH_LIST_TIMEOUT = 180  # seconds

# Per-page bound for paginated REST list_issues (desktop-rla8). 25 keeps each
# request cheap on the gh side and bounded in memory; a 290-issue full sync
# costs ~12 small requests. Page bound (_LIST_ISSUES_MAX_PAGES) caps total
# work at 1000×25=25k issues, well above any realistic repo size.
_ISSUES_PER_PAGE = 25
_LIST_ISSUES_MAX_PAGES = 1000

# Shared ``--json`` field list for the cacheable PR record. Single-sourced so
# ``list_pull_requests`` and ``fetch_pull_request_by_number`` can never drift
# (they did before the desktop-08a948ed fix). Any field added to
# ``_pr_record_from_json`` must be added here too.
_PR_JSON_FIELDS = (
    "number,title,url,state,headRefName,baseRefName,headRefOid,labels,"
    "reviewDecision,statusCheckRollup,isDraft,author,createdAt,mergeable,"
    "body,closingIssuesReferences"
)


def _priority_from_labels(labels: list[str]) -> int | None:
    """Return the numeric priority rank from labels, or None if no priority label."""
    for label in labels:
        rank = PRIORITY_SCORES.get(label)
        if rank is not None:
            return rank
    return None


def _pr_record_from_json(session_id: str, item: dict[str, object]) -> PullRequestRecord | None:
    """Map a single ``gh pr`` JSON object (queried with ``_PR_JSON_FIELDS``) to a
    ``PullRequestRecord``, or ``None`` if the item lacks a usable PR number.

    Single source of truth for the record schema shared by
    ``list_pull_requests`` and ``fetch_pull_request_by_number``.
    """
    raw_number = item.get("number")
    if not isinstance(raw_number, (int, str)):
        _logger.warning("gh_response_missing_number", item=item)
        return None
    labels = label_names(item.get("labels", []))
    author = item.get("author")
    github_author = (
        str(author.get("login"))
        if isinstance(author, dict) and author.get("login") is not None
        else None
    )
    issue_links = infer_pr_issue_links(
        closing_issue_references=item.get("closingIssuesReferences"),
        body=item.get("body"),
        branch=item.get("headRefName"),
    )
    return PullRequestRecord(
        pr_number=int(raw_number),
        session_id=session_id,
        state=str(item.get("state", "OPEN")).lower(),
        created_at=str(item.get("createdAt", now_iso())),
        issue_number=issue_links.primary_issue_number,
        linked_issue_numbers=issue_links.issue_numbers,
        branch=str(item["headRefName"]) if item.get("headRefName") else None,
        title=str(item.get("title", "")),
        url=str(item["url"]) if item.get("url") else None,
        github_author=github_author,
        labels=labels,
        review_decision=(str(item["reviewDecision"]) if item.get("reviewDecision") else None),
        status_check_summary=status_rollup_summary(item.get("statusCheckRollup")),
        is_draft=bool(item.get("isDraft", False)),
        head_sha=str(item["headRefOid"]) if item.get("headRefOid") else None,
        mergeable=str(item["mergeable"]) if item.get("mergeable") else None,
        base_ref=str(item["baseRefName"]) if item.get("baseRefName") else None,
    )


class GitHubUnavailableError(Exception):
    """Raised when the `gh` CLI is absent or unauthenticated."""


class GitHubAdapter:
    """Thin async wrapper around `gh` CLI commands.

    Instantiate once per session.  Call ``probe()`` to verify availability
    before first use — after a failed probe all read methods return empty
    lists and all mutating methods are no-ops.
    """

    def __init__(self, store: DataStore, session_id: str, cfg: RuntimeConfig) -> None:
        self._store = store
        self._session_id = session_id
        self._cfg = cfg
        self._available: bool = True

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def probe(self) -> None:
        """Check that `gh` is on PATH and authenticated.

        Sets ``_available = False`` on any failure so callers can proceed in
        degraded mode rather than crashing.
        """
        try:
            rc, _, _ = await _run_gh(["auth", "status"], timeout=_GH_TIMEOUT)
            if rc != 0:
                _logger.warning("gh_auth_failed", session_id=self._session_id)
                self._available = False
        except FileNotFoundError:
            _logger.warning("gh_not_found", session_id=self._session_id)
            self._available = False
        except (OSError, TimeoutError) as exc:
            _logger.warning("gh_probe_error", error=str(exc), session_id=self._session_id)
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    # -------------------------------------------------------------------------
    # Read operations
    # -------------------------------------------------------------------------

    async def list_issues(
        self,
        state: str = "open",
        since: str | None = None,
    ) -> list[GitHubIssueRecord] | None:
        """Return issues from the current repo, applying label filters from config.

        Uses paginated REST (``gh api repos/{owner}/{repo}/issues``) at 25
        issues per page, looping until a short page signals the last one.

        Args:
            state: ``open`` | ``closed`` | ``all``.
            since: Optional ISO8601 timestamp; only return issues with
                ``updated_at >= since``. ``None`` ⇒ unbounded (full sync).

        Returns ``None`` on hard error (gh unavailable, subprocess failure,
        parse error). An empty list is success — "no issues match," which is
        the common steady-state result for ``since=`` queries.

        The REST ``/issues`` endpoint returns pull requests too; entries with a
        ``pull_request`` key are filtered out here so callers see issues only.
        """
        if not self._available:
            return None

        raw_items = await self._paginated_issues(state=state, since=since)
        if raw_items is None:
            return None

        records: list[GitHubIssueRecord] = []
        include = self._cfg.intake.issue_labels_include
        exclude = self._cfg.intake.issue_labels_exclude

        for item in raw_items:
            # REST /issues mixes issues and PRs; PRs carry a ``pull_request``
            # key. Filter here so the rest of AgentShore never sees PRs as issues.
            if "pull_request" in item:
                continue

            label_objs = item.get("labels", [])
            if not isinstance(label_objs, list):
                label_objs = []
            label_names: list[str] = [
                str(lbl["name"]) if isinstance(lbl, dict) else str(lbl) for lbl in label_objs
            ]

            if include and not any(lbl in label_names for lbl in include):
                continue
            if exclude and any(lbl in label_names for lbl in exclude):
                continue

            raw_number = item.get("number")
            if not isinstance(raw_number, (int, str)):
                _logger.warning("gh_response_missing_number", item=item)
                continue
            user = item.get("user")
            github_author = (
                str(user["login"]) if isinstance(user, dict) and user.get("login") else None
            )
            records.append(
                GitHubIssueRecord(
                    issue_number=int(raw_number),
                    session_id=self._session_id,
                    title=str(item.get("title", "")),
                    state=str(item.get("state", "open")).lower(),
                    created_at=str(item.get("created_at", now_iso())),
                    closed_at=str(item["closed_at"]) if item.get("closed_at") else None,
                    labels=label_names,
                    priority=_priority_from_labels(label_names),
                    url=str(item["html_url"]) if item.get("html_url") else None,
                    github_author=github_author,
                )
            )

        return records

    async def _paginated_issues(
        self,
        state: str,
        since: str | None,
    ) -> list[dict[str, object]] | None:
        """Page through ``repos/{owner}/{repo}/issues`` 25 at a time.

        Returns the concatenated list of raw items, or ``None`` on hard error
        (network / gh exit non-zero / parse failure). An empty list is a valid
        success — it means "no issues match," which for ``since=`` queries is
        the common steady-state outcome.
        """
        all_items: list[dict[str, object]] = []
        for page in range(1, _LIST_ISSUES_MAX_PAGES + 1):
            params = [
                f"state={state}",
                f"per_page={_ISSUES_PER_PAGE}",
                f"page={page}",
            ]
            if since is not None:
                params.append(f"since={since}")
            endpoint = "repos/{owner}/{repo}/issues?" + "&".join(params)
            cmd = ["api", endpoint]
            try:
                rc, stdout, stderr = await _run_gh(cmd, timeout=_GH_LIST_TIMEOUT)
            except (OSError, TimeoutError) as exc:
                _logger.warning("gh_list_issues_error", error=str(exc), page=page)
                return None

            if rc != 0:
                _logger.warning("gh_list_issues_failed", stderr=stderr, page=page)
                return None

            try:
                chunk = json.loads(stdout)
            except json.JSONDecodeError:
                _logger.warning("gh_list_issues_parse_error", raw=stdout[:200], page=page)
                return None

            if not isinstance(chunk, list):
                _logger.warning("gh_list_issues_unexpected_shape", page=page)
                return None

            all_items.extend(chunk)
            if len(chunk) < _ISSUES_PER_PAGE:
                break
        else:
            _logger.warning("gh_list_issues_page_cap_hit", cap=_LIST_ISSUES_MAX_PAGES)

        return all_items

    async def list_open_prs(self) -> list[dict[str, object]]:
        """Return open PRs as raw dicts."""
        if not self._available:
            return []
        cmd = [
            "pr",
            "list",
            "--state",
            "open",
            "--json",
            "number,title,headRefName,author,createdAt",
        ]
        return await self._gh_json_list(cmd)

    async def list_pull_requests(
        self,
        state: str = "open",
        limit: int = 50,
    ) -> list[PullRequestRecord]:
        """Return pull requests from the current repo as cacheable records."""
        if not self._available:
            return []

        cmd = [
            "pr",
            "list",
            "--state",
            state,
            "--json",
            _PR_JSON_FIELDS,
            "--limit",
            str(limit),
        ]
        raw_prs = await self._gh_json_list(cmd)
        records: list[PullRequestRecord] = []
        for item in raw_prs:
            record = _pr_record_from_json(self._session_id, item)
            if record is not None:
                records.append(record)
        return records

    async def fetch_pull_request_by_number(self, pr_number: int) -> PullRequestRecord | None:
        """Return a single PR's full cacheable record.

        Used to enrich the cache immediately after a play records a freshly
        created PR (``executor.py``). Without this, the executor inserts a
        row with NULLs for ``review_decision``, ``mergeable``, ``head_sha``,
        and ``is_draft``; subsequent COALESCE-based refreshes can leave
        those NULLs in place if the next sync misses the PR (observed
        2026-05-28 session 08a948ed: 5 freshly-recorded PRs sat with
        ``mergeable=None`` and ``head_sha=None`` for the rest of the
        session, blocking accurate ``mergeable_pr_count`` and the
        ``code_review`` already-approved short-circuit).
        """
        if not self._available:
            return None
        cmd = ["pr", "view", str(pr_number), "--json", _PR_JSON_FIELDS]
        item = await self._gh_json(cmd)
        if not isinstance(item, dict):
            return None
        return _pr_record_from_json(self._session_id, item)

    async def list_approved_prs(self) -> list[dict[str, object]]:
        """Return open PRs that have at least one approved review."""
        if not self._available:
            return []
        cmd = [
            "pr",
            "list",
            "--state",
            "open",
            "--json",
            "number,title,headRefName,reviews,statusCheckRollup",
        ]
        all_prs = await self._gh_json_list(cmd)
        approved = []
        for pr in all_prs:
            reviews = pr.get("reviews", [])
            if not isinstance(reviews, list):
                continue
            if any(isinstance(r, dict) and r.get("state") == "APPROVED" for r in reviews):
                approved.append(pr)
        return approved

    async def ensure_labels(self, required: list[tuple[str, str]]) -> None:
        """Create any labels in *required* that don't already exist in the repo.

        *required* is a list of (name, color) pairs where color is a 6-digit
        hex string without the leading ``#``.  Existing labels are fetched once
        and missing ones are created concurrently (bounded by a semaphore to
        stay polite to GitHub's secondary rate limit). Failures are logged and
        silently skipped.
        """
        if not self._available:
            return
        existing_raw = await self._gh_json_list(
            ["label", "list", "--json", "name", "--limit", "200"]
        )
        existing = {item["name"] for item in existing_raw if isinstance(item.get("name"), str)}
        missing = [(name, color) for name, color in required if name not in existing]
        if not missing:
            return

        sem = asyncio.Semaphore(8)

        async def _create(name: str, color: str) -> None:
            async with sem:
                try:
                    rc, _, stderr = await _run_gh(
                        ["label", "create", name, "--color", color, "--force"],
                        timeout=_GH_TIMEOUT,
                    )
                    if rc == 0:
                        _logger.info("gh_label_created", label=name)
                    else:
                        _logger.warning("gh_label_create_failed", label=name, stderr=stderr[:200])
                except (OSError, TimeoutError) as exc:
                    # f"{type(exc).__name__}: {exc}" — bare str(exc) is empty when
                    # exc.args is empty (e.g. OSError() with no message).
                    _logger.warning(
                        "gh_label_create_error",
                        label=name,
                        error=f"{type(exc).__name__}: {exc}",
                    )

        await asyncio.gather(*(_create(n, c) for n, c in missing))

    # -------------------------------------------------------------------------
    # Mutating operations
    # -------------------------------------------------------------------------

    async def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str],
        idempotency_key: str,
    ) -> dict[str, object] | None:
        if not self._available:
            return None
        pre_key = f"create_issue:{idempotency_key}"
        if await self._mutation_exists(pre_key):
            return None
        await self._record_mutation(pre_key, "create_issue", title, "pending")
        cmd = ["issue", "create", "--title", title, "--body", body]
        for lbl in labels:
            cmd += ["--label", lbl]
        result = await self._run_mutation(pre_key, "create_issue", title, cmd)
        return result

    async def label_issue(
        self,
        issue_number: int,
        labels: list[str],
        idempotency_key: str,
    ) -> bool:
        if not self._available:
            return False
        pre_key = f"label_issue:{idempotency_key}"
        if await self._mutation_exists(pre_key):
            return True
        await self._record_mutation(pre_key, "label_issue", str(issue_number), "pending")
        cmd = ["issue", "edit", str(issue_number), "--add-label", ",".join(labels)]
        result = await self._run_mutation(pre_key, "label_issue", str(issue_number), cmd)
        return result is not None

    async def close_issue(
        self,
        issue_number: int,
        idempotency_key: str,
    ) -> bool:
        if not self._available:
            return False
        pre_key = f"close_issue:{idempotency_key}"
        if await self._mutation_exists(pre_key):
            return True
        await self._record_mutation(pre_key, "close_issue", str(issue_number), "pending")
        cmd = ["issue", "close", str(issue_number)]
        result = await self._run_mutation(pre_key, "close_issue", str(issue_number), cmd)
        return result is not None

    async def create_pr(
        self,
        title: str,
        body: str,
        head: str,
        base: str,
        idempotency_key: str,
        *,
        identity_env: dict[str, str] | None = None,
    ) -> dict[str, object] | None:
        if not self._available:
            return None
        pre_key = f"create_pr:{idempotency_key}"
        if await self._mutation_exists(pre_key):
            return None
        await self._record_mutation(pre_key, "create_pr", head, "pending")
        cmd = [
            "pr",
            "create",
            "--title",
            title,
            "--body",
            body,
            "--head",
            head,
            "--base",
            base,
        ]
        result = await self._run_mutation(
            pre_key, "create_pr", head, cmd, identity_env=identity_env
        )
        if result:
            return result
        return await self.find_open_pr_by_branch(head, identity_env=identity_env)

    async def find_open_pr_by_branch(
        self,
        branch: str,
        *,
        identity_env: dict[str, str] | None = None,
    ) -> dict[str, object] | None:
        """Return the open PR whose head branch matches *branch*, if any."""
        if not self._available or not branch:
            return None
        raw = await self._gh_json_list(
            [
                "pr",
                "list",
                "--state",
                "open",
                "--head",
                branch,
                "--json",
                "number,url,headRefName,headRefOid,title",
                "--limit",
                "10",
            ],
            identity_env=identity_env,
        )
        for item in raw:
            if str(item.get("headRefName") or "") == branch:
                return item
        return raw[0] if raw else None

    async def default_branch(self, *, identity_env: dict[str, str] | None = None) -> str:
        """Return the repository default branch, falling back to ``main``."""
        raw = await self._gh_json(
            ["repo", "view", "--json", "defaultBranchRef"],
            identity_env=identity_env,
        )
        branch_ref = raw.get("defaultBranchRef")
        if isinstance(branch_ref, dict) and isinstance(branch_ref.get("name"), str):
            return str(branch_ref["name"])
        return "main"

    async def retarget_pr_base(
        self,
        pr_number: int,
        base: str,
        idempotency_key: str,
        *,
        identity_env: dict[str, str] | None = None,
    ) -> bool:
        """Retarget an open PR's base branch via ``gh pr edit --base``.

        Self-heals PRs opened against the wrong base (typically the repo
        default instead of the configured ``project.target_branch``), which
        otherwise strand as ``wrong_base_branch`` in ``merge_pr``. Idempotent
        via the mutation ledger; the working PR is untouched aside from its
        base ref. Returns True if the retarget ran (or was already recorded).
        """
        if not self._available or not base:
            return False
        pre_key = f"retarget_pr_base:{idempotency_key}"
        if await self._mutation_exists(pre_key):
            return True
        await self._record_mutation(pre_key, "retarget_pr_base", str(pr_number), "pending")
        cmd = ["pr", "edit", str(pr_number), "--base", base]
        result = await self._run_mutation(
            pre_key, "retarget_pr_base", str(pr_number), cmd, identity_env=identity_env
        )
        return result is not None

    async def merge_pr(
        self,
        pr_number: int,
        idempotency_key: str,
        strategy: str = "merge",
    ) -> bool:
        if not self._available:
            return False
        pre_key = f"merge_pr:{idempotency_key}"
        if await self._mutation_exists(pre_key):
            return True
        await self._record_mutation(pre_key, "merge_pr", str(pr_number), "pending")
        flag = f"--{strategy}"
        cmd = ["pr", "merge", str(pr_number), flag, "--auto"]
        result = await self._run_mutation(pre_key, "merge_pr", str(pr_number), cmd)
        return result is not None

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    async def _gh_json(
        self,
        cmd: list[str],
        *,
        identity_env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        try:
            rc, stdout, _ = await _run_gh(cmd, timeout=_GH_TIMEOUT, env=identity_env)
        except (OSError, TimeoutError) as exc:
            _logger.warning("gh_command_error", cmd=cmd[0], error=str(exc))
            return {}
        if rc != 0:
            return {}
        try:
            result = json.loads(stdout)
            return result if isinstance(result, dict) else {}
        except json.JSONDecodeError:
            return {}

    async def _gh_json_list(
        self,
        cmd: list[str],
        *,
        identity_env: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
        try:
            rc, stdout, _ = await _run_gh(cmd, timeout=_GH_LIST_TIMEOUT, env=identity_env)
        except (OSError, TimeoutError) as exc:
            _logger.warning("gh_command_error", cmd=cmd[0], error=str(exc))
            return []
        if rc != 0:
            return []
        try:
            result = json.loads(stdout)
            if isinstance(result, list):
                return result
            return []
        except json.JSONDecodeError:
            return []

    async def _mutation_exists(self, key: str) -> bool:
        existing = await self._store.get_external_mutation(self._session_id, key)
        if existing is not None and existing.status in ("ok", "pending"):
            _logger.debug(
                "gh_mutation_dedup",
                key=key,
                status=existing.status,
            )
            return True
        return False

    async def _record_mutation(
        self, key: str, mutation_type: str, target: str, status: str
    ) -> None:
        try:
            await self._store.record_external_mutation(
                ExternalMutationRecord(
                    session_id=self._session_id,
                    idempotency_key=key,
                    mutation_type=mutation_type,
                    target=target,
                    status=status,
                    created_at=now_iso(),
                )
            )
        except (aiosqlite.Error, sqlite3.Error, RuntimeError) as exc:
            _logger.warning("gh_record_mutation_failed", error=str(exc))

    async def _update_mutation_status(self, key: str, status: str, response: str) -> None:
        try:
            await self._store.update_external_mutation_status(
                self._session_id, key, status, response
            )
        except (aiosqlite.Error, sqlite3.Error, RuntimeError) as exc:
            _logger.warning("gh_update_mutation_failed", error=str(exc))

    async def _run_mutation(
        self,
        key: str,
        mutation_type: str,
        target: str,
        cmd: list[str],
        *,
        identity_env: dict[str, str] | None = None,
    ) -> dict[str, object] | None:
        try:
            rc, stdout, stderr = await _run_gh(cmd, timeout=_GH_TIMEOUT, env=identity_env)
        except (OSError, TimeoutError) as exc:
            _logger.warning("gh_mutation_error", mutation=mutation_type, error=str(exc))
            await self._update_mutation_status(key, "error", str(exc))
            return None

        if rc != 0:
            _logger.warning(
                "gh_mutation_failed",
                mutation=mutation_type,
                target=target,
                stderr=stderr[:500],
            )
            await self._update_mutation_status(key, "error", stderr)
            return None

        await self._update_mutation_status(key, "ok", stdout[:1000])
        try:
            parsed = json.loads(stdout) if stdout.strip() else {}
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        output = stdout.strip()
        if output.startswith("http://") or output.startswith("https://"):
            return {"url": output}
        return {}


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------


async def _run_gh(
    args: list[str],
    timeout: float = _GH_TIMEOUT,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run ``gh <args>`` and return (returncode, stdout, stderr)."""
    proc_env = {**os.environ, **env} if env else None
    proc = await asyncio.create_subprocess_exec(
        "gh",
        *args,
        env=proc_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        raise

    return (
        proc.returncode or 0,
        stdout_b.decode(errors="replace"),
        stderr_b.decode(errors="replace"),
    )
