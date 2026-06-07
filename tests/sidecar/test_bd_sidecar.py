from __future__ import annotations

import hashlib
import io
import os
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO_ROOT / "packaging" / "desktop" / "build_bd_sidecar.py"


def _load_build_module() -> ModuleType:
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
    assert "/no/such/path" in captured.err


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


def test_pinned_version_matches_runtime_pin() -> None:
    """The bundled bd version must equal the runtime pin (no silent drift)."""
    from agentshore.beads.setup import REQUIRED_BD_VERSION

    build_bd_sidecar = _load_build_module()
    assert build_bd_sidecar.PINNED_BD_VERSION == REQUIRED_BD_VERSION


def test_pinned_checksums_reference_pinned_version() -> None:
    """Every checksum row must belong to the pinned version (stale-row guard)."""
    build_bd_sidecar = _load_build_module()
    version = build_bd_sidecar.PINNED_BD_VERSION
    assert build_bd_sidecar.PINNED_CHECKSUMS, "checksum table must not be empty"
    for asset in build_bd_sidecar.PINNED_CHECKSUMS:
        assert f"_{version}_" in asset, f"{asset} is not for pinned version {version}"


def test_release_asset_name_maps_host() -> None:
    build_bd_sidecar = _load_build_module()
    assert (
        build_bd_sidecar._release_asset_name("1.0.4", "Darwin", "arm64")
        == "beads_1.0.4_darwin_arm64.tar.gz"
    )
    assert (
        build_bd_sidecar._release_asset_name("1.0.4", "Linux", "x86_64")
        == "beads_1.0.4_linux_amd64.tar.gz"
    )
    assert (
        build_bd_sidecar._release_asset_name("1.0.4", "Windows", "amd64")
        == "beads_1.0.4_windows_amd64.zip"
    )
    with pytest.raises(SystemExit):
        build_bd_sidecar._release_asset_name("1.0.4", "Plan9", "sparc")


def _fake_bd_targz(payload: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("./bd")
        info.size = len(payload)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def test_default_path_downloads_verifies_and_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_bd_sidecar = _load_build_module()

    payload = b"#!/bin/sh\necho pinned-bd\n"
    archive = _fake_bd_targz(payload)
    asset = build_bd_sidecar._release_asset_name(
        build_bd_sidecar.PINNED_BD_VERSION, "Darwin", "arm64"
    )

    monkeypatch.setattr(build_bd_sidecar.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(build_bd_sidecar.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(build_bd_sidecar, "_download", lambda _url: archive)
    monkeypatch.setitem(
        build_bd_sidecar.PINNED_CHECKSUMS, asset, hashlib.sha256(archive).hexdigest()
    )

    tmp_out = tmp_path / "out"
    rc = build_bd_sidecar.main(["--out", str(tmp_out), "--target-triple", "aarch64-apple-darwin"])

    target = tmp_out / "agentshore-bd" / "agentshore-bd"
    assert rc == 0
    assert target.read_bytes() == payload
    assert os.access(target, os.X_OK)


def test_default_path_rejects_checksum_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_bd_sidecar = _load_build_module()

    archive = _fake_bd_targz(b"tampered")
    asset = build_bd_sidecar._release_asset_name(
        build_bd_sidecar.PINNED_BD_VERSION, "Darwin", "arm64"
    )

    monkeypatch.setattr(build_bd_sidecar.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(build_bd_sidecar.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(build_bd_sidecar, "_download", lambda _url: archive)
    monkeypatch.setitem(build_bd_sidecar.PINNED_CHECKSUMS, asset, "00" * 32)

    with pytest.raises(SystemExit, match="Checksum mismatch"):
        build_bd_sidecar.main(
            ["--out", str(tmp_path / "out"), "--target-triple", "aarch64-apple-darwin"]
        )
