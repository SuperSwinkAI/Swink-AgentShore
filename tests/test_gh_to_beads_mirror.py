"""Tests for _mirror_issues_to_beads — Track 6 of Phase 1."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agentshore.beads import BeadStatus, EpicStatus, GraphTask, ProjectGraph
from agentshore.core.phases import _mirror_issues_to_beads
from agentshore.data.models import GitHubIssueRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_issue(
    issue_number: int,
    title: str,
    state: str = "open",
    session_id: str = "sess-001",
    created_at: str = "2026-01-01T00:00:00Z",
) -> GitHubIssueRecord:
    return GitHubIssueRecord(
        issue_number=issue_number,
        session_id=session_id,
        title=title,
        state=state,
        created_at=created_at,
    )


def _graph_with_epics(tasks: list[GraphTask] | None = None) -> ProjectGraph:
    """Return a ProjectGraph that has at least one epic (active mirror path)."""
    return ProjectGraph(
        epics=[
            EpicStatus(
                bead_id="epic-1",
                title="Core Features",
                total_tasks=0,
                closed_tasks=0,
                closure_ratio=0.0,
            )
        ],
        tasks=tasks or [],
        tasks_ready=0,
        tasks_total=0,
        global_closure_ratio=0.0,
    )


# ---------------------------------------------------------------------------
# No-op when .beads/ does not exist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mirror_noop_when_no_beads_dir(tmp_path: Path) -> None:
    """_mirror_issues_to_beads is a no-op when .beads/ is absent."""
    issues = [_make_issue(1, "Implement login")]
    with patch("agentshore.beads.bd", new_callable=AsyncMock) as mock_bd:
        await _mirror_issues_to_beads(project_path=tmp_path, issues=issues)
    mock_bd.assert_not_called()


# ---------------------------------------------------------------------------
# No-op when graph has no epics (C4 guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mirror_noop_when_graph_is_none(tmp_path: Path) -> None:
    """_mirror_issues_to_beads is a no-op when graph is None (pre-seed state)."""
    beads_dir = tmp_path / ".beads"
    beads_dir.mkdir()

    issues = [_make_issue(1, "Add authentication")]

    with patch("agentshore.beads.bd", new_callable=AsyncMock) as mock_bd:
        await _mirror_issues_to_beads(project_path=tmp_path, issues=issues, graph=None)

    mock_bd.assert_not_called()


@pytest.mark.asyncio
async def test_mirror_noop_when_graph_has_no_epics(tmp_path: Path) -> None:
    """_mirror_issues_to_beads is a no-op when the graph exists but has no epics.

    This is the pre-seed state — seed_project will build the canonical graph.
    Importing orphan tasks before the hierarchy exists produces floating
    'beads-only' cards that duplicate what seed_project creates.
    """
    beads_dir = tmp_path / ".beads"
    beads_dir.mkdir()

    empty_graph = ProjectGraph()  # no epics
    issues = [_make_issue(1, "Add authentication"), _make_issue(2, "Fix styling")]

    with patch("agentshore.beads.bd", new_callable=AsyncMock) as mock_bd:
        await _mirror_issues_to_beads(project_path=tmp_path, issues=issues, graph=empty_graph)

    mock_bd.assert_not_called()


@pytest.mark.asyncio
async def test_mirror_noop_when_graph_omitted_defaults_to_no_epics(tmp_path: Path) -> None:
    """Calling without graph= defaults to None → no-op even with .beads/ present."""
    beads_dir = tmp_path / ".beads"
    beads_dir.mkdir()

    issues = [_make_issue(1, "Some issue")]

    with patch("agentshore.beads.bd", new_callable=AsyncMock) as mock_bd:
        await _mirror_issues_to_beads(project_path=tmp_path, issues=issues)

    mock_bd.assert_not_called()


# ---------------------------------------------------------------------------
# bd import --dedup called for each open issue (when epics exist)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mirror_calls_bd_import_for_open_issues(tmp_path: Path) -> None:
    """Each open issue triggers a bd import --dedup call when epics are present."""
    beads_dir = tmp_path / ".beads"
    beads_dir.mkdir()

    issues = [
        _make_issue(1, "Add authentication"),
        _make_issue(2, "Fix navbar styling"),
    ]

    with patch("agentshore.beads.bd", new_callable=AsyncMock) as mock_bd:
        await _mirror_issues_to_beads(
            project_path=tmp_path, issues=issues, graph=_graph_with_epics()
        )

    assert mock_bd.call_count == 2

    # Verify call shape: bd("import", "--dedup", "-", cwd=..., stdin_data=...)
    for c in mock_bd.call_args_list:
        args, kwargs = c
        assert args[0] == "import"
        assert args[1] == "--dedup"
        assert args[2] == "-"
        assert kwargs["cwd"] == tmp_path
        assert "stdin_data" in kwargs

    # Verify JSON payloads
    payloads = [json.loads(c.kwargs["stdin_data"].decode().strip()) for c in mock_bd.call_args_list]
    assert payloads[0] == {"title": "Add authentication", "type": "task", "external_ref": "gh-1"}
    assert payloads[1] == {"title": "Fix navbar styling", "type": "task", "external_ref": "gh-2"}


# ---------------------------------------------------------------------------
# Already-tracked issues are skipped (external_ref dedup pre-check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mirror_skips_already_tracked_issues(tmp_path: Path) -> None:
    """Issues whose external_ref already exists in graph.tasks are not re-imported."""
    beads_dir = tmp_path / ".beads"
    beads_dir.mkdir()

    existing_task = GraphTask(
        bead_id="task-99",
        title="Add authentication",
        status=BeadStatus.OPEN,
        external_ref="gh-1",
    )
    graph = _graph_with_epics(tasks=[existing_task])

    issues = [
        _make_issue(1, "Add authentication"),  # already tracked
        _make_issue(2, "Fix navbar styling"),  # new — should be imported
    ]

    with patch("agentshore.beads.bd", new_callable=AsyncMock) as mock_bd:
        await _mirror_issues_to_beads(project_path=tmp_path, issues=issues, graph=graph)

    # Only issue 2 should be imported
    assert mock_bd.call_count == 1
    payload = json.loads(mock_bd.call_args.kwargs["stdin_data"].decode().strip())
    assert payload["external_ref"] == "gh-2"


# ---------------------------------------------------------------------------
# Closed issues are skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mirror_skips_closed_issues(tmp_path: Path) -> None:
    """Closed issues (state != 'open') are not mirrored."""
    beads_dir = tmp_path / ".beads"
    beads_dir.mkdir()

    issues = [
        _make_issue(1, "Open task", state="open"),
        _make_issue(2, "Closed task", state="closed"),
        _make_issue(3, "Another closed", state="closed"),
    ]

    with patch("agentshore.beads.bd", new_callable=AsyncMock) as mock_bd:
        await _mirror_issues_to_beads(
            project_path=tmp_path, issues=issues, graph=_graph_with_epics()
        )

    assert mock_bd.call_count == 1
    payload = json.loads(mock_bd.call_args.kwargs["stdin_data"].decode().strip())
    assert payload["external_ref"] == "gh-1"


# ---------------------------------------------------------------------------
# BdError is swallowed (genuine failures)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mirror_swallows_bd_error(tmp_path: Path) -> None:
    """Genuine BdError raised by bd is caught and does not propagate."""
    from agentshore.beads import BdError

    beads_dir = tmp_path / ".beads"
    beads_dir.mkdir()

    issues = [
        _make_issue(1, "First issue"),
        _make_issue(2, "Second issue"),
    ]

    # First call raises BdError, second succeeds
    with patch(
        "agentshore.beads.bd",
        new_callable=AsyncMock,
        side_effect=[BdError("bd import failed"), None],
    ) as mock_bd:
        # Must not raise
        await _mirror_issues_to_beads(
            project_path=tmp_path, issues=issues, graph=_graph_with_epics()
        )

    # Both were attempted despite the error on the first
    assert mock_bd.call_count == 2


# ---------------------------------------------------------------------------
# "nothing to commit" is treated as a no-op (issue #92)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mirror_treats_nothing_to_commit_as_noop(tmp_path: Path) -> None:
    """bd import --dedup returning 'nothing to commit' must not emit beads_mirror_issue_failed.

    Dolt exits 1 with 'Error: commit: dolt commit: Error 1105: nothing to commit' when the
    import is already fully deduplicated. This is idempotent success, not a failure.
    """
    from agentshore.beads import BdError

    beads_dir = tmp_path / ".beads"
    beads_dir.mkdir()

    nothing_to_commit_error = BdError(
        "bd import --dedup - failed (rc=1): "
        "Error: commit: dolt commit: Error 1105: nothing to commit"
    )

    issues = [_make_issue(1, "Already imported issue")]

    warning_events: list[str] = []

    with (
        patch(
            "agentshore.beads.bd",
            new_callable=AsyncMock,
            side_effect=nothing_to_commit_error,
        ),
        patch("agentshore.core.phases._logger") as mock_logger,
    ):
        mock_logger.warning.side_effect = lambda ev, **kw: warning_events.append(ev)
        await _mirror_issues_to_beads(
            project_path=tmp_path, issues=issues, graph=_graph_with_epics()
        )

    # beads_mirror_issue_failed must NOT have been emitted.
    assert "beads_mirror_issue_failed" not in warning_events, (
        f"Expected no beads_mirror_issue_failed for nothing-to-commit; got: {warning_events}"
    )


@pytest.mark.asyncio
async def test_mirror_emits_failure_for_genuine_bd_error(tmp_path: Path) -> None:
    """Genuine BdError (not 'nothing to commit') must still emit beads_mirror_issue_failed."""
    from agentshore.beads import BdError

    beads_dir = tmp_path / ".beads"
    beads_dir.mkdir()

    genuine_error = BdError("bd import --dedup - failed (rc=1): permission denied")

    issues = [_make_issue(1, "Some issue")]

    warning_events: list[str] = []

    with (
        patch(
            "agentshore.beads.bd",
            new_callable=AsyncMock,
            side_effect=genuine_error,
        ),
        patch("agentshore.core.phases._logger") as mock_logger,
    ):
        mock_logger.warning.side_effect = lambda ev, **kw: warning_events.append(ev)
        await _mirror_issues_to_beads(
            project_path=tmp_path, issues=issues, graph=_graph_with_epics()
        )

    assert "beads_mirror_issue_failed" in warning_events, (
        f"Expected beads_mirror_issue_failed for genuine error; got: {warning_events}"
    )


@pytest.mark.asyncio
async def test_mirror_empty_issues_list(tmp_path: Path) -> None:
    """Empty issues list results in no bd calls even when .beads/ exists."""
    beads_dir = tmp_path / ".beads"
    beads_dir.mkdir()

    with patch("agentshore.beads.bd", new_callable=AsyncMock) as mock_bd:
        await _mirror_issues_to_beads(project_path=tmp_path, issues=[], graph=_graph_with_epics())

    mock_bd.assert_not_called()
