"""Tests for the Windows event-loop-policy shim (Windows issue #64).

Every CLI agent is spawned via ``asyncio.create_subprocess_exec``, which on
Windows needs the ProactorEventLoop for subprocess-pipe support. The shim
installs the Proactor policy on win32 and is a no-op elsewhere. The
``WindowsProactorEventLoopPolicy`` attribute does not exist on macOS/Linux, so
the win32 test patches it with ``create=True``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agentshore.platform_compat import ensure_windows_event_loop_policy, force_utf8_stdio


def test_installs_proactor_policy_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    sentinel = object()
    with (
        patch(
            "asyncio.WindowsProactorEventLoopPolicy",
            create=True,
            return_value=sentinel,
        ),
        patch("asyncio.set_event_loop_policy") as set_policy,
    ):
        ensure_windows_event_loop_policy()
    set_policy.assert_called_once_with(sentinel)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_no_op_on_non_windows(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr("sys.platform", platform)
    with patch("asyncio.set_event_loop_policy") as set_policy:
        ensure_windows_event_loop_policy()
    set_policy.assert_not_called()


def test_force_utf8_stdio_reconfigures_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detached background procs redirect stdio to a cp1252 file; force UTF-8 so
    the box-drawing/arrow bootstrap output can't crash with UnicodeEncodeError."""
    monkeypatch.setattr("sys.platform", "win32")
    out, err = MagicMock(), MagicMock()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)

    force_utf8_stdio()

    out.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")
    err.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_force_utf8_stdio_no_op_off_windows(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr("sys.platform", platform)
    out = MagicMock()
    monkeypatch.setattr("sys.stdout", out)
    force_utf8_stdio()
    out.reconfigure.assert_not_called()
