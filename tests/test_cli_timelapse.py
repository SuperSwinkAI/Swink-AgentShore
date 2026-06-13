"""CLI dashboard-timelapse wiring.

The desktop sidecar owns timelapse capture in-memory; the CLI splits a session
across detached processes, so it coordinates the capture run-id through the
session dir. These tests pin that coordination: start persists the run-id, the
orchestrator/stop finalise it, and every step is best-effort (a missing binary
or a capture error must never block start/stop).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

# Establish the cli<->session.bootstrap import order (see test_cli_config_overrides).
import agentshore.cli  # noqa: F401
import agentshore.session_path as sp
from agentshore.cli import main
from agentshore.cli.runtime import _finalize_cli_timelapse, _maybe_start_cli_timelapse
from agentshore.timelapse import TimelapseError, TimelapseRun


@pytest.fixture
def sessions_dir(monkeypatch: pytest.MonkeyPatch) -> Path:
    # POSIX uses /tmp to keep AF_UNIX socket paths short; Windows has no /tmp.
    tmp_root = None if sys.platform.startswith("win") else "/tmp"
    d = Path(tempfile.mkdtemp(prefix="tl_sessions_", dir=tmp_root))
    monkeypatch.setattr(sp, "_SESSIONS_DIR", d)
    return d


# --------------------------------------------------------------------------- #
# session_path persistence
# --------------------------------------------------------------------------- #


def test_timelapse_info_round_trip(tmp_path: Path, sessions_dir: Path) -> None:
    sp.write_timelapse_info(tmp_path, run_id="brave-otter-7", runs_cwd=tmp_path / ".agentshore")
    info = sp.read_timelapse_info(tmp_path)
    assert info == {"run_id": "brave-otter-7", "runs_cwd": str(tmp_path / ".agentshore")}


def test_timelapse_info_absent_returns_none(tmp_path: Path, sessions_dir: Path) -> None:
    assert sp.read_timelapse_info(tmp_path) is None


def test_clear_timelapse_info_removes_file(tmp_path: Path, sessions_dir: Path) -> None:
    sp.write_timelapse_info(tmp_path, run_id="x", runs_cwd=tmp_path)
    sp.clear_timelapse_info(tmp_path)
    assert sp.read_timelapse_info(tmp_path) is None
    sp.clear_timelapse_info(tmp_path)  # idempotent


def test_cleanup_session_removes_timelapse_info(tmp_path: Path, sessions_dir: Path) -> None:
    sp.write_timelapse_info(tmp_path, run_id="x", runs_cwd=tmp_path)
    sp.cleanup_session(tmp_path)
    assert sp.read_timelapse_info(tmp_path) is None


# --------------------------------------------------------------------------- #
# start: _maybe_start_cli_timelapse
# --------------------------------------------------------------------------- #


def test_start_capture_persists_run_id(tmp_path: Path, sessions_dir: Path) -> None:
    run = TimelapseRun(run_id="swift-fox-3", run_dir="/runs/swift-fox-3")
    with (
        patch("agentshore.timelapse.resolve_timelapse_binary", return_value="/usr/bin/tl"),
        patch("agentshore.timelapse.start_capture", new=AsyncMock(return_value=run)) as start,
    ):
        _maybe_start_cli_timelapse(tmp_path, "http://localhost:9400")

    start.assert_awaited_once()
    assert start.await_args.args[0] == "http://localhost:9400"
    assert sp.read_timelapse_info(tmp_path) == {
        "run_id": "swift-fox-3",
        "runs_cwd": str(tmp_path / ".agentshore"),
    }


def test_start_capture_skipped_when_binary_missing(tmp_path: Path, sessions_dir: Path) -> None:
    with (
        patch("agentshore.timelapse.resolve_timelapse_binary", return_value=None),
        patch("agentshore.timelapse.start_capture", new=AsyncMock()) as start,
    ):
        _maybe_start_cli_timelapse(tmp_path, "http://localhost:9400")

    start.assert_not_called()
    assert sp.read_timelapse_info(tmp_path) is None


def test_start_capture_error_is_swallowed(tmp_path: Path, sessions_dir: Path) -> None:
    with (
        patch("agentshore.timelapse.resolve_timelapse_binary", return_value="/usr/bin/tl"),
        patch(
            "agentshore.timelapse.start_capture",
            new=AsyncMock(side_effect=TimelapseError("boom")),
        ),
    ):
        _maybe_start_cli_timelapse(tmp_path, "http://localhost:9400")  # must not raise

    assert sp.read_timelapse_info(tmp_path) is None


# --------------------------------------------------------------------------- #
# finalize: _finalize_cli_timelapse
# --------------------------------------------------------------------------- #


def test_finalize_stops_renders_and_clears(tmp_path: Path, sessions_dir: Path) -> None:
    sp.write_timelapse_info(tmp_path, run_id="run-9", runs_cwd=tmp_path / ".agentshore")
    with (
        patch("agentshore.timelapse.stop_capture", new=AsyncMock()) as stop,
        patch("agentshore.timelapse.await_output", new=AsyncMock(return_value="/out/x.mp4")),
    ):
        out = _finalize_cli_timelapse(tmp_path)

    assert out == "/out/x.mp4"
    stop.assert_awaited_once()
    assert stop.await_args.args[0] == "run-9"
    assert sp.read_timelapse_info(tmp_path) is None


def test_finalize_noop_without_sidecar(tmp_path: Path, sessions_dir: Path) -> None:
    with patch("agentshore.timelapse.stop_capture", new=AsyncMock()) as stop:
        assert _finalize_cli_timelapse(tmp_path) is None
    stop.assert_not_called()


def test_finalize_swallows_stop_error_and_clears(tmp_path: Path, sessions_dir: Path) -> None:
    sp.write_timelapse_info(tmp_path, run_id="run-9", runs_cwd=tmp_path)
    with (
        patch(
            "agentshore.timelapse.stop_capture",
            new=AsyncMock(side_effect=TimelapseError("already stopped")),
        ),
        patch("agentshore.timelapse.await_output", new=AsyncMock(return_value=None)),
    ):
        out = _finalize_cli_timelapse(tmp_path)  # must not raise

    assert out is None
    assert sp.read_timelapse_info(tmp_path) is None


# --------------------------------------------------------------------------- #
# stop command backstop
# --------------------------------------------------------------------------- #


def _make_git_repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "agentshore.yaml").write_text("budget:\n  enabled: true\n  total: 20.0\n")
    return tmp_path


def test_stop_finalizes_timelapse_when_present(tmp_path: Path) -> None:
    project = _make_git_repo(tmp_path)
    info = {"run_id": "run-1", "runs_cwd": str(project / ".agentshore")}
    runner = CliRunner()
    with (
        patch("agentshore.session_path.is_session_running", return_value=True),
        patch("agentshore.session_path.request_drain", return_value="sent"),
        patch("agentshore.session_path.read_timelapse_info", return_value=info),
        patch("agentshore.cli.commands.stop._wait_for_session_exit", return_value=True),
        patch("agentshore.cli.runtime._finalize_cli_timelapse", return_value="/out.mp4") as fin,
    ):
        result = runner.invoke(main, ["stop", "--project", str(project)])

    assert result.exit_code == 0, result.output
    fin.assert_called_once_with(project, info=info, echo=True)


def test_stop_skips_finalize_when_no_timelapse(tmp_path: Path) -> None:
    project = _make_git_repo(tmp_path)
    runner = CliRunner()
    with (
        patch("agentshore.session_path.is_session_running", return_value=True),
        patch("agentshore.session_path.request_drain", return_value="sent"),
        patch("agentshore.session_path.read_timelapse_info", return_value=None),
        patch("agentshore.cli.commands.stop._wait_for_session_exit", return_value=True),
        patch("agentshore.cli.runtime._finalize_cli_timelapse") as fin,
    ):
        result = runner.invoke(main, ["stop", "--project", str(project)])

    assert result.exit_code == 0, result.output
    fin.assert_not_called()
