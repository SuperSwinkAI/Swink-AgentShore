"""Guard: every version mirror must equal the canonical pyproject.toml version.

This is the in-suite half of the version single-source-of-truth (the other half
is `python -m scripts.buildkit version --check`, run by the build spine and CI).
Loading the spine module by file path keeps the test independent of whether
`scripts/` is importable as a package in the test environment.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_REPO = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO / "scripts" / "buildkit" / "version.py"


def _load_version_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agentshore_build_version", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_all_version_mirrors_match_canonical() -> None:
    version = _load_version_module()
    drift = version.find_drift(_REPO)
    assert drift == [], (
        "Version drift from canonical pyproject.toml: "
        + ", ".join(f"{rel}={value}" for rel, value in drift)
        + " — run `uv run python -m scripts.buildkit version --write` to sync."
    )


def test_canonical_version_is_nonempty() -> None:
    version = _load_version_module()
    assert version.read_canonical(_REPO).strip()
