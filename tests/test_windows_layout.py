"""Cross-language install-layout pin test.

Two groups of tests:

1. **Windows-only path-layout pin** (``pytestmark`` skip on POSIX): asserts
   that ``subprocess_env._canonical_windows_paths`` includes the key directories
   that ``desktop/src-tauri/src/install_layout.rs`` defines. If this test fails,
   one side drifted — update both files together and refresh this test.

2. **Cross-platform PATH-safety test** (runs everywhere): asserts that
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

import os
from pathlib import Path

import pytest


def _canonical_paths_for(tool: str) -> list[Path]:
    """Return the candidate list from subprocess_env for *tool*."""
    from agentshore import subprocess_env

    return list(subprocess_env._canonical_windows_paths(tool))


def _programdata() -> Path:
    """Resolve %ProgramData% (PROGRAMDATA) with the standard Windows fallback."""
    value = os.environ.get("PROGRAMDATA")
    return Path(value) if value else Path(r"C:\ProgramData")


def test_agentshore_bin_in_git_candidates() -> None:
    """The managed bin dir must appear in the git canonical-path list.

    install_layout::managed_bin_path() == ProgramData\\AgentShore\\bin.
    subprocess_env._canonical_windows_paths("git") must include it (via the
    'Common to all' tail that ends with ProgramData\\AgentShore\\bin).
    """
    managed_bin = _programdata() / "AgentShore" / "bin"
    candidates = _canonical_paths_for("git")
    candidate_dirs = {c.parent for c in candidates}
    assert managed_bin in candidate_dirs, (
        f"ProgramData\\AgentShore\\bin missing from git canonical-path candidates.\n"
        f"Expected {managed_bin} in candidate dirs: {sorted(candidate_dirs)}"
    )


def test_agentshore_bin_in_gh_candidates() -> None:
    """The managed bin dir must appear in the gh canonical-path list."""
    managed_bin = _programdata() / "AgentShore" / "bin"
    candidates = _canonical_paths_for("gh")
    candidate_dirs = {c.parent for c in candidates}
    assert managed_bin in candidate_dirs, (
        f"ProgramData\\AgentShore\\bin missing from gh canonical-path candidates.\n"
        f"Expected {managed_bin} in candidate dirs: {sorted(candidate_dirs)}"
    )


def test_managed_bin_path_suffix_matches_rust_constant() -> None:
    """The suffix ``AgentShore\\bin`` must be the last component pair in git/gh lists.

    This asserts the string literal in subprocess_env._canonical_windows_paths
    matches the path install_layout::managed_bin_path() produces.
    The Rust side uses: ``agentshore_data_root().join("bin")``
                      = ``ProgramData\\AgentShore\\bin``

    bd is excluded: bd resolution is owned by agentshore.beads.resolve_bd_binary
    (PATH + AGENTSHORE_BD_BIN only). The installer drops bd.exe into the managed
    bin dir, which the sidecar PATH overlay already prepends.
    """
    expected_suffix = Path("AgentShore") / "bin"
    for tool in ("git", "gh"):
        candidates = _canonical_paths_for(tool)
        # At least one candidate must end with AgentShore\bin\<tool>.exe
        matching = [c for c in candidates if c.parts[-3:-1] == expected_suffix.parts]
        assert matching, (
            f"No candidate for tool={tool!r} ends with {expected_suffix}. Candidates: {candidates}"
        )


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
        # Also assert no path is just a drive letter or backslash root without
        # the expected AgentShore sub-directory (guards against partial-path bugs).
        assert len(path.parts) >= 2, (
            f"Suspiciously short path in _canonical_windows_paths({tool!r}): {path}"
        )
