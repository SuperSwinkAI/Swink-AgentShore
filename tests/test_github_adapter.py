"""Phase 4B: GitHubAdapter unit tests — all subprocess calls are mocked."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.config import RuntimeConfig
from agentshore.github import GitHubAdapter


def _make_adapter(
    tmp_path: Path, cfg: RuntimeConfig | None = None
) -> tuple[GitHubAdapter, MagicMock]:
    mock_store = AsyncMock()
    mock_store.get_external_mutation = AsyncMock(return_value=None)
    mock_store.record_external_mutation = AsyncMock()
    mock_store.update_external_mutation_status = AsyncMock()
    adapter = GitHubAdapter(
        store=mock_store,
        session_id="test-session",
        cfg=cfg or RuntimeConfig(),
    )
    return adapter, mock_store


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_sets_available_true_on_success(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)
    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (0, "", "")
        await adapter.probe()
    assert adapter.available is True


@pytest.mark.asyncio
async def test_probe_sets_available_false_on_nonzero(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)
    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (1, "", "")
        await adapter.probe()
    assert adapter.available is False


@pytest.mark.asyncio
async def test_probe_sets_available_false_when_gh_missing(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)
    # gh missing now surfaces as a normal return value (127, ...), not an exception.
    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (127, "", "gh not found")
        await adapter.probe()
    assert adapter.available is False


# ---------------------------------------------------------------------------
# list_issues
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_issues_returns_records(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)
    # REST /issues shape: snake_case keys, lowercase state, html_url.
    payload = json.dumps(
        [
            {
                "number": 1,
                "title": "Fix login bug",
                "html_url": "https://github.com/example/repo/issues/1",
                "state": "open",
                "labels": [{"name": "bug"}],
                "created_at": "2024-01-01T00:00:00Z",
                "closed_at": None,
            }
        ]
    )
    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (0, payload, "")
        records = await adapter.list_issues()

    assert records is not None
    assert len(records) == 1
    assert records[0].issue_number == 1
    assert records[0].title == "Fix login bug"
    assert records[0].url == "https://github.com/example/repo/issues/1"
    assert records[0].labels == ["bug"]
    assert records[0].priority is None  # no priority/* label


def _issue_json(
    number: int,
    *,
    state: str = "open",
    labels: list[str] | None = None,
    is_pr: bool = False,
) -> dict[str, object]:
    """Build a single REST /issues item with the new snake_case shape."""
    item: dict[str, object] = {
        "number": number,
        "title": f"Issue {number}",
        "html_url": f"https://github.com/example/repo/issues/{number}",
        "state": state,
        "labels": [{"name": lbl} for lbl in (labels or [])],
        "created_at": "2024-01-01T00:00:00Z",
        "closed_at": None,
    }
    if is_pr:
        item["pull_request"] = {"url": "https://api.github.com/.../pulls/1"}
    return item


@pytest.mark.asyncio
async def test_list_issues_extracts_priority_from_labels(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)
    payload = json.dumps(
        [
            _issue_json(1, labels=["priority/critical", "size/S"]),
            _issue_json(2, labels=["priority/low"]),
            _issue_json(3, labels=["priority/medium"]),
        ]
    )
    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (0, payload, "")
        records = await adapter.list_issues()

    assert records is not None
    by_num = {r.issue_number: r for r in records}
    assert by_num[1].priority == 0  # critical
    assert by_num[2].priority == 3  # low
    assert by_num[3].priority == 2  # medium


@pytest.mark.asyncio
async def test_list_issues_returns_none_when_unavailable(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)
    adapter._available = False
    records = await adapter.list_issues()
    assert records is None


@pytest.mark.asyncio
async def test_list_issues_applies_exclude_filter(tmp_path: Path) -> None:
    import dataclasses

    from agentshore.config import IntakeConfig, RuntimeConfig

    cfg = dataclasses.replace(
        RuntimeConfig(),
        intake=IntakeConfig(issue_labels_exclude=["wontfix"]),
    )
    adapter, _ = _make_adapter(tmp_path, cfg=cfg)
    payload = json.dumps(
        [
            _issue_json(1, labels=["wontfix"]),
            _issue_json(2, labels=["bug"]),
        ]
    )
    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (0, payload, "")
        records = await adapter.list_issues()

    assert records is not None
    assert len(records) == 1
    assert records[0].issue_number == 2


@pytest.mark.asyncio
async def test_list_issues_applies_include_filter(tmp_path: Path) -> None:
    import dataclasses

    from agentshore.config import IntakeConfig, RuntimeConfig

    cfg = dataclasses.replace(
        RuntimeConfig(),
        intake=IntakeConfig(issue_labels_include=["agentshore/active"]),
    )
    adapter, _ = _make_adapter(tmp_path, cfg=cfg)
    payload = json.dumps(
        [
            _issue_json(1, labels=["bug"]),
            _issue_json(2, labels=["agentshore/active"]),
        ]
    )
    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (0, payload, "")
        records = await adapter.list_issues()

    assert records is not None
    assert len(records) == 1
    assert records[0].issue_number == 2


@pytest.mark.asyncio
async def test_list_issues_returns_none_on_gh_error(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)
    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (1, "", "not found")
        records = await adapter.list_issues()
    assert records is None


@pytest.mark.asyncio
async def test_list_issues_filters_pull_requests(tmp_path: Path) -> None:
    """REST /issues returns PRs too; entries with ``pull_request`` are skipped."""
    adapter, _ = _make_adapter(tmp_path)
    payload = json.dumps(
        [
            _issue_json(1, labels=["bug"]),
            _issue_json(2, labels=["enhancement"], is_pr=True),
            _issue_json(3, labels=["bug"]),
        ]
    )
    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (0, payload, "")
        records = await adapter.list_issues()

    assert records is not None
    assert sorted(r.issue_number for r in records) == [1, 3]


@pytest.mark.asyncio
async def test_list_issues_paginates_until_short_page(tmp_path: Path) -> None:
    """Pages of 25 are requested in sequence until a page returns < 25 items."""
    adapter, _ = _make_adapter(tmp_path)
    page1 = json.dumps([_issue_json(i) for i in range(1, 26)])  # full page
    page2 = json.dumps([_issue_json(i) for i in range(26, 30)])  # short → last
    call_count = {"n": 0}

    async def fake(args: object, *a: object, **k: object) -> tuple[int, str, str]:
        call_count["n"] += 1
        stdout = page1 if call_count["n"] == 1 else page2
        return (0, stdout, "")

    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.side_effect = fake
        records = await adapter.list_issues()

    assert records is not None
    assert call_count["n"] == 2
    assert len(records) == 29


@pytest.mark.asyncio
async def test_list_issues_passes_since_in_query(tmp_path: Path) -> None:
    """``since=`` is forwarded to gh api as a query-string parameter."""
    adapter, _ = _make_adapter(tmp_path)
    captured: dict[str, list[str]] = {}

    async def fake(args: list[str], *a: object, **k: object) -> tuple[int, str, str]:
        captured["args"] = args
        return (0, "[]", "")

    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.side_effect = fake
        records = await adapter.list_issues(since="2026-05-23T22:00:00+00:00")

    assert records == []
    # gh argv (no leading "gh"): ["api", "repos/{owner}/{repo}/issues?...&since=..."]
    argv = captured["args"]
    assert any("since=2026-05-23T22:00:00+00:00" in str(a) for a in argv)
    assert any(str(a) == "api" for a in argv)


# ---------------------------------------------------------------------------
# list_pull_requests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pull_requests_returns_metadata_records(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)
    payload = json.dumps(
        [
            {
                "number": 42,
                "title": "Fix blocked flow",
                "url": "https://github.com/acme/repo/pull/42",
                "state": "OPEN",
                "headRefName": "agentshore/109-fix-blocked-flow",
                "labels": [{"name": "changes-requested"}],
                "reviewDecision": "CHANGES_REQUESTED",
                "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
                "isDraft": False,
                "author": {"login": "octocat"},
                "createdAt": "2026-01-01T00:00:00Z",
                "body": "Closes #109, #110",
                "closingIssuesReferences": [{"number": 109}],
            }
        ]
    )
    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (0, payload, "")
        records = await adapter.list_pull_requests()

    assert len(records) == 1
    pr = records[0]
    assert pr.pr_number == 42
    assert pr.title == "Fix blocked flow"
    assert pr.url == "https://github.com/acme/repo/pull/42"
    assert pr.state == "open"
    assert pr.branch == "agentshore/109-fix-blocked-flow"
    assert pr.issue_number == 109
    assert pr.linked_issue_numbers == (109, 110)
    assert pr.labels == ["changes-requested"]
    assert pr.review_decision == "CHANGES_REQUESTED"
    assert pr.status_check_summary == "failed"
    assert pr.github_author == "octocat"


# ---------------------------------------------------------------------------
# Mutations: dedup via idempotency key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mutation_dedup_on_existing_key(tmp_path: Path) -> None:
    from agentshore.data.store import ExternalMutationRecord

    adapter, mock_store = _make_adapter(tmp_path)
    existing = ExternalMutationRecord(
        session_id="test-session",
        idempotency_key="create_issue:key1",
        mutation_type="create_issue",
        target="My issue",
        status="ok",
        created_at="2024-01-01T00:00:00Z",
    )
    mock_store.get_external_mutation = AsyncMock(return_value=existing)

    result = await adapter.create_issue(
        title="My issue",
        body="body",
        labels=[],
        idempotency_key="key1",
    )
    # Should return None immediately without running gh
    assert result is None


@pytest.mark.asyncio
async def test_create_issue_records_mutation(tmp_path: Path) -> None:
    adapter, mock_store = _make_adapter(tmp_path)
    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (0, '{"number": 1}', "")
        await adapter.create_issue(
            title="New issue",
            body="body",
            labels=["bug"],
            idempotency_key="create-abc",
        )

    # Should have recorded a pre-flight "pending" row then updated to "ok"
    mock_store.record_external_mutation.assert_called_once()
    mock_store.update_external_mutation_status.assert_called_once()
    _, call_kwargs = mock_store.update_external_mutation_status.call_args
    args = mock_store.update_external_mutation_status.call_args.args
    assert args[2] == "ok"


# ---------------------------------------------------------------------------
# ensure_labels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_labels_skips_existing_and_creates_missing(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)
    adapter._available = True

    create_calls: list[str] = []

    async def fake_json_list(cmd: list[str]) -> list[dict[str, str]]:
        assert cmd[0] == "label" and cmd[1] == "list"
        return [{"name": "existing"}]

    async def fake_run_gh(cmd: list[str], timeout: int) -> tuple[int, str, str]:
        assert cmd[0] == "label" and cmd[1] == "create"
        create_calls.append(cmd[2])
        return (0, "", "")

    with (
        patch.object(adapter, "_gh_json_list", side_effect=fake_json_list),
        patch("agentshore.github.adapter._run_gh", side_effect=fake_run_gh),
    ):
        await adapter.ensure_labels(
            [("existing", "ffffff"), ("new-a", "aaaaaa"), ("new-b", "bbbbbb")]
        )

    assert sorted(create_calls) == ["new-a", "new-b"]


@pytest.mark.asyncio
async def test_ensure_labels_creates_concurrently(tmp_path: Path) -> None:
    """All missing labels start before any complete — proves no serial loop."""
    adapter, _ = _make_adapter(tmp_path)
    adapter._available = True

    started = 0
    peak = 0
    release = asyncio.Event()

    async def fake_json_list(cmd: list[str]) -> list[dict[str, str]]:
        return []

    async def fake_run_gh(cmd: list[str], timeout: int) -> tuple[int, str, str]:
        nonlocal started, peak
        started += 1
        peak = max(peak, started)
        await release.wait()
        started -= 1
        return (0, "", "")

    async def run() -> None:
        await asyncio.sleep(0.02)
        release.set()

    with (
        patch.object(adapter, "_gh_json_list", side_effect=fake_json_list),
        patch("agentshore.github.adapter._run_gh", side_effect=fake_run_gh),
    ):
        await asyncio.gather(
            adapter.ensure_labels([(f"l-{i}", "ffffff") for i in range(5)]),
            run(),
        )

    assert peak >= 2, f"expected concurrent label creation, peak in-flight was {peak}"
