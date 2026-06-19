"""Phase 0: beads foundation — ProjectGraph, schema migration, OrchestratorState.graph."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from agentshore.beads import (
    Bead,
    BeadStatus,
    BeadType,
    EpicStatus,
    ProjectGraph,
)
from agentshore.state import OrchestratorState, SessionState


class _VersionRun:
    """Stand-in for ``subprocess.run`` that reports a fixed ``bd --version``.

    Used instead of a real on-disk fake binary so the version-check tests are
    cross-platform — a ``#!/bin/sh`` stub is not a valid Win32 executable
    ([WinError 193]) and cannot be spawned directly on Windows.
    """

    def __init__(self, version: str) -> None:
        self._version = version

    def __call__(self, *_args: object, **_kwargs: object) -> _VersionRun:
        self.stdout = f"bd version {self._version}\n"
        self.stderr = ""
        self.returncode = 0
        return self


# ---------------------------------------------------------------------------
# ProjectGraph dataclass
# ---------------------------------------------------------------------------


def test_project_graph_has_ready_tasks_true() -> None:
    g = ProjectGraph(tasks_ready=3)
    assert g.has_ready_tasks


def test_project_graph_has_ready_tasks_false() -> None:
    g = ProjectGraph(tasks_ready=0)
    assert not g.has_ready_tasks


def test_project_graph_has_epics_true() -> None:
    epic = EpicStatus(
        bead_id="bd-001", title="E1", total_tasks=2, closed_tasks=1, closure_ratio=0.5
    )
    g = ProjectGraph(epics=[epic])
    assert g.has_epics


def test_project_graph_has_epics_false() -> None:
    g = ProjectGraph()
    assert not g.has_epics


def test_project_graph_global_closure_ratio() -> None:
    g = ProjectGraph(tasks_total=10, tasks_ready=3, global_closure_ratio=0.4)
    assert g.global_closure_ratio == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# OrchestratorState.graph field
# ---------------------------------------------------------------------------


def _make_state(graph: ProjectGraph | None = None) -> OrchestratorState:
    return OrchestratorState(
        session_id="test",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        graph=graph,
    )


def test_agentshore_state_graph_defaults_to_none() -> None:
    state = _make_state()
    assert state.graph is None


def test_agentshore_state_graph_can_be_set() -> None:
    graph = ProjectGraph(tasks_ready=5, global_closure_ratio=0.3)
    state = _make_state(graph=graph)
    assert state.graph is graph
    assert state.graph.tasks_ready == 5


# ---------------------------------------------------------------------------
# Bead / BeadType / BeadStatus
# ---------------------------------------------------------------------------


def test_bead_type_str_enum() -> None:
    assert BeadType.EPIC == "epic"
    assert BeadType.TASK == "task"


def test_bead_status_str_enum() -> None:
    assert BeadStatus.OPEN == "open"
    assert BeadStatus.CLOSED == "closed"


def test_bead_construction() -> None:
    b = Bead(
        bead_id="bd-abc",
        title="Add login page",
        bead_type=BeadType.TASK,
        status=BeadStatus.OPEN,
        external_ref="gh-42",
    )
    assert b.external_ref == "gh-42"
    assert b.bead_type == BeadType.TASK


# ---------------------------------------------------------------------------
# load_graph: absent .beads/ returns None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_graph_returns_none_when_no_beads_dir(tmp_path: object) -> None:
    from pathlib import Path

    from agentshore.beads import load_graph

    p = Path(str(tmp_path))
    result = await load_graph(p)
    assert result is None


@pytest.mark.asyncio
async def test_load_graph_raises_graph_read_error_when_bd_command_fails(
    tmp_path: object,
) -> None:
    """load_graph raises GraphReadError after exhausting retries — never returns stale None."""
    from pathlib import Path

    from agentshore.beads import _GRAPH_READ_RETRIES, BdError, GraphReadError, load_graph

    p = Path(str(tmp_path))
    (p / ".beads").mkdir()

    # All retry attempts fail — GraphReadError must be raised, not None returned.
    with (
        patch("agentshore.beads.bd", side_effect=BdError("bd failed")) as mock_bd,
        pytest.raises(GraphReadError),
    ):
        await load_graph(p)
    # Transient BdError is retried the full _GRAPH_READ_RETRIES times.
    assert mock_bd.call_count == _GRAPH_READ_RETRIES


@pytest.mark.asyncio
async def test_load_graph_fails_fast_on_timeout_without_retrying(
    tmp_path: object,
) -> None:
    """A bd timeout raises GraphReadError on the first attempt — no 3x retry (#237).

    Retrying a timeout only re-pays the full timeout budget (the 360s = 3x120s
    pathology), so the graph reader must fail fast on ``BdTimeoutError`` rather
    than treat it as a transient, retry-worthy failure.
    """
    from pathlib import Path

    from agentshore.beads import BdTimeoutError, GraphReadError, load_graph

    p = Path(str(tmp_path))
    (p / ".beads").mkdir()

    with (
        patch("agentshore.beads.bd", side_effect=BdTimeoutError("bd list timed out")) as mock_bd,
        pytest.raises(GraphReadError),
    ):
        await load_graph(p)
    assert mock_bd.call_count == 1


@pytest.mark.asyncio
async def test_load_graph_populates_tasks_and_resolves_epic(tmp_path: object) -> None:
    from pathlib import Path

    from agentshore.beads import _BD_GRAPH_TIMEOUT_SECONDS, load_graph

    p = Path(str(tmp_path))
    (p / ".beads").mkdir()
    all_beads = [
        {"id": "epic-1", "title": "Auth", "type": "epic", "status": "open"},
        {"id": "story-1", "title": "Login story", "type": "story", "parent_id": "epic-1"},
        {
            "id": "task-1",
            "title": "Login issue",
            "type": "task",
            "status": "open",
            "parent_id": "story-1",
            "external_ref": "gh-42",
        },
        {
            "id": "task-2",
            "title": "Closed task",
            "type": "task",
            "status": "closed",
            "parent_id": "epic-1",
        },
    ]

    with patch(
        "agentshore.beads.bd", new_callable=AsyncMock, return_value=json.dumps(all_beads)
    ) as bd:
        result = await load_graph(p)

    assert result is not None
    bd.assert_awaited_once_with(
        "list",
        "--all",
        "--json",
        "--limit",
        "0",
        cwd=p,
        timeout_seconds=_BD_GRAPH_TIMEOUT_SECONDS,
    )
    assert result.tasks_ready == 1
    assert result.tasks_total == 2
    assert result.global_closure_ratio == pytest.approx(0.5)
    assert len(result.tasks) == 2
    assert result.epics[0].total_tasks == 2
    assert result.epics[0].closed_tasks == 1
    assert result.epics[0].closure_ratio == pytest.approx(0.5)
    first = result.tasks[0]
    assert first.issue_number == 42
    assert first.epic_id == "epic-1"
    assert first.epic_title == "Auth"
    assert first.ready is True
    assert result.tasks[1].ready is False


@pytest.mark.asyncio
async def test_load_graph_uses_epic_name_when_title_missing(tmp_path: object) -> None:
    from pathlib import Path

    from agentshore.beads import load_graph

    p = Path(str(tmp_path))
    (p / ".beads").mkdir()
    all_beads = [
        {"id": "epic-1", "title": " ", "name": "Runtime Reliability", "type": "epic"},
        {
            "id": "task-1",
            "title": "Harden retry path",
            "type": "task",
            "status": "open",
            "parent_id": "epic-1",
        },
    ]

    with patch("agentshore.beads.bd", new_callable=AsyncMock, return_value=json.dumps(all_beads)):
        result = await load_graph(p)

    assert result is not None
    assert result.epics[0].title == "Runtime Reliability"
    assert result.tasks[0].epic_title == "Runtime Reliability"


@pytest.mark.asyncio
async def test_load_graph_resolves_parent_child_dependencies(tmp_path: object) -> None:
    from pathlib import Path

    from agentshore.beads import load_graph

    p = Path(str(tmp_path))
    (p / ".beads").mkdir()
    all_beads = [
        {"id": "epic-1", "title": "Render & Cleanup Safety", "type": "epic"},
        {
            "id": "story-1",
            "title": "Cleanup story",
            "type": "story",
            "dependencies": [{"type": "parent-child", "depends_on_id": "epic-1"}],
        },
        {
            "id": "task-1",
            "title": "Validate MP4 before cleanup",
            "type": "task",
            "status": "open",
            "dependencies": [{"type": "parent-child", "depends_on_id": "story-1"}],
        },
    ]

    with patch("agentshore.beads.bd", new_callable=AsyncMock, return_value=json.dumps(all_beads)):
        result = await load_graph(p)

    assert result is not None
    assert result.epics[0].bead_id == "epic-1"
    assert result.epics[0].title == "Render & Cleanup Safety"
    assert result.epics[0].total_tasks == 1
    assert result.epics[0].closed_tasks == 0
    assert result.tasks[0].parent_id == "story-1"
    assert result.tasks[0].epic_title == "Render & Cleanup Safety"


@pytest.mark.asyncio
async def test_load_graph_treats_bd_blocks_edge_as_blocking(tmp_path: object) -> None:
    """A bd ``blocks`` dependency (bd's default link type) blocks the task.

    Regression guard for the parser hardening: bd emits ``blocks`` (not the
    ``depends-on`` string the parser used to look for), so a leaf task with an
    open ``blocks`` edge must parse as NOT ready and surface its blocker, while
    a ``parent-child`` containment edge stays non-blocking (ready).
    """
    from pathlib import Path

    from agentshore.beads import load_graph

    p = Path(str(tmp_path))
    (p / ".beads").mkdir()
    all_beads = [
        {"id": "epic-1", "title": "Epic", "type": "epic", "status": "open"},
        {
            "id": "task-blocked",
            "title": "Blocked by a real dependency",
            "type": "task",
            "status": "open",
            "dependencies": [{"type": "blocks", "depends_on_id": "task-dep"}],
        },
        {"id": "task-dep", "title": "Open blocker", "type": "task", "status": "open"},
        {
            "id": "task-contained",
            "title": "Only a containment edge",
            "type": "task",
            "status": "open",
            "dependencies": [{"type": "parent-child", "depends_on_id": "epic-1"}],
        },
    ]

    with patch("agentshore.beads.bd", new_callable=AsyncMock, return_value=json.dumps(all_beads)):
        result = await load_graph(p)

    assert result is not None
    by_id = {t.bead_id: t for t in result.tasks}
    # `blocks` edge -> blocked, not ready, blocker surfaced
    assert by_id["task-blocked"].ready is False
    assert "task-dep" in by_id["task-blocked"].blocked_by_ids
    # `parent-child` containment -> still ready (non-blocking rollup)
    assert by_id["task-contained"].ready is True
    assert by_id["task-contained"].blocked_by_ids == frozenset()


@pytest.mark.asyncio
async def test_load_graph_falls_back_to_epic_id_when_title_blank(tmp_path: object) -> None:
    from pathlib import Path

    from agentshore.beads import load_graph

    p = Path(str(tmp_path))
    (p / ".beads").mkdir()
    all_beads = [{"id": "epic-1", "title": "   ", "type": "epic", "status": "open"}]

    with patch("agentshore.beads.bd", new_callable=AsyncMock, return_value=json.dumps(all_beads)):
        result = await load_graph(p)

    assert result is not None
    assert result.epics[0].title == "epic-1"


@pytest.mark.asyncio
async def test_load_graph_counts_closed_tasks_missing_from_epic_status(tmp_path: object) -> None:
    from pathlib import Path

    from agentshore.beads import load_graph

    p = Path(str(tmp_path))
    (p / ".beads").mkdir()
    specs = {
        "epic-diag": ("Doctor Command & Diagnostics", 7, 5),
        "epic-render": ("Render & Cleanup Safety", 12, 9),
        "epic-test": ("Test Suite & Local Preflight", 5, 3),
    }
    all_beads: list[dict[str, object]] = []
    for epic_id, (title, total, closed) in specs.items():
        story_id = f"{epic_id}-story"
        all_beads.append({"id": epic_id, "title": title, "type": "epic", "status": "open"})
        all_beads.append(
            {"id": story_id, "title": f"{title} story", "type": "story", "parent_id": epic_id}
        )
        for index in range(total):
            status = "closed" if index < closed else ("open" if index == closed else "in_progress")
            all_beads.append(
                {
                    "id": f"{epic_id}-task-{index}",
                    "title": f"{title} task {index}",
                    "type": "task",
                    "status": status,
                    "parent_id": story_id,
                    "external_ref": f"gh-{index + 1}",
                }
            )

    with patch("agentshore.beads.bd", new_callable=AsyncMock, return_value=json.dumps(all_beads)):
        result = await load_graph(p)

    assert result is not None
    assert result.tasks_total == 24
    assert len(result.tasks) == 24
    assert result.tasks_ready == 3
    assert result.global_closure_ratio == pytest.approx(17 / 24)
    by_title = {epic.title: epic for epic in result.epics}
    assert by_title["Doctor Command & Diagnostics"].closure_ratio == pytest.approx(5 / 7)
    assert by_title["Render & Cleanup Safety"].closure_ratio == pytest.approx(9 / 12)
    assert by_title["Test Suite & Local Preflight"].closure_ratio == pytest.approx(3 / 5)


# ---------------------------------------------------------------------------
# clear_in_progress_beads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_in_progress_beads_returns_zero_without_beads_dir(tmp_path: object) -> None:
    from pathlib import Path

    from agentshore.beads import clear_in_progress_beads

    p = Path(str(tmp_path))

    with patch("agentshore.beads.bd", new_callable=AsyncMock) as bd:
        count = await clear_in_progress_beads(p)

    assert count == 0
    bd.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_in_progress_beads_resets_in_progress_to_open(tmp_path: object) -> None:
    from pathlib import Path

    from agentshore.beads import clear_in_progress_beads

    p = Path(str(tmp_path))
    (p / ".beads").mkdir()
    query_result = [
        {"id": "task-1", "title": "Stale task", "type": "task", "status": "in_progress"},
        {"id": "story-1", "title": "Stale story", "type": "story", "status": "in_progress"},
        {"id": "task-open", "title": "Open task", "type": "task", "status": "open"},
        {"title": "Missing id", "type": "task", "status": "in_progress"},
    ]
    calls: list[tuple[str, ...]] = []

    async def _fake_bd(*args: str, cwd: object, stdin_data: object = None) -> str:
        calls.append(args)
        if args[0] == "query":
            return json.dumps(query_result)
        return ""

    with patch("agentshore.beads.bd", new=_fake_bd):
        count = await clear_in_progress_beads(p)

    assert count == 2
    assert calls[0] == ("query", "status=in_progress", "--json")
    assert ("update", "task-1", "--status", "open", "--dolt-auto-commit=on") in calls
    assert ("update", "story-1", "--status", "open", "--dolt-auto-commit=on") in calls
    assert not any("task-open" in call for call in calls)


# ---------------------------------------------------------------------------
# resolve_bd_binary
# ---------------------------------------------------------------------------


def test_resolve_bd_binary_returns_env_var_when_set(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path

    from agentshore.beads import resolve_bd_binary

    bd_path = Path(str(tmp_path)) / "bd"
    bd_path.write_text("#!/bin/sh\necho bd\n", encoding="utf-8")
    bd_path.chmod(0o755)

    monkeypatch.setenv("AGENTSHORE_BD_BIN", str(bd_path))

    assert resolve_bd_binary() == str(bd_path.resolve())


def test_resolve_bd_binary_ignores_env_var_when_missing_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentshore.beads import resolve_bd_binary

    monkeypatch.setenv("AGENTSHORE_BD_BIN", "/does/not/exist")

    with patch("shutil.which", return_value="/usr/local/bin/bd"):
        assert resolve_bd_binary() == "/usr/local/bin/bd"


def test_resolve_bd_binary_warns_when_env_var_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured AGENTSHORE_BD_BIN should emit a warning before falling back."""
    import structlog

    from agentshore.beads import resolve_bd_binary

    monkeypatch.setenv("AGENTSHORE_BD_BIN", "/does/not/exist")

    with (
        structlog.testing.capture_logs() as captured,
        patch("shutil.which", return_value="/usr/local/bin/bd"),
    ):
        assert resolve_bd_binary() == "/usr/local/bin/bd"

    matching = [e for e in captured if e.get("event") == "agentshore_bd_bin_invalid"]
    assert len(matching) == 1, captured
    assert matching[0]["env_path"] == "/does/not/exist"
    assert matching[0]["log_level"] == "warning"


def test_resolve_bd_binary_does_not_warn_when_env_var_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset AGENTSHORE_BD_BIN is the common CLI case — no warning should fire."""
    import structlog

    from agentshore.beads import resolve_bd_binary

    monkeypatch.delenv("AGENTSHORE_BD_BIN", raising=False)

    with (
        structlog.testing.capture_logs() as captured,
        patch("shutil.which", return_value="/usr/local/bin/bd"),
    ):
        assert resolve_bd_binary() == "/usr/local/bin/bd"

    assert [e for e in captured if e.get("event") == "agentshore_bd_bin_invalid"] == []


def test_resolve_bd_binary_falls_back_to_path_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentshore.beads import resolve_bd_binary

    monkeypatch.delenv("AGENTSHORE_BD_BIN", raising=False)

    with patch("shutil.which", return_value="/usr/local/bin/bd"):
        assert resolve_bd_binary() == "/usr/local/bin/bd"


def test_resolve_bd_binary_returns_none_when_nothing_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentshore.beads import resolve_bd_binary

    monkeypatch.delenv("AGENTSHORE_BD_BIN", raising=False)

    with patch("shutil.which", return_value=None):
        assert resolve_bd_binary() is None


# ---------------------------------------------------------------------------
# ensure_bd_installed
# ---------------------------------------------------------------------------


def test_ensure_bd_installed_passes_when_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentshore.beads.setup import ensure_bd_installed

    # Presence-only assertion: disable the version pin (empty override) so the
    # fake path doesn't need to be an executable that reports a version.
    monkeypatch.setenv("AGENTSHORE_BD_VERSION", "")
    with patch("shutil.which", return_value="/usr/local/bin/bd"):
        ensure_bd_installed()  # must not raise


def test_ensure_bd_installed_raises_when_missing() -> None:
    from agentshore.beads.setup import ensure_bd_installed

    with (
        patch("shutil.which", return_value=None),
        pytest.raises(RuntimeError, match="bd.*not found"),
    ):
        ensure_bd_installed()


def test_ensure_bd_installed_accepts_env_var(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    from agentshore.beads.setup import REQUIRED_BD_VERSION, ensure_bd_installed

    bd_path = Path(str(tmp_path)) / "bd"
    bd_path.write_text("stub", encoding="utf-8")
    bd_path.chmod(0o755)
    monkeypatch.setenv("AGENTSHORE_BD_BIN", str(bd_path))
    monkeypatch.delenv("AGENTSHORE_BD_VERSION", raising=False)

    # Stub reports the pinned version so the version check passes too.
    with (
        patch("shutil.which", return_value=None),
        patch("agentshore.beads.setup.subprocess.run", new=_VersionRun(REQUIRED_BD_VERSION)),
    ):
        ensure_bd_installed()


def test_ensure_bd_installed_rejects_version_mismatch(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    from agentshore.beads.setup import ensure_bd_installed

    bd_path = Path(str(tmp_path)) / "bd"
    bd_path.write_text("stub", encoding="utf-8")
    bd_path.chmod(0o755)
    monkeypatch.setenv("AGENTSHORE_BD_BIN", str(bd_path))
    monkeypatch.delenv("AGENTSHORE_BD_VERSION", raising=False)

    with (
        patch("shutil.which", return_value=None),
        patch("agentshore.beads.setup.subprocess.run", new=_VersionRun("0.9.0")),
        pytest.raises(RuntimeError, match="does not match"),
    ):
        ensure_bd_installed()


def test_ensure_bd_installed_version_override(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    from agentshore.beads.setup import ensure_bd_installed

    bd_path = Path(str(tmp_path)) / "bd"
    bd_path.write_text("stub", encoding="utf-8")
    bd_path.chmod(0o755)
    monkeypatch.setenv("AGENTSHORE_BD_BIN", str(bd_path))
    # Explicit override to the stub's version makes the check pass.
    monkeypatch.setenv("AGENTSHORE_BD_VERSION", "0.9.0")

    with (
        patch("shutil.which", return_value=None),
        patch("agentshore.beads.setup.subprocess.run", new=_VersionRun("0.9.0")),
    ):
        ensure_bd_installed()


def test_ensure_bd_installed_error_mentions_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentshore.beads.setup import ensure_bd_installed

    monkeypatch.delenv("AGENTSHORE_BD_BIN", raising=False)
    with (
        patch("shutil.which", return_value=None),
        pytest.raises(RuntimeError, match="AGENTSHORE_BD_BIN"),
    ):
        ensure_bd_installed()
