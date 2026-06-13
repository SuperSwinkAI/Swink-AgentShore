"""Windows loader-lock deadlock guard.

numpy (OpenBLAS) and torch spawn native worker threads during C-extension init,
under the OS loader lock held by the importing thread. If that import runs after
the sidecar already has live threads (the ``project.inspect`` probe pool and the
``asyncio.to_thread`` executor created on the setup screens), the new native
threads deadlock on ``DllMain(THREAD_ATTACH)`` and the sidecar wedges at 0 CPU.
:func:`agentshore.sidecar.server._preload_native_libraries` maps both DLLs at
boot, single-threaded, before :func:`serve` starts any thread. These tests lock
in that ordering and the win32 gate.
"""

from __future__ import annotations

import sys

import pytest

from agentshore.sidecar import server


def test_run_preloads_native_libraries_before_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(server, "force_utf8_stdio", lambda: calls.append("utf8"))
    monkeypatch.setattr(
        server, "ensure_windows_event_loop_policy", lambda: calls.append("loop_policy")
    )
    monkeypatch.setattr(server, "_configure_sidecar_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(server, "_preload_native_libraries", lambda: calls.append("preload"))
    monkeypatch.setattr(server, "serve", lambda *args, **kwargs: calls.append("serve"))

    server.run()

    assert "preload" in calls
    assert "serve" in calls
    assert calls.index("preload") < calls.index("serve"), (
        "native libs must be preloaded before serve() spawns the reader thread / loop"
    )


def test_preload_is_noop_off_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    # On POSIX there is no loader-lock hazard and torch's import cost should not
    # be forced onto every sidecar boot — the function must short-circuit.
    monkeypatch.setattr(server.sys, "platform", "linux")
    server._preload_native_libraries()  # must not raise


@pytest.mark.skipif(sys.platform != "win32", reason="win32-only loader-lock guard")
def test_preload_loads_numpy_and_torch_on_win32() -> None:
    server._preload_native_libraries()
    assert "numpy" in sys.modules
    assert "torch" in sys.modules
