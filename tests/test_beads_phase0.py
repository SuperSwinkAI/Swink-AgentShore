"""Phase 0: beads foundation — ProjectGraph, schema migration, OrchestratorState.graph."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
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
async def test_load_graph_returns_none_when_bd_command_fails(tmp_path: object) -> None:
    """load_graph distinguishes command failures from a valid empty graph."""
    from pathlib import Path

    from agentshore.beads import load_graph

    p = Path(str(tmp_path))
    (p / ".beads").mkdir()

    from agentshore.beads import BdError

    with patch("agentshore.beads.bd", side_effect=BdError("bd failed")):
        result = await load_graph(p)

    assert result is None


@pytest.mark.asyncio
async def test_load_graph_retries_empty_stdout_then_returns_none(tmp_path: object) -> None:
    """Empty stdout (contended Dolt read) is retried, then yields None — not an
    empty graph — when no populated graph was ever cached.

    ``bd list --json`` always prints at least ``[]`` on a real read, so empty
    stdout signals a lock-contended read (Windows mandatory file locks), never a
    truthful empty store. Treating it as empty would flip ``has_epics`` off and
    re-trigger seed_project.
    """
    from pathlib import Path

    import agentshore.beads as beads_mod
    from agentshore.beads import load_graph

    beads_mod._reset_graph_cache()
    p = Path(str(tmp_path))
    (p / ".beads").mkdir()

    bd = AsyncMock(return_value="   ")  # whitespace-only == empty
    with (
        patch("agentshore.beads.bd", bd),
        patch("agentshore.beads.asyncio.sleep", new_callable=AsyncMock) as sleep,
    ):
        result = await load_graph(p)

    assert result is None  # NOT an empty ProjectGraph
    # One initial read + the full retry budget.
    assert bd.await_count == beads_mod._BD_EMPTY_READ_RETRIES + 1
    assert sleep.await_count == beads_mod._BD_EMPTY_READ_RETRIES


@pytest.mark.asyncio
async def test_load_graph_empty_read_reuses_last_good_graph(tmp_path: object) -> None:
    """A contended empty read after a populated load reuses the cached graph,
    so a momentary lock contention never downgrades a seeded project."""
    from pathlib import Path

    import agentshore.beads as beads_mod
    from agentshore.beads import load_graph

    beads_mod._reset_graph_cache()
    p = Path(str(tmp_path))
    (p / ".beads").mkdir()
    populated = [
        {"id": "epic-1", "title": "Auth", "type": "epic", "status": "open"},
        {
            "id": "task-1",
            "title": "Login",
            "type": "task",
            "status": "open",
            "parent_id": "epic-1",
        },
    ]

    with patch("agentshore.beads.bd", new_callable=AsyncMock, return_value=json.dumps(populated)):
        good = await load_graph(p)
    assert good is not None and good.has_epics

    # Next tick: bd returns empty stdout on every attempt (full contention).
    with (
        patch("agentshore.beads.bd", new_callable=AsyncMock, return_value=""),
        patch("agentshore.beads.asyncio.sleep", new_callable=AsyncMock),
    ):
        reused = await load_graph(p)

    assert reused is good  # exact cached instance, not a fresh empty graph
    assert reused.has_epics


@pytest.mark.asyncio
async def test_load_graph_transient_bderror_reuses_last_good_graph(tmp_path: object) -> None:
    """A transient BdError after a populated load reuses the cached graph too."""
    from pathlib import Path

    import agentshore.beads as beads_mod
    from agentshore.beads import BdError, load_graph

    beads_mod._reset_graph_cache()
    p = Path(str(tmp_path))
    (p / ".beads").mkdir()
    populated = [{"id": "epic-1", "title": "Auth", "type": "epic", "status": "open"}]

    with patch("agentshore.beads.bd", new_callable=AsyncMock, return_value=json.dumps(populated)):
        good = await load_graph(p)
    assert good is not None and good.has_epics

    with patch("agentshore.beads.bd", side_effect=BdError("dolt lock held")):
        reused = await load_graph(p)

    assert reused is good


@pytest.mark.asyncio
async def test_load_graph_valid_empty_list_is_truthful_empty_graph(tmp_path: object) -> None:
    """A valid empty ``[]`` response is a real empty store: an empty graph (not
    None), and it is NOT cached so it never masks a later real read."""
    from pathlib import Path

    import agentshore.beads as beads_mod
    from agentshore.beads import load_graph

    beads_mod._reset_graph_cache()
    p = Path(str(tmp_path))
    (p / ".beads").mkdir()

    with patch("agentshore.beads.bd", new_callable=AsyncMock, return_value="[]"):
        result = await load_graph(p)

    assert result is not None
    assert not result.has_epics
    assert result.tasks_total == 0
    key = os.path.normcase(os.path.normpath(str(p.resolve())))
    assert key not in beads_mod._LAST_GOOD_GRAPH


@pytest.mark.asyncio
async def test_load_graph_valid_empty_clears_stale_last_good_cache(tmp_path: object) -> None:
    """A genuine wipe (valid ``[]`` after a populated load) both reports empty
    now AND drops the stale cache, so a later contended read can't resurrect the
    wiped graph."""
    from pathlib import Path

    import agentshore.beads as beads_mod
    from agentshore.beads import load_graph

    beads_mod._reset_graph_cache()
    p = Path(str(tmp_path))
    (p / ".beads").mkdir()
    key = os.path.normcase(os.path.normpath(str(p.resolve())))

    populated = [{"id": "epic-1", "title": "Auth", "type": "epic", "status": "open"}]
    with patch("agentshore.beads.bd", new_callable=AsyncMock, return_value=json.dumps(populated)):
        await load_graph(p)
    assert key in beads_mod._LAST_GOOD_GRAPH  # cached while populated

    # Genuine wipe: bd now reports a valid empty store.
    with patch("agentshore.beads.bd", new_callable=AsyncMock, return_value="[]"):
        empty = await load_graph(p)
    assert empty is not None and not empty.has_epics
    assert key not in beads_mod._LAST_GOOD_GRAPH  # stale entry cleared

    # A subsequent contended read must NOT resurrect the wiped graph.
    with (
        patch("agentshore.beads.bd", new_callable=AsyncMock, return_value=""),
        patch("agentshore.beads.asyncio.sleep", new_callable=AsyncMock),
    ):
        after_wipe = await load_graph(p)
    assert after_wipe is None


@pytest.mark.asyncio
async def test_load_graph_populates_tasks_and_resolves_epic(tmp_path: object) -> None:
    from pathlib import Path

    from agentshore.beads import load_graph

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
    bd.assert_awaited_once_with("list", "--all", "--json", "--limit", "0", cwd=p)
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
    from pathlib import Path

    from agentshore.beads import resolve_bd_binary

    monkeypatch.delenv("AGENTSHORE_BD_BIN", raising=False)

    with (
        patch("shutil.which", return_value=None),
        patch("agentshore.beads._managed_bd_path", return_value=Path("/does/not/exist/bd")),
    ):
        assert resolve_bd_binary() is None


def test_resolve_bd_binary_uses_managed_dir_when_path_empty(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With env unset and bd off PATH, the init-managed dir is the last resort."""
    import sys
    from pathlib import Path

    import agentshore.beads as beads_mod
    from agentshore.beads import resolve_bd_binary

    managed = Path(str(tmp_path)) / ("bd.exe" if sys.platform.startswith("win") else "bd")
    managed.write_text("#!/bin/sh\necho bd\n", encoding="utf-8")
    managed.chmod(0o755)

    monkeypatch.delenv("AGENTSHORE_BD_BIN", raising=False)
    monkeypatch.setattr(beads_mod, "_managed_bd_path", lambda: managed)
    with patch("shutil.which", return_value=None):
        assert resolve_bd_binary() == str(managed.resolve())


