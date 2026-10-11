"""Cross-language install-layout pin test.

Two groups of tests:

**Cross-platform PATH-safety test** (runs everywhere): asserts that
   ``subprocess_env._canonical_windows_paths`` never yields an empty path, even
   on POSIX hosts where ProgramData/LOCALAPPDATA env vars might be set
   accidentally. An empty PATH entry is cwd in POSIX PATH semantics — a
   security hole. Fixes the bug from issue #125 where
   ``sidecar_runtime.machine_managed_bin_path`` returned ``PathBuf::new()``
   off-Windows and was pushed into the PATH candidates without a guard.

Note: ``subprocess_env._canonical_windows_paths`` handles git/gh only.
``bd`` resolution is owned by ``agentshore.beads.resolve_bd_binary`` (#126).
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _canonical_paths_for(tool: str) -> list[Path]:
    """Return the candidate list from subprocess_env for *tool*."""
    from agentshore import subprocess_env

    return list(subprocess_env._canonical_windows_paths(tool))


# ---------------------------------------------------------------------------
# Cross-platform PATH-safety tests (run on all platforms)
# These run unconditionally — the safety property must hold everywhere.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["git", "gh"])
def test_canonical_paths_never_empty_string(tool: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """No candidate from ``_canonical_windows_paths`` may be an empty path.

    An empty PATH entry is treated as the current working directory by POSIX
    shells — a security hole. The Rust sidecar_env.rs previously pushed
    ``PathBuf::new()`` (empty) off-Windows when PROGRAMDATA was set, prepending
    cwd to the sidecar PATH. The #[cfg(target_os = "windows")] guard in
    install_layout.rs fixes the Rust side; this test pins the Python side.

    We simulate a POSIX host with PROGRAMDATA set (mimicking a CI runner that
    exports it) to confirm no empty-string candidate is produced.

    bd is excluded: ``_canonical_windows_paths`` no longer handles bd (#126).
    """
    from agentshore import subprocess_env

    # Simulate POSIX with PROGRAMDATA accidentally set.
    monkeypatch.setattr(subprocess_env.sys, "platform", "linux")
    monkeypatch.setenv("PROGRAMDATA", r"C:\ProgramData")

    candidates = list(subprocess_env._canonical_windows_paths(tool))
    for path in candidates:
        assert str(path) != "", (
            f"Empty path in _canonical_windows_paths({tool!r}) on POSIX: {candidates}"
        )
        # Guard partial-path bugs: never a bare drive letter / root sans subdir.
        assert len(path.parts) >= 2, (
            f"Suspiciously short path in _canonical_windows_paths({tool!r}): {path}"
        )
