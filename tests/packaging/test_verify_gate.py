"""Guard: the post-build verification gate catches a stale/stray .app payload.

Exercises scripts/buildkit/verify.py against synthetic .app bundles so the
"no stray binary in Contents/MacOS" invariant (the false-green that bundled a
stale agentshore-provisioner) is enforced in the suite, without a real build.
"""

from __future__ import annotations

import importlib.util
import plistlib
from pathlib import Path
from types import ModuleType

_REPO = Path(__file__).resolve().parents[2]
_VERIFY_PATH = _REPO / "scripts" / "buildkit" / "verify.py"


def _load() -> ModuleType:
    # Load as part of the scripts.buildkit package so its relative imports resolve.
    pkg_spec = importlib.util.spec_from_file_location(
        "scripts_buildkit_pkg",
        _REPO / "scripts" / "buildkit" / "__init__.py",
        submodule_search_locations=[str(_REPO / "scripts" / "buildkit")],
    )
    assert pkg_spec is not None and pkg_spec.loader is not None
    pkg = importlib.util.module_from_spec(pkg_spec)
    import sys

    sys.modules["scripts_buildkit_pkg"] = pkg
    pkg_spec.loader.exec_module(pkg)

    spec = importlib.util.spec_from_file_location("scripts_buildkit_pkg.verify", _VERIFY_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scripts_buildkit_pkg.verify"] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_app(tmp: Path, binaries: list[str], version: str) -> Path:
    app = tmp / "AgentShore.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    for name in binaries:
        (macos / name).write_bytes(b"\x00")
    with (app / "Contents" / "Info.plist").open("wb") as fh:
        plistlib.dump({"CFBundleShortVersionString": version}, fh)
    return app


def test_expected_payload_excludes_provisioner() -> None:
    verify = _load()
    expected = verify.expected_macos_payload(_REPO)
    assert "agentshore-desktop" in expected
    assert "agentshore-bd" in expected
    assert "agentshore-provisioner" not in expected


def test_clean_bundle_passes(tmp_path: Path) -> None:
    verify = _load()
    canonical = verify.read_canonical(_REPO)
    expected = sorted(verify.expected_macos_payload(_REPO))
    app = _make_app(tmp_path, expected, canonical)
    assert verify.verify_macos(app, _REPO, require_signature=False) == []


def test_stray_provisioner_fails(tmp_path: Path) -> None:
    verify = _load()
    canonical = verify.read_canonical(_REPO)
    payload = sorted(verify.expected_macos_payload(_REPO)) + ["agentshore-provisioner"]
    app = _make_app(tmp_path, payload, canonical)
    problems = verify.verify_macos(app, _REPO, require_signature=False)
    assert any("agentshore-provisioner" in p for p in problems), problems


def test_version_mismatch_fails(tmp_path: Path) -> None:
    verify = _load()
    expected = sorted(verify.expected_macos_payload(_REPO))
    app = _make_app(tmp_path, expected, "0.0.0-wrong")
    problems = verify.verify_macos(app, _REPO, require_signature=False)
    assert any("version mismatch" in p for p in problems), problems
