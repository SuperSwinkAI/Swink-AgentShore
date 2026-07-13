from __future__ import annotations

import hashlib
import io
import os
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO_ROOT / "packaging" / "desktop" / "build_bd_sidecar.py"

# Arbitrary version for consent-gate tests; only echoed into instructions, never matched.
_PINNED = "1.1.0"


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
    # Script prints the resolved absolute path (Windows: D:\no\such\path), so resolve first.
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


def test_pinned_version_matches_runtime_pin() -> None:
    """The bundled bd version must equal the runtime pin (no silent drift)."""
    from agentshore.beads.setup import REQUIRED_BD_VERSION

    build_bd_sidecar = _load_build_module()
    assert build_bd_sidecar.PINNED_BD_VERSION == REQUIRED_BD_VERSION


def test_provision_bd_noop_when_already_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """When bd already resolves, provision is a no-op returning the existing path
    (no download, no raise)."""
    from agentshore.beads import downloader

    monkeypatch.setattr("agentshore.beads.resolve_bd_binary", lambda: "/usr/bin/bd")
    assert downloader.provision_bd(_PINNED) == "/usr/bin/bd"  # must not raise/download


def test_provision_bd_headless_without_opt_in_raises_with_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headless + no opt-in must fail conservatively with install instructions —
    never auto-download a third-party binary in CI/agent/server contexts."""
    import types

    from agentshore.beads import downloader

    monkeypatch.setattr("agentshore.beads.resolve_bd_binary", lambda: None)
    monkeypatch.delenv("AGENTSHORE_AUTO_INSTALL_BD", raising=False)
    monkeypatch.setattr(downloader.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))

    with pytest.raises(RuntimeError, match="bd binary was not found"):
        downloader.provision_bd(_PINNED)


def test_provision_bd_opt_in_downloads_into_dest_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With the explicit opt-in (or a consented caller), provision downloads the
    pinned release into dest_dir and returns the installed path."""
    from agentshore.beads import downloader

    monkeypatch.setattr("agentshore.beads.resolve_bd_binary", lambda: None)
    monkeypatch.setenv("AGENTSHORE_AUTO_INSTALL_BD", "1")

    captured: dict[str, object] = {}

    def _fake_download(version: str, asset: str, kind: str, *, dest_dir: Path) -> str:
        captured["version"] = version
        captured["dest_dir"] = dest_dir
        installed = dest_dir / ("bd.exe" if sys.platform.startswith("win") else "bd")
        return str(installed)

    monkeypatch.setattr(downloader, "_download_bd", _fake_download)

    dest = tmp_path / "bin"
    result = downloader.provision_bd(_PINNED, dest_dir=dest)

    assert captured["version"] == _PINNED
    assert captured["dest_dir"] == dest
    assert result is not None and result.endswith(
        "bd.exe" if sys.platform.startswith("win") else "bd"
    )


def test_provision_bd_assume_yes_consents_without_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A consented caller (the installer) passes assume_yes=True and downloads
    even without the opt-in env var set."""
    from agentshore.beads import downloader

    monkeypatch.setattr("agentshore.beads.resolve_bd_binary", lambda: None)
    monkeypatch.delenv("AGENTSHORE_AUTO_INSTALL_BD", raising=False)

    def _fake_download(version: str, asset: str, kind: str, *, dest_dir: Path) -> str:
        return str(dest_dir / "bd")

    monkeypatch.setattr(downloader, "_download_bd", _fake_download)

    result = downloader.provision_bd(_PINNED, assume_yes=True, dest_dir=tmp_path)
    assert result == str(tmp_path / "bd")


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
        build_bd_sidecar._release_asset_name("1.1.0", "Darwin", "arm64")
        == "beads_1.1.0_darwin_arm64.tar.gz"
    )
    assert (
        build_bd_sidecar._release_asset_name("1.1.0", "Linux", "x86_64")
        == "beads_1.1.0_linux_amd64.tar.gz"
    )
    assert (
        build_bd_sidecar._release_asset_name("1.1.0", "Windows", "amd64")
        == "beads_1.1.0_windows_amd64.zip"
    )
    with pytest.raises(SystemExit):
        build_bd_sidecar._release_asset_name("1.1.0", "Plan9", "sparc")


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