# ---------------------------------------------------------------------------
# managed_bd_dir / ensure_bd_dir_on_path — install to the canonical beads
# location and make it resolvable for agent subprocesses.
# ---------------------------------------------------------------------------


def test_managed_bd_dir_is_canonical_beads_location(monkeypatch: pytest.MonkeyPatch) -> None:
    """managed_bd_dir mirrors beads' own install.ps1 / install.sh targets so a
    provisioned bd is a normal, on-PATH beads install (not a swink-private dir)."""
    import agentshore.beads as beads_mod

    # Windows → %LOCALAPPDATA%\Programs\bd (matches install.ps1).
    monkeypatch.setattr(beads_mod.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\tester\AppData\Local")
    win_dir = beads_mod.managed_bd_dir()
    assert win_dir.parts[-2:] == ("Programs", "bd")
    assert "AppData" in str(win_dir)

    # POSIX with /usr/local/bin writable → that (matches install.sh first choice).
    monkeypatch.setattr(beads_mod.sys, "platform", "linux")
    with patch("agentshore.beads.os.access", return_value=True):
        assert beads_mod.managed_bd_dir() == Path("/usr/local/bin")

    # POSIX with /usr/local/bin not writable → ~/.local/bin fallback.
    with patch("agentshore.beads.os.access", return_value=False):
        assert beads_mod.managed_bd_dir() == Path.home() / ".local" / "bin"


def test_ensure_bd_dir_on_path_prepends_existing_dir(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the install dir exists and is off PATH, it is prepended for this
    process (so inheriting agent subprocesses resolve a bare ``bd``)."""
    from pathlib import Path

    import agentshore.beads as beads_mod

    bd_dir = Path(str(tmp_path)) / "Programs" / "bd"
    bd_dir.mkdir(parents=True)
    monkeypatch.setattr(beads_mod, "managed_bd_dir", lambda: bd_dir)
    monkeypatch.setenv("PATH", "/already/here")

    beads_mod.ensure_bd_dir_on_path()

    parts = os.environ["PATH"].split(os.pathsep)
    assert parts[0] == str(bd_dir)
    assert "/already/here" in parts


def test_ensure_bd_dir_on_path_idempotent(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second call does not duplicate the entry."""
    from pathlib import Path

    import agentshore.beads as beads_mod

    bd_dir = Path(str(tmp_path)) / "bd"
    bd_dir.mkdir(parents=True)
    monkeypatch.setattr(beads_mod, "managed_bd_dir", lambda: bd_dir)
    monkeypatch.setenv("PATH", str(bd_dir) + os.pathsep + "/already/here")

    beads_mod.ensure_bd_dir_on_path()

    assert os.environ["PATH"].split(os.pathsep).count(str(bd_dir)) == 1


def test_ensure_bd_dir_on_path_noop_when_dir_missing(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No PATH mutation when nothing is installed at the canonical dir."""
    from pathlib import Path

    import agentshore.beads as beads_mod

    missing = Path(str(tmp_path)) / "nope"  # not created
    monkeypatch.setattr(beads_mod, "managed_bd_dir", lambda: missing)
    monkeypatch.setenv("PATH", "/already/here")

    beads_mod.ensure_bd_dir_on_path()

    assert os.environ["PATH"] == "/already/here"


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
    from pathlib import Path

    from agentshore.beads.setup import ensure_bd_installed

    with (
        patch("shutil.which", return_value=None),
        patch("agentshore.beads._managed_bd_path", return_value=Path("/does/not/exist/bd")),
        patch("agentshore.beads.setup.provision_bd", return_value=None),
        pytest.raises(RuntimeError, match="bd.*not found"),
    ):
        ensure_bd_installed()


@pytest.mark.skipif(
    sys.platform.startswith("win"), reason="bd stub is a POSIX shell script, not a Win32 exe"
)
def test_ensure_bd_installed_accepts_env_var(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    from agentshore.beads.setup import REQUIRED_BD_VERSION, ensure_bd_installed

    bd_path = Path(str(tmp_path)) / "bd"
    # Stub reports the pinned version so the version check passes too.
    bd_path.write_text(f'#!/bin/sh\necho "bd version {REQUIRED_BD_VERSION}"\n', encoding="utf-8")
    bd_path.chmod(0o755)
    monkeypatch.setenv("AGENTSHORE_BD_BIN", str(bd_path))
    monkeypatch.delenv("AGENTSHORE_BD_VERSION", raising=False)

    with patch("shutil.which", return_value=None):
        ensure_bd_installed()


@pytest.mark.skipif(
    sys.platform.startswith("win"), reason="bd stub is a POSIX shell script, not a Win32 exe"
)
def test_ensure_bd_installed_rejects_version_mismatch(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    from agentshore.beads.setup import ensure_bd_installed

    bd_path = Path(str(tmp_path)) / "bd"
    bd_path.write_text('#!/bin/sh\necho "bd version 0.9.0"\n', encoding="utf-8")
    bd_path.chmod(0o755)
    monkeypatch.setenv("AGENTSHORE_BD_BIN", str(bd_path))
    monkeypatch.delenv("AGENTSHORE_BD_VERSION", raising=False)

    with (
        patch("shutil.which", return_value=None),
        pytest.raises(RuntimeError, match="does not match"),
    ):
        ensure_bd_installed()


@pytest.mark.skipif(
    sys.platform.startswith("win"), reason="bd stub is a POSIX shell script, not a Win32 exe"
)
def test_ensure_bd_installed_version_override(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    from agentshore.beads.setup import ensure_bd_installed

    bd_path = Path(str(tmp_path)) / "bd"
    bd_path.write_text('#!/bin/sh\necho "bd version 0.9.0"\n', encoding="utf-8")
    bd_path.chmod(0o755)
    monkeypatch.setenv("AGENTSHORE_BD_BIN", str(bd_path))
    # Explicit override to the stub's version makes the check pass.
    monkeypatch.setenv("AGENTSHORE_BD_VERSION", "0.9.0")

    with patch("shutil.which", return_value=None):
        ensure_bd_installed()


def test_ensure_bd_installed_error_mentions_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    from agentshore.beads.setup import ensure_bd_installed

    monkeypatch.delenv("AGENTSHORE_BD_BIN", raising=False)
    with (
        patch("shutil.which", return_value=None),
        patch("agentshore.beads._managed_bd_path", return_value=Path("/does/not/exist/bd")),
        patch("agentshore.beads.setup.provision_bd", return_value=None),
        pytest.raises(RuntimeError, match="AGENTSHORE_BD_BIN"),
    ):
        ensure_bd_installed()


def test_ensure_bd_installed_auto_provisions_when_missing(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When bd is absent everywhere, ensure_bd_installed provisions it rather
    than raising."""
    from pathlib import Path

    from agentshore.beads import setup as setup_mod

    fake_bd = str(Path(str(tmp_path)) / "bd")
    # Empty pin disables the version check so the fake path need not run.
    monkeypatch.setenv("AGENTSHORE_BD_VERSION", "")
    monkeypatch.setattr(setup_mod, "provision_bd", lambda **_k: fake_bd)
    with (
        patch("shutil.which", return_value=None),
        patch("agentshore.beads._managed_bd_path", return_value=Path("/does/not/exist/bd")),
    ):
        setup_mod.ensure_bd_installed()  # must not raise


def _zip_with_bd(bd_name: str, payload: bytes) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(bd_name, payload)
    return buf.getvalue()


def _fake_httpx_client(archive_bytes: bytes, checksums_text: str) -> type:
    class _Resp:
        def __init__(self, content: bytes = b"", text: str = "") -> None:
            self.content = content
            self.text = text

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self, *_a: object, **_k: object) -> None: ...

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def get(self, url: str) -> _Resp:
            if url.endswith("checksums.txt"):
                return _Resp(text=checksums_text)
            return _Resp(content=archive_bytes)

    return _Client


def test_provision_bd_downloads_verifies_and_installs(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: asset downloaded, sha256-verified, bd extracted to the
    managed dir."""
    import hashlib
    import sys
    from pathlib import Path

    from agentshore.beads import setup as setup_mod

    bd_name = "bd.exe" if sys.platform.startswith("win") else "bd"
    archive = _zip_with_bd(bd_name, b"FAKE-BD-BINARY")
    sha = hashlib.sha256(archive).hexdigest()
    asset = "beads_test.zip"
    checksums = f"{sha}  {asset}\n0000  other_asset.tar.gz\n"

    managed_dir = Path(str(tmp_path)) / "bin"
    monkeypatch.setattr(setup_mod, "_beads_release_asset", lambda _v: (asset, "zip"))
    monkeypatch.setattr(setup_mod, "managed_bd_dir", lambda: managed_dir)
    monkeypatch.setattr("httpx.Client", _fake_httpx_client(archive, checksums))

    result = setup_mod.provision_bd(assume_yes=True)
    assert result == str(managed_dir / bd_name)
    assert (managed_dir / bd_name).read_bytes() == b"FAKE-BD-BINARY"


def test_provision_bd_returns_none_on_sha_mismatch(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    from pathlib import Path

    from agentshore.beads import setup as setup_mod

    bd_name = "bd.exe" if sys.platform.startswith("win") else "bd"
    archive = _zip_with_bd(bd_name, b"FAKE")
    asset = "beads_test.zip"
    checksums = f"{'0' * 64}  {asset}\n"  # deliberately wrong hash

    managed_dir = Path(str(tmp_path)) / "bin"
    monkeypatch.setattr(setup_mod, "_beads_release_asset", lambda _v: (asset, "zip"))
    monkeypatch.setattr(setup_mod, "managed_bd_dir", lambda: managed_dir)
    monkeypatch.setattr("httpx.Client", _fake_httpx_client(archive, checksums))

    assert setup_mod.provision_bd(assume_yes=True) is None
    assert not (managed_dir / bd_name).exists()


def test_provision_bd_uses_windows_native_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    import ssl

    from agentshore.beads import setup as setup_mod

    monkeypatch.setattr(setup_mod.sys, "platform", "win32")

    assert isinstance(setup_mod._httpx_verify_config(), ssl.SSLContext)


def test_provision_bd_declined_does_not_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """An interactive 'no' to the prompt skips the download entirely."""
    from unittest.mock import MagicMock

    from agentshore.beads import setup as setup_mod

    download = MagicMock()
    monkeypatch.setattr(setup_mod, "_download_bd", download)
    with patch("click.confirm", return_value=False):
        assert setup_mod.provision_bd(assume_yes=False) is None
    download.assert_not_called()


def test_drain_terminal_input_never_raises() -> None:
    """Draining is best-effort — must not raise with no real console (e.g. CI)."""
    from agentshore.beads import setup as setup_mod

    setup_mod._drain_terminal_input()  # must not raise


def test_provision_bd_drains_stdin_before_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Buffered keystrokes are flushed *before* the confirm so the prompt blocks
    instead of consuming a stray newline left by the preceding wizards."""
    from unittest.mock import MagicMock

    from agentshore.beads import setup as setup_mod

    order: list[str] = []
    monkeypatch.setattr(setup_mod, "_drain_terminal_input", lambda: order.append("drain"))
    monkeypatch.setattr(setup_mod, "_download_bd", MagicMock())

    def _confirm(*_a: object, **_k: object) -> bool:
        order.append("confirm")
        return False

    with patch("click.confirm", side_effect=_confirm):
        assert setup_mod.provision_bd(assume_yes=False) is None
    assert order == ["drain", "confirm"]
