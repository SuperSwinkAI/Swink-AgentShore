"""Tests for the caffeinate re-exec wrapper (desktop-n7ci).

The wrapper protects ``agentshore start`` from macOS screen-lock I/O
throttling that produces silent SQLite corruption (desktop-tvsb). It
must be a no-op on non-Darwin, inside pytest, when caffeinate is missing,
when the user opted out, and on a recursive call after a successful
re-exec.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agentshore.cli import caffeinate as caffeinate_mod


def _hide_pytest_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide pytest's ``PYTEST_CURRENT_TEST`` env var so the wrapper's
    in-pytest guard doesn't short-circuit the test. pytest sets this var
    at the start of each test phase (setup → call → teardown), so a
    fixture-time delete is reverted before the test body runs; we have
    to call this helper *inside* the call phase."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear caffeinate-related env so each test starts from a known state."""
    monkeypatch.delenv("AGENTSHORE_CAFFEINATED", raising=False)
    monkeypatch.delenv("AGENTSHORE_NO_CAFFEINATE", raising=False)


def test_no_op_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    _hide_pytest_marker(monkeypatch)
    monkeypatch.setattr("sys.platform", "linux")
    with patch("os.execvp") as execvp:
        caffeinate_mod.maybe_re_exec_under_caffeinate()
    execvp.assert_not_called()


def test_no_op_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    _hide_pytest_marker(monkeypatch)
    monkeypatch.setattr("sys.platform", "win32")
    with patch("os.execvp") as execvp:
        caffeinate_mod.maybe_re_exec_under_caffeinate()
    execvp.assert_not_called()


def test_no_op_under_pytest_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    """When PYTEST_CURRENT_TEST is set we must not re-exec. (This test does
    NOT call _hide_pytest_marker — it relies on pytest's own marker.)"""
    monkeypatch.setattr("sys.platform", "darwin")
    with patch("shutil.which", return_value="/usr/bin/caffeinate"), patch("os.execvp") as execvp:
        caffeinate_mod.maybe_re_exec_under_caffeinate()
    execvp.assert_not_called()


def test_no_op_when_already_caffeinated(monkeypatch: pytest.MonkeyPatch) -> None:
    _hide_pytest_marker(monkeypatch)
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setenv("AGENTSHORE_CAFFEINATED", "1")
    with patch("shutil.which", return_value="/usr/bin/caffeinate"), patch("os.execvp") as execvp:
        caffeinate_mod.maybe_re_exec_under_caffeinate()
    execvp.assert_not_called()


def test_no_op_when_user_opted_out(monkeypatch: pytest.MonkeyPatch) -> None:
    _hide_pytest_marker(monkeypatch)
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setenv("AGENTSHORE_NO_CAFFEINATE", "1")
    with patch("shutil.which", return_value="/usr/bin/caffeinate"), patch("os.execvp") as execvp:
        caffeinate_mod.maybe_re_exec_under_caffeinate()
    execvp.assert_not_called()


def test_no_op_when_caffeinate_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _hide_pytest_marker(monkeypatch)
    monkeypatch.setattr("sys.platform", "darwin")
    with patch("shutil.which", return_value=None), patch("os.execvp") as execvp:
        caffeinate_mod.maybe_re_exec_under_caffeinate()
    execvp.assert_not_called()


def test_re_execs_on_darwin_with_expected_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    _hide_pytest_marker(monkeypatch)
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("sys.argv", ["/path/to/agentshore", "start", "--budget", "10"])
    with (
        patch("shutil.which", return_value="/usr/bin/caffeinate"),
        patch("os.execvp") as execvp,
    ):
        caffeinate_mod.maybe_re_exec_under_caffeinate()
    execvp.assert_called_once_with(
        "/usr/bin/caffeinate",
        [
            "/usr/bin/caffeinate",
            "-i",
            "/path/to/agentshore",
            "start",
            "--budget",
            "10",
        ],
    )


def test_sets_sentinel_before_exec_to_block_recursive_re_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sentinel env must be set before exec so the re-execd process
    short-circuits on its first call to the wrapper."""
    _hide_pytest_marker(monkeypatch)
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("sys.argv", ["agentshore", "start"])

    captured: dict[str, str | None] = {}

    def fake_execvp(_path: str, _args: list[str]) -> None:
        # In a real exec the env is inherited by the new process; we just
        # check it's set at the moment we exec.
        import os as _os

        captured["sentinel"] = _os.environ.get("AGENTSHORE_CAFFEINATED")

    with (
        patch("shutil.which", return_value="/usr/bin/caffeinate"),
        patch("os.execvp", side_effect=fake_execvp),
    ):
        caffeinate_mod.maybe_re_exec_under_caffeinate()

    assert captured["sentinel"] == "1"
