"""Tests for the Tauri signing configuration and hardened-runtime entitlements."""

from __future__ import annotations

import json
import plistlib
from pathlib import Path
from typing import Any, cast

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TAURI_DIR = _REPO_ROOT / "desktop" / "src-tauri"
_TAURI_CONF = _TAURI_DIR / "tauri.conf.json"
_ENTITLEMENTS = _TAURI_DIR / "entitlements.plist"


_REQUIRED_ENTITLEMENT_KEYS = (
    "com.apple.security.cs.allow-jit",
    "com.apple.security.cs.allow-unsigned-executable-memory",
    "com.apple.security.cs.disable-library-validation",
    "com.apple.security.cs.allow-dyld-environment-variables",
)


@pytest.fixture(scope="module")
def tauri_conf() -> dict[str, Any]:
    with _TAURI_CONF.open() as fh:
        return cast("dict[str, Any]", json.load(fh))


def test_macos_bundle_block_has_required_keys(tauri_conf: dict[str, Any]) -> None:
    bundle = tauri_conf["bundle"]
    macos = bundle["macOS"]
    assert "signingIdentity" in macos, "bundle.macOS.signingIdentity is required"
    assert "entitlements" in macos, "bundle.macOS.entitlements is required"
    assert "minimumSystemVersion" in macos, "bundle.macOS.minimumSystemVersion is required"


def test_windows_bundle_block_has_required_keys(tauri_conf: dict[str, Any]) -> None:
    bundle = tauri_conf["bundle"]
    windows = bundle["windows"]
    assert "certificateThumbprint" in windows
    assert windows.get("digestAlgorithm") == "sha256"
    assert "timestampUrl" in windows
    assert windows["timestampUrl"], "timestampUrl must be a non-empty string default"
    assert windows.get("tsp") is False


def test_entitlements_file_exists_and_parses(tauri_conf: dict[str, Any]) -> None:
    bundle = tauri_conf["bundle"]
    macos = bundle["macOS"]
    rel = macos["entitlements"]
    assert rel, "bundle.macOS.entitlements must point at a plist file"
    resolved = (_TAURI_DIR / rel).resolve()
    assert resolved.exists(), f"entitlements file missing: {resolved}"
    assert resolved.suffix == ".plist"
    with resolved.open("rb") as fh:
        data = plistlib.load(fh)
    assert isinstance(data, dict)


def test_entitlements_contains_required_keys() -> None:
    with _ENTITLEMENTS.open("rb") as fh:
        data = plistlib.load(fh)
    for key in _REQUIRED_ENTITLEMENT_KEYS:
        assert data.get(key) is True, f"entitlement {key} must be present and true"


def test_updater_pubkey_placeholder_preserved(tauri_conf: dict[str, Any]) -> None:
    updater = tauri_conf["plugins"]["updater"]
    assert updater["pubkey"] == "TAURI_SIGNING_PUBLIC_KEY"
    assert updater["endpoints"], "updater endpoints must remain configured"
