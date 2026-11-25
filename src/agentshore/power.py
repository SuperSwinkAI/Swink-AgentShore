"""Cross-platform power assertion — keep the OS from idling our process.

Safe to hold concurrently with the CLI caffeinate wrapper.

Platforms
---------
* macOS — IOKit ``IOPMAssertionCreateWithName`` with type
  ``PreventUserIdleSystemSleep`` (system stays awake, I/O priority
  stays normal). Released via ``IOPMAssertionRelease``.
* Windows — ``kernel32!SetThreadExecutionState`` with
  ``ES_CONTINUOUS | ES_SYSTEM_REQUIRED``. Released by calling again
  with just ``ES_CONTINUOUS``.
* Linux / other — no-op. The screen-lock-throttle failure mode is
  macOS-specific and Linux uses per-service policies (systemd-inhibit)
  that don't have a single drop-in equivalent.

This module is import-safe on every platform: the native bindings
are only loaded inside ``acquire()`` so importing on, e.g., Linux
doesn't try to load libIOKit."""

from __future__ import annotations

import sys

from agentshore.logging import get_logger

_logger = get_logger(__name__)


_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

_KIOPM_ASSERT_LEVEL_ON = 255
_KIO_RETURN_SUCCESS = 0


class PowerAssertion:
    """Handle for an active OS-level "keep this process responsive" assertion.

    Acquire via :func:`acquire`. Release explicitly with :meth:`release`
    or by using the instance as a context manager. Idempotent — calling
    ``acquire`` twice in a row reuses the existing native handle so the
    OS-side ref count stays correct.
    """

    def __init__(self, reason: str) -> None:
        self._reason = reason
        self._held = False
        self._mac_assertion_id: int | None = None

    @property
    def is_held(self) -> bool:
        return self._held

    @property
    def platform(self) -> str:
        return sys.platform

    def release(self) -> None:
        if not self._held:
            return
        if sys.platform == "darwin":
            self._release_macos()
        elif sys.platform == "win32":
            self._release_windows()
        self._held = False
        _logger.info("power_assertion_released", reason=self._reason, platform=sys.platform)

    def __enter__(self) -> PowerAssertion:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def _acquire_macos(self) -> bool:
        """Take an IOPMAssertion. Returns True on success, False on failure."""
        try:
            import ctypes
            import ctypes.util
        except ImportError:  # pragma: no cover - ctypes is stdlib
            return False

        iokit_path = ctypes.util.find_library("IOKit")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if iokit_path is None or cf_path is None:
            _logger.warning(
                "power_assertion_libs_missing",
                iokit=iokit_path,
                core_foundation=cf_path,
            )
            return False
        iokit = ctypes.CDLL(iokit_path)
        cf = ctypes.CDLL(cf_path)

        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        cf.CFRelease.argtypes = [ctypes.c_void_p]

        _utf8 = 0x08000100
        type_cf = cf.CFStringCreateWithCString(None, b"PreventUserIdleSystemSleep", _utf8)
        name_cf = cf.CFStringCreateWithCString(None, self._reason.encode("utf-8"), _utf8)
        if not type_cf or not name_cf:
            if type_cf:
                cf.CFRelease(type_cf)
            if name_cf:
                cf.CFRelease(name_cf)
            return False

        iokit.IOPMAssertionCreateWithName.restype = ctypes.c_int
        iokit.IOPMAssertionCreateWithName.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        assertion_id = ctypes.c_uint32(0)
        rc = iokit.IOPMAssertionCreateWithName(
            type_cf,
            _KIOPM_ASSERT_LEVEL_ON,
            name_cf,
            ctypes.byref(assertion_id),
        )
        cf.CFRelease(type_cf)
        cf.CFRelease(name_cf)
        if rc != _KIO_RETURN_SUCCESS:
            _logger.warning("power_assertion_iokit_failed", rc=rc)
            return False
        self._mac_assertion_id = int(assertion_id.value)
        return True

    def _release_macos(self) -> None:
        if self._mac_assertion_id is None:
            return
        try:
            import ctypes
            import ctypes.util
        except ImportError:  # pragma: no cover
            return
        iokit_path = ctypes.util.find_library("IOKit")
        if iokit_path is None:
            return
        iokit = ctypes.CDLL(iokit_path)
        iokit.IOPMAssertionRelease.restype = ctypes.c_int
        iokit.IOPMAssertionRelease.argtypes = [ctypes.c_uint32]
        rc = iokit.IOPMAssertionRelease(self._mac_assertion_id)
        if rc != _KIO_RETURN_SUCCESS:
            _logger.warning("power_assertion_release_failed", rc=rc)
        self._mac_assertion_id = None

    def _acquire_windows(self) -> bool:
        try:
            import ctypes
        except ImportError:  # pragma: no cover
            return False
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        except AttributeError:  # pragma: no cover - non-Windows ctypes layout
            return False
        prev = kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
        if prev == 0:
            _logger.warning("power_assertion_set_thread_execution_state_failed")
            return False
        return True

    def _release_windows(self) -> None:
        try:
            import ctypes
        except ImportError:  # pragma: no cover
            return
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        except AttributeError:  # pragma: no cover
            return
        kernel32.SetThreadExecutionState(_ES_CONTINUOUS)


def acquire(reason: str = "AgentShore session active") -> PowerAssertion:
    """Acquire a power assertion for the lifetime of the returned handle.

    The reason string is surfaced in macOS ``pmset -g assertions`` output
    and any structured-log telemetry. Always release the returned handle
    in the session-shutdown path (and in signal handlers) — leaking the
    assertion can keep the user's machine awake after AgentShore exits.
    """
    handle = PowerAssertion(reason)
    if sys.platform == "darwin":
        ok = handle._acquire_macos()
    elif sys.platform == "win32":
        ok = handle._acquire_windows()
    else:
        ok = True
    handle._held = ok
    if ok:
        _logger.info("power_assertion_acquired", reason=reason, platform=sys.platform)
    else:
        _logger.warning("power_assertion_acquire_failed", platform=sys.platform)
    return handle


__all__ = ["PowerAssertion", "acquire"]
