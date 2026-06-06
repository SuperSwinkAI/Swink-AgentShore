from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO_ROOT / "packaging" / "desktop" / "build_bd_sidecar.py"


def _load_build_module() -> object:
    import importlib.util

    spec = importlib.util.spec_from_file_location("agentshore_build_bd_sidecar", BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_bd_sidecar_copies_binary(tmp_path: Path) -> None:
    build_bd_sidecar = _load_build_module()

    tmp_bd = tmp_path / "bd"
    tmp_out = tmp_path / "out"
    tmp_bd.write_bytes(b"#!/bin/sh\necho ok\n")
    tmp_bd.chmod(0o755)

    rc = build_bd_sidecar.main(
        ["--bd", str(tmp_bd), "--out", str(tmp_out), "--target-triple", "x86_64-unknown-linux-gnu"]
    )

    target = tmp_out / "agentshore-bd" / "agentshore-bd"
    target_with_triple = tmp_out / "agentshore-bd" / "agentshore-bd-x86_64-unknown-linux-gnu"
    assert rc == 0
    assert target.exists()
    assert target_with_triple.exists()
    assert os.access(target, os.X_OK)
    assert target.read_bytes() == tmp_bd.read_bytes()
    assert target_with_triple.read_bytes() == tmp_bd.read_bytes()


def test_build_bd_sidecar_rejects_missing_binary(capsys: pytest.CaptureFixture[str]) -> None:
    build_bd_sidecar = _load_build_module()

    with pytest.raises(SystemExit):
        build_bd_sidecar.main(["--bd", "/no/such/path"])

    captured = capsys.readouterr()
    # The script prints the resolved absolute path; on Windows that becomes
    # e.g. D:\no\such\path, so compare against the same resolution.
    assert str(Path("/no/such/path").resolve()) in captured.err


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="no POSIX exec bit on Windows; os.access(X_OK) is True for any readable file",
)
def test_build_bd_sidecar_rejects_non_executable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build_bd_sidecar = _load_build_module()

    tmp_bd = tmp_path / "bd"
    tmp_bd.write_text("echo nope\n", encoding="utf-8")
    tmp_bd.chmod(0o644)

    with pytest.raises(SystemExit):
        build_bd_sidecar.main(["--bd", str(tmp_bd)])

    captured = capsys.readouterr()
    assert "not executable" in captured.err


def test_build_bd_sidecar_uses_exe_suffix_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_bd_sidecar = _load_build_module()

    tmp_bd = tmp_path / "bd"
    tmp_out = tmp_path / "out"
    tmp_bd.write_bytes(b"#!/bin/sh\necho ok\n")
    tmp_bd.chmod(0o755)

    monkeypatch.setattr(build_bd_sidecar.sys, "platform", "win32")

    rc = build_bd_sidecar.main(
        ["--bd", str(tmp_bd), "--out", str(tmp_out), "--target-triple", "x86_64-pc-windows-msvc"]
    )

    target = tmp_out / "agentshore-bd" / "agentshore-bd.exe"
    target_with_triple = tmp_out / "agentshore-bd" / "agentshore-bd-x86_64-pc-windows-msvc.exe"
    assert rc == 0
    assert target.exists()
    assert target_with_triple.exists()
