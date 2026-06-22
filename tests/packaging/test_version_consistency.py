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


def test_cargo_lock_crates_match_canonical() -> None:
    """The desktop Cargo.lock's local crates must match canonical, or the signed
    `cargo build --locked` step fails on the next bump."""
    version = _load_version_module()
    canonical = version.read_canonical(_REPO)
    locked = version.cargo_lock_versions(_REPO)
    assert set(locked) == set(version.CARGO_LOCK_PACKAGES)
    assert all(v == canonical for v in locked.values()), (
        f"Cargo.lock drift from {canonical}: {locked} — "
        "run `uv run python -m scripts.buildkit version --write` to sync."
    )


def test_write_refreshes_cargo_lock_round_trip(tmp_path: Path) -> None:
    """`write()`'s lock helper bumps only the local workspace crates, leaves other
    crates alone, and is idempotent (the regression that broke the 0.6.2 build)."""
    version = _load_version_module()
    lock = tmp_path / "desktop" / "src-tauri" / "Cargo.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(
        '[[package]]\nname = "agentshore-desktop"\nversion = "0.0.0"\ndependencies = []\n\n'
        '[[package]]\nname = "some-dep"\nversion = "1.2.3"\n\n'
        '[[package]]\nname = "agentshore-provisioner"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )

    changed = version._write_cargo_lock(tmp_path, "9.9.9")
    assert changed is True
    assert version.cargo_lock_versions(tmp_path) == {
        "agentshore-desktop": "9.9.9",
        "agentshore-provisioner": "9.9.9",
    }
    # An unrelated dependency's version is untouched.
    assert 'name = "some-dep"\nversion = "1.2.3"' in lock.read_text(encoding="utf-8")
    # Idempotent: a second sync at the same version is a no-op.
    assert version._write_cargo_lock(tmp_path, "9.9.9") is False
