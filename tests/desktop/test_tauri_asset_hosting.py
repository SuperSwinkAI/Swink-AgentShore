"""Tests for Tauri asset hosting + WebView origin (DESIGN §3.3)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TAURI_DIR = _REPO_ROOT / "desktop" / "src-tauri"
_TAURI_CONF = _TAURI_DIR / "tauri.conf.json"
_DESKTOP_DIR = _REPO_ROOT / "desktop"
_DESKTOP_PKG = _DESKTOP_DIR / "package.json"
_PREPARE_SCRIPT = _DESKTOP_DIR / "scripts" / "prepare-tauri-assets.mjs"
_DASHBOARD_INDEX = _REPO_ROOT / "dashboard" / "index.html"
_DASHBOARD_STATIC = _REPO_ROOT / "src" / "agentshore" / "dashboard" / "static"


@pytest.fixture(scope="module")
def tauri_conf() -> dict[str, Any]:
    with _TAURI_CONF.open() as fh:
        return cast("dict[str, Any]", json.load(fh))


@pytest.fixture(scope="module")
def desktop_pkg() -> dict[str, Any]:
    with _DESKTOP_PKG.open() as fh:
        return cast("dict[str, Any]", json.load(fh))


def _parse_csp(csp: str) -> dict[str, list[str]]:
    directives: dict[str, list[str]] = {}
    for chunk in csp.split(";"):
        parts = chunk.strip().split()
        if not parts:
            continue
        directives[parts[0]] = parts[1:]
    return directives


def test_csp_is_set_and_restricts_to_self(tauri_conf: dict[str, Any]) -> None:
    csp = tauri_conf["app"]["security"]["csp"]
    assert isinstance(csp, str) and csp.strip(), "CSP must be a non-empty string"
    directives = _parse_csp(csp)
    assert "default-src" in directives, "CSP must declare default-src"
    assert "'self'" in directives["default-src"], "default-src must include 'self'"


def test_csp_allows_only_self_and_websocket_for_connect_src(
    tauri_conf: dict[str, Any],
) -> None:
    """connect-src must reach the sidecar via WebSocket only; no remote http/https."""
    csp = tauri_conf["app"]["security"]["csp"]
    directives = _parse_csp(csp)
    assert "connect-src" in directives, "CSP must declare connect-src"
    connect = directives["connect-src"]
    assert "'self'" in connect
    # WebSocket sources are required for the dashboard sidecar bridge.
    ws_sources = [t for t in connect if t in ("ws:", "wss:") or t.startswith(("ws://", "wss://"))]
    assert ws_sources, f"connect-src must allow at least one WebSocket source; got {connect}"
    # Each WebSocket source must be loopback-only — the sidecar binds 127.0.0.1
    # on a dynamic port, never a remote host.
    for token in ws_sources:
        assert token not in ("ws:", "wss:"), (
            f"connect-src must not allow the bare {token!r} scheme; "
            "restrict WebSocket sources to localhost/127.0.0.1"
        )
        assert "localhost" in token or "127.0.0.1" in token, (
            f"WebSocket source {token!r} must be loopback-only (localhost/127.0.0.1)"
        )
    # No remote HTTP fetches — design §3.3 says only WebSocket goes to the
    # Python sidecar; everything else is offline. Tauri's own IPC channel
    # (http://ipc.localhost) is the only http source allowed.
    forbidden = {"http:", "https:", "*"}
    leaks = [token for token in connect if token in forbidden]
    assert not leaks, f"connect-src must not allow remote HTTP origins: {leaks}"
    http_sources = [t for t in connect if t.startswith(("http://", "https://"))]
    for token in http_sources:
        assert token in ("http://ipc.localhost", "http://asset.localhost"), (
            f"connect-src http source {token!r} must be a Tauri-internal origin only"
        )


def test_csp_allows_tauri_ipc_in_connect_src(tauri_conf: dict[str, Any]) -> None:
    """Tauri 2 invoke() requires `ipc:` and `http://ipc.localhost` to be reachable."""
    csp = tauri_conf["app"]["security"]["csp"]
    directives = _parse_csp(csp)
    connect = directives["connect-src"]
    assert "ipc:" in connect, "connect-src must include `ipc:` for Tauri 2 invoke()"
    assert "http://ipc.localhost" in connect, (
        "connect-src must include `http://ipc.localhost` for the Windows webview IPC bridge"
    )


def test_csp_script_src_disallows_inline_and_eval(tauri_conf: dict[str, Any]) -> None:
    """script-src must not permit 'unsafe-inline' or 'unsafe-eval' in release."""
    csp = tauri_conf["app"]["security"]["csp"]
    directives = _parse_csp(csp)
    assert "script-src" in directives, "CSP must declare script-src explicitly"
    script = directives["script-src"]
    assert "'self'" in script
    assert "'unsafe-inline'" not in script, "script-src must not allow inline scripts"
    assert "'unsafe-eval'" not in script, "script-src must not allow eval()"
    assert "*" not in script
    assert "data:" not in script
    assert "blob:" not in script


def test_csp_hardening_directives_present(tauri_conf: dict[str, Any]) -> None:
    """CSP must include the standard hardening directives expected before a signed release."""
    csp = tauri_conf["app"]["security"]["csp"]
    directives = _parse_csp(csp)
    # Block legacy plugin embeds — there is no Flash/Java content in this app.
    assert directives.get("object-src") == ["'none'"], (
        "CSP must set `object-src 'none'` to block legacy plugin embeds"
    )
    # Prevent <base> tag hijacking that could rewrite relative URLs.
    assert directives.get("base-uri") == ["'self'"], "CSP must set `base-uri 'self'`"
    # Restrict where forms can post — the desktop shell never submits forms cross-origin.
    assert directives.get("form-action") == ["'self'"], "CSP must set `form-action 'self'`"
    # Block the app from being embedded by another frame (clickjacking).
    assert directives.get("frame-ancestors") == ["'none'"], (
        "CSP must set `frame-ancestors 'none'` to prevent clickjacking"
    )


def test_asset_protocol_enabled_with_resource_scope(tauri_conf: dict[str, Any]) -> None:
    security = tauri_conf["app"]["security"]
    assert "assetProtocol" in security, "assetProtocol block is required"
    asset = security["assetProtocol"]
    assert asset.get("enable") is True, "asset protocol must be enabled"
    scope = asset.get("scope")
    assert isinstance(scope, list) and scope, "asset protocol scope must be a non-empty list"
    # Scope entries must be confined to bundle resources, never the whole filesystem.
    for entry in scope:
        assert isinstance(entry, str)
        assert "$RESOURCE" in entry, (
            f"asset protocol scope entry {entry!r} must be confined to $RESOURCE"
        )


def test_dashboard_static_is_bundled_as_resource(tauri_conf: dict[str, Any]) -> None:
    bundle = tauri_conf["bundle"]
    resources = bundle.get("resources")
    assert resources, "bundle.resources must include the dashboard static dir"

    src_targets: list[tuple[str, str]] = []
    if isinstance(resources, dict):
        src_targets = [(src, dst) for src, dst in resources.items()]
    elif isinstance(resources, list):
        src_targets = [(entry, entry) for entry in resources]
    else:
        pytest.fail(f"bundle.resources must be a list or object, got {type(resources)!r}")

    matched = [
        (src, dst)
        for src, dst in src_targets
        if "src/agentshore/dashboard/static" in src.replace("\\", "/")
    ]
    assert matched, (
        "bundle.resources must reference src/agentshore/dashboard/static so the dashboard "
        "build output is copied into Tauri resources (DESIGN §3.3)"
    )


def test_before_build_command_runs_dashboard_prep(tauri_conf: dict[str, Any]) -> None:
    cmd = tauri_conf["build"]["beforeBuildCommand"]
    assert isinstance(cmd, str) and cmd.strip(), "beforeBuildCommand must be set"
    # The command must invoke the script that builds the dashboard package and
    # copies it into desktop/dist so Tauri can bundle the assets.
    assert re.search(r"(prepare:tauri-assets|build:tauri-frontend)", cmd), (
        f"beforeBuildCommand must invoke the dashboard-asset prep step; got {cmd!r}"
    )


def test_desktop_package_exposes_prepare_script(desktop_pkg: dict[str, Any]) -> None:
    scripts = desktop_pkg.get("scripts", {})
    assert "prepare:tauri-assets" in scripts, (
        "desktop/package.json must expose a 'prepare:tauri-assets' script"
    )
    assert "build:tauri-frontend" in scripts, (
        "desktop/package.json must expose a 'build:tauri-frontend' script that "
        "wraps the Vite build with dashboard-asset staging"
    )
    assert "node scripts/prepare-tauri-assets.mjs" in scripts["prepare:tauri-assets"]


def test_prepare_tauri_assets_script_exists() -> None:
    assert _PREPARE_SCRIPT.exists(), (
        f"missing dashboard-asset prep script at {_PREPARE_SCRIPT.relative_to(_REPO_ROOT)}"
    )
    body = _PREPARE_SCRIPT.read_text(encoding="utf-8")
    # The script must build the dashboard package and stage its static dir
    # into the desktop dist for Tauri bundling. Sprite PNGs are emitted by
    # the desktop Vite build via Rollup's new URL(path, import.meta.url)
    # pattern — no manual lib-asset copy step needed.
    assert "dashboard" in body
    assert "static" in body
    assert "dist" in body


def test_dashboard_static_dir_exists_for_bundle_resource() -> None:
    """The path referenced by bundle.resources must exist in the repo.

    Tauri's bundler fails the build if a resource glob points at a missing
    directory. The dashboard build keeps this directory populated; we assert it
    exists at rest so that a fresh checkout (after `npm --prefix dashboard run
    build`) bundles successfully.
    """
    # We don't require contents at rest — only that the directory tree exists,
    # since CI runs the dashboard build before packaging.
    assert _DASHBOARD_STATIC.exists(), (
        f"dashboard static dir missing: {_DASHBOARD_STATIC.relative_to(_REPO_ROOT)}"
    )
    assert _DASHBOARD_STATIC.is_dir()


def test_dashboard_entry_mounts_only_shared_react_surface() -> None:
    body = _DASHBOARD_INDEX.read_text(encoding="utf-8")
    assert 'id="react-root"' in body
    assert 'src="/src/main.ts"' in body
    assert "topbar-left-mount" not in body
    assert 'id="hud"' not in body
