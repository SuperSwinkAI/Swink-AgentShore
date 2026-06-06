"""Tests for the Windows event-loop-policy shim (Windows issue #64).

Every CLI agent is spawned via ``asyncio.create_subprocess_exec``, which on
Windows needs the ProactorEventLoop for subprocess-pipe support. The shim
installs the Proactor policy on win32 and is a no-op elsewhere. The
``WindowsProactorEventLoopPolicy`` attribute does not exist on macOS/Linux, so
the win32 test patches it with ``create=True``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agentshore.platform_compat import ensure_windows_event_loop_policy


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
