"""Tests for scripts/generate_update_manifest.py."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


def _load_script() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "generate_update_manifest.py"
    spec = importlib.util.spec_from_file_location("generate_update_manifest", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_script()
generate_manifest = _mod.generate_manifest
main = _mod.main


_ALL_SIGS = {
    "darwin-x86_64": "dW5pdHRlc3QtZGFyd2luLXg2NA==",
    "darwin-aarch64": "dW5pdHRlc3QtZGFyd2luLWFhcmNoNjQ=",
    "windows-x86_64": "dW5pdHRlc3Qtd2luZG93cy14NjQ=",
    "linux-x86_64": "dW5pdHRlc3QtbGludXgteA==",
}

_COMMON = {
    "version": "1.2.3",
    "notes": "Bugfixes.",
    "tag": "v1.2.3",
    "pub_date": "2026-01-01T00:00:00Z",
}


class TestGenerateManifest:
    def test_required_top_level_keys(self) -> None:
        manifest = generate_manifest(**_COMMON, signatures=_ALL_SIGS)
        assert set(manifest) >= {"version", "notes", "pub_date", "platforms"}

    def test_version_passthrough(self) -> None:
        manifest = generate_manifest(**_COMMON, signatures=_ALL_SIGS)
        assert manifest["version"] == "1.2.3"

    def test_pub_date_passthrough(self) -> None:
        manifest = generate_manifest(**_COMMON, signatures=_ALL_SIGS)
        assert manifest["pub_date"] == "2026-01-01T00:00:00Z"

    def test_all_four_platforms_present(self) -> None:
        manifest = generate_manifest(**_COMMON, signatures=_ALL_SIGS)
        platforms = manifest["platforms"]
        assert isinstance(platforms, dict)
        assert set(platforms) == {
            "darwin-x86_64",
            "darwin-aarch64",
            "windows-x86_64",
            "linux-x86_64",
        }

    def test_platform_has_url_and_signature(self) -> None:
        manifest = generate_manifest(**_COMMON, signatures=_ALL_SIGS)
        for plat, entry in manifest["platforms"].items():
            assert "url" in entry, f"{plat} missing url"
            assert "signature" in entry, f"{plat} missing signature"

    def test_url_contains_version_and_tag(self) -> None:
        manifest = generate_manifest(**_COMMON, signatures=_ALL_SIGS)
        for plat, entry in manifest["platforms"].items():
            assert "1.2.3" in entry["url"], f"{plat} url missing version"
            assert "v1.2.3" in entry["url"], f"{plat} url missing tag"

    def test_url_points_to_github_releases(self) -> None:
        manifest = generate_manifest(**_COMMON, signatures=_ALL_SIGS)
        for _, entry in manifest["platforms"].items():
            assert entry["url"].startswith(
                "https://github.com/SuperSwinkAI/Swink-AgentShore/releases/download/"
            )

    def test_signature_values_are_preserved(self) -> None:
        manifest = generate_manifest(**_COMMON, signatures=_ALL_SIGS)
        for plat, sig in _ALL_SIGS.items():
            assert manifest["platforms"][plat]["signature"] == sig

    def test_empty_signature_excludes_platform(self) -> None:
        partial = dict(_ALL_SIGS)
        partial["linux-x86_64"] = ""
        manifest = generate_manifest(**_COMMON, signatures=partial)
        assert "linux-x86_64" not in manifest["platforms"]

    def test_all_empty_signatures_produces_empty_platforms(self) -> None:
        empty: dict[str, str] = {k: "" for k in _ALL_SIGS}
        manifest = generate_manifest(**_COMMON, signatures=empty)
        assert manifest["platforms"] == {}

    def test_macos_only_release(self) -> None:
        sigs = {
            "darwin-x86_64": "sig-mac-x64",
            "darwin-aarch64": "sig-mac-arm",
            "windows-x86_64": "",
            "linux-x86_64": "",
        }
        manifest = generate_manifest(**_COMMON, signatures=sigs)
        assert set(manifest["platforms"]) == {"darwin-x86_64", "darwin-aarch64"}


class TestDarwinArtifactNames:
    def test_darwin_x64_artifact_name(self) -> None:
        manifest = generate_manifest(
            **_COMMON,
            signatures={
                "darwin-x86_64": "sig",
                **{k: "" for k in ["darwin-aarch64", "windows-x86_64", "linux-x86_64"]},
            },
        )
        url = manifest["platforms"]["darwin-x86_64"]["url"]
        assert "x64.app.tar.gz" in url

    def test_darwin_aarch64_artifact_name(self) -> None:
        manifest = generate_manifest(
            **_COMMON,
            signatures={
                "darwin-aarch64": "sig",
                **{k: "" for k in ["darwin-x86_64", "windows-x86_64", "linux-x86_64"]},
            },
        )
        url = manifest["platforms"]["darwin-aarch64"]["url"]
        assert "aarch64.app.tar.gz" in url

    def test_windows_x64_artifact_name(self) -> None:
        manifest = generate_manifest(
            **_COMMON,
            signatures={
                "windows-x86_64": "sig",
                **{k: "" for k in ["darwin-x86_64", "darwin-aarch64", "linux-x86_64"]},
            },
        )
        url = manifest["platforms"]["windows-x86_64"]["url"]
        assert "x64-setup.exe" in url

    def test_linux_x64_artifact_name(self) -> None:
        manifest = generate_manifest(
            **_COMMON,
            signatures={
                "linux-x86_64": "sig",
                **{k: "" for k in ["darwin-x86_64", "darwin-aarch64", "windows-x86_64"]},
            },
        )
        url = manifest["platforms"]["linux-x86_64"]["url"]
        assert "amd64.AppImage" in url


class TestMainCLI:
    def test_stdout_output_is_valid_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(
            [
                "--version",
                "2.0.0",
                "--notes",
                "Test release",
                "--tag",
                "v2.0.0",
                "--sig-darwin-x64",
                "test-sig",
                "--pub-date",
                "2026-06-01T00:00:00Z",
            ]
        )
        assert rc == 0
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["version"] == "2.0.0"
        assert "darwin-x86_64" in parsed["platforms"]

    def test_default_pub_date_is_utc_iso8601(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(
            [
                "--version",
                "1.0.0",
                "--notes",
                "Notes",
                "--tag",
                "v1.0.0",
                "--sig-darwin-x64",
                "sig",
            ]
        )
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        pub = parsed["pub_date"]
        assert pub.endswith("Z"), f"pub_date not UTC: {pub}"
        assert "T" in pub

    def test_output_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        out = tmp_path / "latest.json"
        rc = main(
            [
                "--version",
                "1.0.0",
                "--notes",
                "Notes",
                "--tag",
                "v1.0.0",
                "--sig-darwin-x64",
                "sig",
                "--output",
                str(out),
            ]
        )
        assert rc == 0
        assert out.exists()
        parsed = json.loads(out.read_text())
        assert parsed["version"] == "1.0.0"


class TestTauriConfUpdater:
    """Verify tauri.conf.json has the correct updater stanza."""

    def test_updater_plugin_configured(self) -> None:
        conf_path = Path(__file__).parents[2] / "desktop" / "src-tauri" / "tauri.conf.json"
        assert conf_path.exists(), "tauri.conf.json not found"
        conf = json.loads(conf_path.read_text())
        plugins = conf.get("plugins", {})
        assert "updater" in plugins, "updater plugin not in tauri.conf.json"

    def test_updater_has_pubkey(self) -> None:
        conf_path = Path(__file__).parents[2] / "desktop" / "src-tauri" / "tauri.conf.json"
        conf = json.loads(conf_path.read_text())
        pubkey = conf["plugins"]["updater"].get("pubkey", "")
        assert pubkey, "updater pubkey is empty"

    def test_updater_has_github_releases_endpoint(self) -> None:
        conf_path = Path(__file__).parents[2] / "desktop" / "src-tauri" / "tauri.conf.json"
        conf = json.loads(conf_path.read_text())
        endpoints = conf["plugins"]["updater"].get("endpoints", [])
        assert any("github.com" in ep and "latest.json" in ep for ep in endpoints), (
            f"No GitHub Releases latest.json endpoint found: {endpoints}"
        )
