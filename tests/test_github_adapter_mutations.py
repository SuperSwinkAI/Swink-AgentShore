"""Tests for GitHubAdapter mutating methods (create_issue, label_issue,
close_issue, create_pr) and probe() error branches.

Companion to ``tests/test_github_adapter.py`` which focuses on read paths.
All `gh` invocations are mocked — no subprocess is ever spawned.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.config import RuntimeConfig
from agentshore.data.store import ExternalMutationRecord
from agentshore.github import GitHubAdapter

# Fixture helpers duplicated (not shared via conftest) to avoid import-time coupling.


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


@pytest.mark.asyncio
async def test_probe_handles_unexpected_exception(tmp_path: Path) -> None:
    """A non-FileNotFoundError OS-level error raised by the gh invocation must be
    swallowed and downgrade the adapter to unavailable, never raise."""
    adapter, _ = _make_adapter(tmp_path)
    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.side_effect = OSError("permission denied")
        await adapter.probe()
    assert adapter.available is False
    assert adapter.probe_failure_reason == "probe error: permission denied"


@pytest.mark.asyncio
async def test_probe_nonzero_exit_captures_stderr(tmp_path: Path) -> None:
    """#369: a non-zero `gh auth status` must preserve the stderr tail so a
    github.com 503 is distinguishable from a real credential problem."""
    adapter, _ = _make_adapter(tmp_path)
    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (1, "", "HTTP 503: Service Unavailable\n")
        await adapter.probe()
    assert adapter.available is False
    assert adapter.probe_failure_reason == "gh auth status exited 1: HTTP 503: Service Unavailable"


@pytest.mark.asyncio
async def test_probe_success_leaves_no_failure_reason(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)
    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (0, "Logged in to github.com", "")
        await adapter.probe()
    assert adapter.available is True
    assert adapter.probe_failure_reason is None


@pytest.mark.asyncio
async def test_create_issue_runs_gh_with_title_body_labels(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)

    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (0, '{"number": 7}', "")
        result = await adapter.create_issue(
            title="My new issue",
            body="Body text",
            labels=["bug", "p1"],
            idempotency_key="ci-1",
        )

    assert result == {"number": 7}
    # gh argv is the first positional arg (a list, no leading "gh").
    rest = list(run_gh.call_args.args[0])
    assert rest[0:2] == ["issue", "create"]
    assert "--title" in rest and "My new issue" in rest
    assert "--body" in rest and "Body text" in rest
    # Each label must appear after its own --label flag.
    label_indices = [i for i, a in enumerate(rest) if a == "--label"]
    label_values = [rest[i + 1] for i in label_indices]
    assert sorted(label_values) == ["bug", "p1"]


@pytest.mark.asyncio
async def test_create_issue_returns_none_on_gh_failure(tmp_path: Path) -> None:
    """gh exits non-zero → None and the mutation row gets marked 'error'."""
    adapter, mock_store = _make_adapter(tmp_path)

    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (1, "", "permission denied")
        result = await adapter.create_issue(
            title="boom",
            body="",
            labels=[],
            idempotency_key="ci-fail",
        )

    assert result is None
    update_args = mock_store.update_external_mutation_status.call_args.args
    assert update_args[2] == "error"
    assert "permission denied" in update_args[3]


@pytest.mark.asyncio
async def test_create_issue_returns_none_when_unavailable(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)
    adapter._available = False
    result = await adapter.create_issue(title="x", body="", labels=[], idempotency_key="ci-na")
    assert result is None


@pytest.mark.asyncio
async def test_label_issue_runs_issue_edit_with_add_label(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)

    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (0, "ok", "")
        ok = await adapter.label_issue(
            issue_number=42,
            labels=["agentshore/active", "bug"],
            idempotency_key="li-1",
        )

    assert ok is True
    rest = list(run_gh.call_args.args[0])
    assert rest[0:3] == ["issue", "edit", "42"]
    add_label_idx = rest.index("--add-label")
    # gh accepts a comma-joined label list for --add-label.
    assert rest[add_label_idx + 1] == "agentshore/active,bug"


@pytest.mark.asyncio
async def test_label_issue_returns_false_on_gh_error(tmp_path: Path) -> None:
    adapter, mock_store = _make_adapter(tmp_path)

    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (1, "", "not found")
        ok = await adapter.label_issue(
            issue_number=99,
            labels=["nope"],
            idempotency_key="li-err",
        )

    assert ok is False
    update_args = mock_store.update_external_mutation_status.call_args.args
    assert update_args[2] == "error"


@pytest.mark.asyncio
async def test_label_issue_returns_false_when_unavailable(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)
    adapter._available = False
    ok = await adapter.label_issue(issue_number=1, labels=["x"], idempotency_key="li-na")
    assert ok is False


@pytest.mark.asyncio
async def test_label_issue_dedups_when_mutation_already_recorded(
    tmp_path: Path,
) -> None:
    """An idempotency key that already resolved 'ok' must short-circuit
    without re-invoking gh."""
    adapter, mock_store = _make_adapter(tmp_path)
    mock_store.get_external_mutation = AsyncMock(
        return_value=ExternalMutationRecord(
            session_id="test-session",
            idempotency_key="label_issue:dup",
            mutation_type="label_issue",
            target="42",
            status="ok",
            created_at="2024-01-01T00:00:00Z",
        )
    )

    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        ok = await adapter.label_issue(issue_number=42, labels=["x"], idempotency_key="dup")

    assert ok is True
    run_gh.assert_not_called()


@pytest.mark.asyncio
async def test_comment_issue_runs_issue_comment(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)

    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (0, "https://github.com/o/r/issues/7#c1", "")
        ok = await adapter.comment_issue(
            issue_number=7,
            body="<!-- agentshore:blocked-by #12 -->",
            idempotency_key="cm-1",
        )

    assert ok is True
    rest = list(run_gh.call_args.args[0])
    assert rest[0:3] == ["issue", "comment", "7"]
    body_idx = rest.index("--body")
    assert rest[body_idx + 1] == "<!-- agentshore:blocked-by #12 -->"


@pytest.mark.asyncio
async def test_comment_issue_returns_false_on_empty_body(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)
    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        ok = await adapter.comment_issue(issue_number=7, body="", idempotency_key="cm-empty")
    assert ok is False
    run_gh.assert_not_called()


@pytest.mark.asyncio
async def test_comment_issue_returns_false_when_unavailable(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)
    adapter._available = False
    ok = await adapter.comment_issue(issue_number=1, body="hi", idempotency_key="cm-na")
    assert ok is False


@pytest.mark.asyncio
async def test_close_issue_runs_issue_close(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)

    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (0, "closed", "")
        ok = await adapter.close_issue(issue_number=15, idempotency_key="cl-1")

    assert ok is True
    rest = list(run_gh.call_args.args[0])
    assert rest == ["issue", "close", "15"]


@pytest.mark.asyncio
async def test_close_issue_returns_false_on_gh_failure(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)

    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (1, "", "not found")
        ok = await adapter.close_issue(issue_number=404, idempotency_key="cl-err")

    assert ok is False


@pytest.mark.asyncio
async def test_close_issue_returns_false_when_unavailable(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)
    adapter._available = False
    ok = await adapter.close_issue(issue_number=1, idempotency_key="cl-na")
    assert ok is False


@pytest.mark.asyncio
async def test_close_issue_dedups_when_already_recorded(tmp_path: Path) -> None:
    """A repeat close_issue call with the same key short-circuits."""
    adapter, mock_store = _make_adapter(tmp_path)
    mock_store.get_external_mutation = AsyncMock(
        return_value=ExternalMutationRecord(
            session_id="test-session",
            idempotency_key="close_issue:dup",
            mutation_type="close_issue",
            target="42",
            status="ok",
            created_at="2024-01-01T00:00:00Z",
        )
    )

    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        ok = await adapter.close_issue(issue_number=42, idempotency_key="dup")

    assert ok is True
    run_gh.assert_not_called()


@pytest.mark.asyncio
async def test_create_pr_runs_gh_with_head_and_base(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)

    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (0, '{"url": "https://github.com/x/y/pull/9"}', "")
        result = await adapter.create_pr(
            title="Add feature",
            body="Closes #1",
            head="feature-branch",
            base="main",
            idempotency_key="pr-1",
        )

    assert result == {"url": "https://github.com/x/y/pull/9"}
    rest = list(run_gh.call_args.args[0])
    assert rest[0:2] == ["pr", "create"]
    assert "--head" in rest and "feature-branch" in rest
    assert "--base" in rest and "main" in rest
    assert "--title" in rest and "Add feature" in rest


@pytest.mark.asyncio
async def test_create_pr_returns_none_on_gh_failure(tmp_path: Path) -> None:
    adapter, mock_store = _make_adapter(tmp_path)

    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (1, "", "branch not pushed")
        result = await adapter.create_pr(
            title="x",
            body="x",
            head="missing",
            base="main",
            idempotency_key="pr-err",
        )

    assert result is None
    update_args = mock_store.update_external_mutation_status.call_args.args
    assert update_args[2] == "error"


@pytest.mark.asyncio
async def test_create_pr_returns_none_when_unavailable(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)
    adapter._available = False
    result = await adapter.create_pr(
        title="x", body="", head="x", base="main", idempotency_key="pr-na"
    )
    assert result is None


@pytest.mark.asyncio
async def test_create_pr_dedups_when_already_recorded(tmp_path: Path) -> None:
    """A repeat create_pr call with the same key short-circuits with None."""
    adapter, mock_store = _make_adapter(tmp_path)
    mock_store.get_external_mutation = AsyncMock(
        return_value=ExternalMutationRecord(
            session_id="test-session",
            idempotency_key="create_pr:dup",
            mutation_type="create_pr",
            target="my-branch",
            status="ok",
            created_at="2024-01-01T00:00:00Z",
        )
    )

    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        result = await adapter.create_pr(
            title="x",
            body="",
            head="my-branch",
            base="main",
            idempotency_key="dup",
        )

    assert result is None
    run_gh.assert_not_called()


# retarget_pr_base — #8: self-heal wrong-base PRs


@pytest.mark.asyncio
async def test_retarget_pr_base_runs_gh_pr_edit_with_base(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)

    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        run_gh.return_value = (0, "https://example/pr/9", "")
        ok = await adapter.retarget_pr_base(pr_number=9, base="integration", idempotency_key="r-1")

    assert ok is True
    rest = list(run_gh.call_args.args[0])
    assert rest[0:3] == ["pr", "edit", "9"]
    assert "--base" in rest
    assert "integration" in rest


@pytest.mark.asyncio
async def test_retarget_pr_base_noop_on_empty_base(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(tmp_path)
    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        ok = await adapter.retarget_pr_base(pr_number=9, base="", idempotency_key="r-empty")
    assert ok is False
    run_gh.assert_not_called()


@pytest.mark.asyncio
async def test_retarget_pr_base_dedups_when_already_recorded(tmp_path: Path) -> None:
    adapter, mock_store = _make_adapter(tmp_path)
    mock_store.get_external_mutation = AsyncMock(
        return_value=ExternalMutationRecord(
            session_id="test-session",
            idempotency_key="retarget_pr_base:dup",
            mutation_type="retarget_pr_base",
            target="9",
            status="ok",
            created_at="2024-01-01T00:00:00Z",
        )
    )
    with patch("agentshore.github.adapter._run_gh", new_callable=AsyncMock) as run_gh:
        ok = await adapter.retarget_pr_base(pr_number=9, base="integration", idempotency_key="dup")
    assert ok is True
    run_gh.assert_not_called()
