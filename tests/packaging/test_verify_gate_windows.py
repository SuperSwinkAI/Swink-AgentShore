"""Guard: the Windows post-build verification gate catches a stale/stray staged
payload, a stale wheel, or a missing file — the Windows analogue of
test_verify_gate.py's macOS coverage.

Exercises scripts/buildkit/verify.py's verify_windows against synthetic staged
`app`/`installer` directories (what windows.py:stage_payload produces), without
a real Windows build.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parents[2]
_VERIFY_PATH = _REPO / "scripts" / "buildkit" / "verify.py"


def _load() -> ModuleType:
    # Load as part of the scripts.buildkit package so its relative imports resolve.
    pkg_spec = importlib.util.spec_from_file_location(
        "scripts_buildkit_pkg4",
        _REPO / "scripts" / "buildkit" / "__init__.py",
        submodule_search_locations=[str(_REPO / "scripts" / "buildkit")],
    )
    assert pkg_spec is not None and pkg_spec.loader is not None
    pkg = importlib.util.module_from_spec(pkg_spec)
    sys.modules["scripts_buildkit_pkg4"] = pkg
    pkg_spec.loader.exec_module(pkg)

    spec = importlib.util.spec_from_file_location("scripts_buildkit_pkg4.verify", _VERIFY_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scripts_buildkit_pkg4.verify"] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_stage(
    tmp: Path, *, app_files: list[str], installer_files: list[str]
) -> tuple[Path, Path]:
    app_dir = tmp / "app"
    installer_dir = tmp / "installer"
    app_dir.mkdir(parents=True)
    installer_dir.mkdir(parents=True)
    for name in app_files:
        (app_dir / name).write_bytes(b"\x00")
    for name in installer_files:
        (installer_dir / name).write_bytes(b"\x00")
    return app_dir, installer_dir


def test_expected_windows_payloads_derive_from_cargo_manifests() -> None:
    verify = _load()
    assert verify.expected_windows_app_payload(_REPO) == {"agentshore-desktop.exe"}
    assert verify.expected_windows_installer_payload(_REPO) == {
        "agentshore-provisioner.exe",
        "uv.exe",
    }


def test_clean_stage_passes(tmp_path: Path) -> None:
    verify = _load()
    canonical = verify.read_canonical(_REPO)
    app_dir, installer_dir = _make_stage(
        tmp_path,
        app_files=["agentshore-desktop.exe"],
        installer_files=[
            "agentshore-provisioner.exe",
            "uv.exe",
            f"agentshore-{canonical}-py3-none-any.whl",
        ],
    )
    assert (
        verify.verify_windows(app_dir, installer_dir, _REPO, require_signature=False) == []
    )


def test_stray_binary_in_app_stage_fails(tmp_path: Path) -> None:
    verify = _load()
    canonical = verify.read_canonical(_REPO)
    app_dir, installer_dir = _make_stage(
        tmp_path,
        app_files=["agentshore-desktop.exe", "agentshore-github-helper.exe"],
        installer_files=[
            "agentshore-provisioner.exe",
            "uv.exe",
            f"agentshore-{canonical}-py3-none-any.whl",
        ],
    )
    problems = verify.verify_windows(app_dir, installer_dir, _REPO, require_signature=False)
    assert any("agentshore-github-helper.exe" in p for p in problems), problems


def test_missing_provisioner_in_installer_stage_fails(tmp_path: Path) -> None:
    verify = _load()
    canonical = verify.read_canonical(_REPO)
    app_dir, installer_dir = _make_stage(
        tmp_path,
        app_files=["agentshore-desktop.exe"],
        installer_files=["uv.exe", f"agentshore-{canonical}-py3-none-any.whl"],
    )
    problems = verify.verify_windows(app_dir, installer_dir, _REPO, require_signature=False)
    assert any("agentshore-provisioner.exe" in p for p in problems), problems


def test_missing_wheel_fails(tmp_path: Path) -> None:
    verify = _load()
    app_dir, installer_dir = _make_stage(
        tmp_path,
        app_files=["agentshore-desktop.exe"],
        installer_files=["agentshore-provisioner.exe", "uv.exe"],
    )
    problems = verify.verify_windows(app_dir, installer_dir, _REPO, require_signature=False)
    assert any("missing bundled wheel" in p for p in problems), problems


def test_multiple_wheels_fails(tmp_path: Path) -> None:
    verify = _load()
    canonical = verify.read_canonical(_REPO)
    app_dir, installer_dir = _make_stage(
        tmp_path,
        app_files=["agentshore-desktop.exe"],
        installer_files=[
            "agentshore-provisioner.exe",
            "uv.exe",
            f"agentshore-{canonical}-py3-none-any.whl",
            "agentshore-0.0.1-py3-none-any.whl",
        ],
    )
    problems = verify.verify_windows(app_dir, installer_dir, _REPO, require_signature=False)
    assert any("multiple wheel files" in p for p in problems), problems


def test_stale_wheel_version_fails(tmp_path: Path) -> None:
    verify = _load()
    app_dir, installer_dir = _make_stage(
        tmp_path,
        app_files=["agentshore-desktop.exe"],
        installer_files=[
            "agentshore-provisioner.exe",
            "uv.exe",
            "agentshore-0.0.0-wrong-py3-none-any.whl",
        ],
    )
    problems = verify.verify_windows(app_dir, installer_dir, _REPO, require_signature=False)
    assert any("version mismatch" in p for p in problems), problems


def test_missing_app_stage_directory_fails(tmp_path: Path) -> None:
    verify = _load()
    installer_dir = tmp_path / "installer"
    installer_dir.mkdir()
    problems = verify.verify_windows(
        tmp_path / "app", installer_dir, _REPO, require_signature=False
    )
    assert len(problems) == 1
    assert "does not exist" in problems[0]


def test_missing_installer_stage_directory_fails(tmp_path: Path) -> None:
    verify = _load()
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    problems = verify.verify_windows(
        app_dir, tmp_path / "installer", _REPO, require_signature=False
    )
    assert len(problems) == 1
    assert "does not exist" in problems[0]


def test_require_signature_without_signtool_on_path_is_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best-effort signature check: no signtool.exe on this (non-Windows) test host,
    so require_signature=True must not fail the otherwise-clean stage."""
    verify = _load()
    canonical = verify.read_canonical(_REPO)
    app_dir, installer_dir = _make_stage(
        tmp_path,
        app_files=["agentshore-desktop.exe"],
        installer_files=[
            "agentshore-provisioner.exe",
            "uv.exe",
            f"agentshore-{canonical}-py3-none-any.whl",
        ],
    )
    monkeypatch.setattr(verify.shutil, "which", lambda name: None)
    problems = verify.verify_windows(app_dir, installer_dir, _REPO, require_signature=True)
    assert problems == []
