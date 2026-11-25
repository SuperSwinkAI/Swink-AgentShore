"""Tests for the cross-platform power assertion (desktop-gkku).

Covers:
  - Linux/non-Darwin/non-Win32 path is a no-op (no native calls).
  - macOS path resolves IOKit + CoreFoundation, calls
    IOPMAssertionCreateWithName, stores the returned assertion ID, and
    releases it on .release().
  - Windows path calls kernel32.SetThreadExecutionState with
    ES_CONTINUOUS|ES_SYSTEM_REQUIRED on acquire and ES_CONTINUOUS alone
    on release.
  - Acquire failures (missing libs, IOReturn != success, SetThread
    returns 0) leave the handle in held=False without raising.
  - Context-manager protocol releases on exit.
"""

from __future__ import annotations

import ctypes
from unittest.mock import MagicMock, patch

import pytest

from agentshore import power as power_mod


def test_acquire_on_linux_is_a_noop_but_returns_held(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    with patch.object(ctypes, "CDLL") as cdll:
        handle = power_mod.acquire("test")
    assert handle.is_held is True
    cdll.assert_not_called()
    handle.release()
    assert handle.is_held is False


def test_acquire_on_unknown_platform_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "freebsd14")
    handle = power_mod.acquire("test")
    assert handle.is_held is True
    handle.release()


def _make_fake_macos_libs() -> tuple[MagicMock, MagicMock]:
    """Build mock IOKit + CoreFoundation handles that simulate success."""
    iokit = MagicMock()
    cf = MagicMock()

    cf.CFStringCreateWithCString.return_value = 0xDEADBEEF

    def _create_assertion(_type, _level, _name, id_ptr):  # noqa: ANN001
        id_ptr._obj.value = 1234
        return 0

    iokit.IOPMAssertionCreateWithName.side_effect = _create_assertion
    iokit.IOPMAssertionRelease.return_value = 0
    return iokit, cf


def test_acquire_on_macos_calls_iopm_assertion_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    iokit, cf = _make_fake_macos_libs()

    def fake_find_library(name: str) -> str:
        return f"/usr/lib/lib{name}.dylib"

    def fake_cdll(path: str) -> MagicMock:
        return iokit if "IOKit" in path else cf

    with (
        patch("ctypes.util.find_library", side_effect=fake_find_library),
        patch("ctypes.CDLL", side_effect=fake_cdll),
    ):
        handle = power_mod.acquire("test-macos")

    assert handle.is_held is True
    assert handle._mac_assertion_id == 1234
    iokit.IOPMAssertionCreateWithName.assert_called_once()
    assert cf.CFRelease.call_count == 2


def test_release_on_macos_calls_iopm_assertion_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    iokit, cf = _make_fake_macos_libs()

    with (
        patch(
            "ctypes.util.find_library",
            side_effect=lambda name: f"/usr/lib/lib{name}.dylib",
        ),
        patch("ctypes.CDLL", side_effect=lambda path: iokit if "IOKit" in path else cf),
    ):
        handle = power_mod.acquire("test-macos-release")
        handle.release()

    iokit.IOPMAssertionRelease.assert_called_once_with(1234)
    assert handle.is_held is False
    assert handle._mac_assertion_id is None


def test_macos_acquire_failure_when_iokit_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    with (
        patch("ctypes.util.find_library", return_value=None),
        patch("ctypes.CDLL") as cdll,
    ):
        handle = power_mod.acquire("missing-libs")
    assert handle.is_held is False
    cdll.assert_not_called()


def test_macos_acquire_failure_when_ioreturn_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    iokit, cf = _make_fake_macos_libs()
    iokit.IOPMAssertionCreateWithName.side_effect = lambda *_a, **_k: 0xE0000001

    with (
        patch(
            "ctypes.util.find_library",
            side_effect=lambda name: f"/usr/lib/lib{name}.dylib",
        ),
        patch("ctypes.CDLL", side_effect=lambda path: iokit if "IOKit" in path else cf),
    ):
        handle = power_mod.acquire("ioreturn-fail")

    assert handle.is_held is False
    assert cf.CFRelease.call_count == 2


def test_acquire_on_windows_sets_thread_execution_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    kernel32 = MagicMock()
    kernel32.SetThreadExecutionState.return_value = 0x80000000

    fake_windll = MagicMock(kernel32=kernel32)
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)

    handle = power_mod.acquire("win32-acquire")

    assert handle.is_held is True
    kernel32.SetThreadExecutionState.assert_called_once_with(0x80000001)


def test_release_on_windows_clears_execution_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    kernel32 = MagicMock()
    kernel32.SetThreadExecutionState.return_value = 0x80000000
    fake_windll = MagicMock(kernel32=kernel32)
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)

    handle = power_mod.acquire("win32-release")
    kernel32.SetThreadExecutionState.reset_mock()
    handle.release()

    kernel32.SetThreadExecutionState.assert_called_once_with(0x80000000)
    assert handle.is_held is False


def test_windows_acquire_failure_when_set_thread_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    kernel32 = MagicMock()
    kernel32.SetThreadExecutionState.return_value = 0
    fake_windll = MagicMock(kernel32=kernel32)
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)

    handle = power_mod.acquire("win32-fail")
    assert handle.is_held is False


def test_release_twice_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    handle = power_mod.acquire("idempotent-release")
    handle.release()
    handle.release()
    assert handle.is_held is False


def test_context_manager_releases_on_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    with power_mod.acquire("ctx-mgr") as handle:
        assert handle.is_held is True
    assert handle.is_held is False


def test_release_before_acquire_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """release() on a never-acquired handle must not raise.

    Defensive — the orchestrator's stop path runs unconditionally and
    must tolerate the case where __aenter__ failed before the assertion
    was taken.
    """
    monkeypatch.setattr("sys.platform", "linux")
    handle = power_mod.PowerAssertion("never-acquired")
    handle.release()
    assert handle.is_held is False
