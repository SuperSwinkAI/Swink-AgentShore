"""Cross-language install-layout pin test.

Asserts that the paths emitted by ``subprocess_env._canonical_windows_paths``
(the Python side of the layout contract) include the key directories that
``desktop/src-tauri/src/install_layout.rs`` (the Rust side) defines.

If this test fails it means one side drifted — update both files together and
refresh this test so the two-sided contract is re-pinned.

Runs only on Windows; skipped on macOS/Linux where the ProgramData layout does
not apply.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows install-layout pin only applies on Windows",
)


def _canonical_paths_for(tool: str) -> list[Path]:
    """Return the candidate list from subprocess_env for *tool*."""
    from agentshore import subprocess_env

    return list(subprocess_env._canonical_windows_paths(tool))


def _programdata() -> Path:
    """Resolve %ProgramData% with the same fallback as install_layout.rs."""
    value = os.environ.get("ProgramData") or os.environ.get("PROGRAMDATA")
    return Path(value) if value else Path(r"C:\ProgramData")


def test_agentshore_bin_in_git_candidates() -> None:
    """The managed bin dir must appear in the git canonical-path list.

    install_layout::managed_bin_path() == ProgramData\AgentShore\bin.
    subprocess_env._canonical_windows_paths("git") must include it (via the
    'Common to all' tail that ends with ProgramData\AgentShore\bin).
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


def test_agentshore_bin_in_bd_candidates() -> None:
    """The managed bin dir must appear in the bd canonical-path list.

    This path is what install_layout::managed_bin_path() resolves to and where
    the provisioner drops bd.exe during install.
    """
    managed_bin = _programdata() / "AgentShore" / "bin"
    candidates = _canonical_paths_for("bd")
    candidate_dirs = {c.parent for c in candidates}
    assert managed_bin in candidate_dirs, (
        f"ProgramData\\AgentShore\\bin missing from bd canonical-path candidates.\n"
        f"Expected {managed_bin} in candidate dirs: {sorted(candidate_dirs)}"
    )


def test_managed_bin_path_suffix_matches_rust_constant() -> None:
    """The suffix ``AgentShore\\bin`` must be the last component pair in all tool lists.

    This asserts the string literal in subprocess_env._canonical_windows_paths
    matches the path install_layout::managed_bin_path() produces.
    The Rust side uses: ``agentshore_data_root().join("bin")``
                      = ``ProgramData\\AgentShore\\bin``
    """
    expected_suffix = Path("AgentShore") / "bin"
    for tool in ("git", "gh", "bd"):
        candidates = _canonical_paths_for(tool)
        # At least one candidate must end with AgentShore\bin\<tool>.exe
        matching = [
            c
            for c in candidates
            if c.parts[-3:-1] == expected_suffix.parts
        ]
        assert matching, (
            f"No candidate for tool={tool!r} ends with {expected_suffix}. "
            f"Candidates: {candidates}"
        )
