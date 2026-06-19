"""macOS desktop build pipeline (`python -m scripts.buildkit macos`).

Builds AgentShore's package layout (dashboard + sidecar + Tauri 2 shell), signs
the .app and .pkg with any available Developer ID certs, verifies the artifact,
and reveals the .pkg in Finder. The cross-platform phases live in phases.py, the
artifact gate in verify.py.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from . import verify
from ._proc import BuildError, die, fatal, info, log, require_tool, run, run_ok, run_text
from .context import APP_BUNDLE_ID, APP_NAME, BuildContext, default_context
from .phases import build_dashboard, build_frontend, build_wheel
from .version import read_canonical

_COMPONENT_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<array>
    <dict>
        <key>BundleHasStrictIdentifier</key>
        <true/>
        <key>BundleIsRelocatable</key>
        <false/>
        <key>BundleIsVersionChecked</key>
        <true/>
        <key>BundleOverwriteAction</key>
        <string>upgrade</string>
        <key>RootRelativeBundlePath</key>
        <string>{app_name}.app</string>
    </dict>
</array>
</plist>
"""

_PROC_NAMES = ("AgentShore Desktop", "agentshore-desktop")
_PROC_PATTERNS = ("agentshore.sidecar", "agentshore dashboard")


def _first_identity(output: str, prefix: str) -> str:
    """First quoted identity name on a `security find-identity` line (awk -F'\"' $2)."""
    for line in output.splitlines():
        if prefix in line:
            parts = line.split('"')
            if len(parts) >= 2:
                return parts[1]
    return ""


# ── 1. Stop running processes ────────────────────────────────────────────────
def stop_processes(ctx: BuildContext) -> None:
    log("Stopping running AgentShore processes")
    killed = False
    for proc in _PROC_NAMES:
        if run_ok(["pgrep", "-ix", proc]) and run_ok(["killall", "-TERM", proc]):
            info(f"SIGTERM → {proc}")
            killed = True
    for pattern in _PROC_PATTERNS:
        if run_ok(["pgrep", "-f", pattern]) and run_ok(["pkill", "-TERM", "-f", pattern]):
            info(f"SIGTERM → {pattern}")
            killed = True
    if killed:
        time.sleep(1)
    for proc in ("AgentShore Desktop", "agentshore-desktop"):
        if run_ok(["killall", "-KILL", proc]):
            info(f"SIGKILL → {proc}")
    for pattern in _PROC_PATTERNS:
        run_ok(["pkill", "-KILL", "-f", pattern])


# ── 2. Clean stale files ─────────────────────────────────────────────────────
def clean_stale(ctx: BuildContext) -> None:
    log("Cleaning stale files")
    sessions = Path.home() / ".agentshore" / "sessions"
    if sessions.is_dir():
        for pattern in ("socket.sock", "dashboard.pid", "agentshore.pid"):
            for path in sessions.rglob(pattern):
                path.unlink(missing_ok=True)
        info("Removed stale sockets + PID files under ~/.agentshore/sessions/")

    bundle = ctx.built_app
    if bundle.is_dir():
        try:
            shutil.rmtree(bundle)
            info("Removed old bundle at target/.../bundle/macos/")
        except OSError:
            # Tauri signs via the codesign daemon (root), so the bundle ends up
            # root-owned. Authenticated GUI delete — no TTY sudo needed.
            script = f"do shell script \"rm -rf '{bundle}'\" with administrator privileges"
            if run_ok(["osascript", "-e", script]):
                info("Removed old bundle (authenticated via GUI)")
            else:
                print("    warning: could not remove old bundle — build may fail", file=sys.stderr)

    installed = Path("/Applications") / f"{APP_NAME}.app"
    if installed.is_dir() and ctx.do_install and run_ok(["sudo", "rm", "-rf", str(installed)]):
        info("Removed installed app from /Applications/")

    # Best-effort pre-install cleanup: -n so it never blocks on a password prompt
    # (forgets only when sudo creds are already cached; harmless to skip otherwise).
    receipts = run_text(["pkgutil", "--pkgs"], check=False).split()
    for receipt in (APP_BUNDLE_ID, "ai.agentshore.app", "ai.agentshore.cli"):
        if receipt in receipts and run_ok(["sudo", "-n", "pkgutil", "--forget", receipt]):
            info(f"Forgot pkg receipt: {receipt}")


# ── 4. Bundled bd sidecar ────────────────────────────────────────────────────
def build_sidecar(ctx: BuildContext) -> None:
    if ctx.skip_sidecar:
        log("Skipping sidecar binary build (--skip-sidecar)")
        return
    log("Building bundled bd sidecar binary")
    npm = require_tool("npm", "install Node.js (npm)")
    run([npm, "run", "build:tauri-sidecars"], cwd=ctx.desktop_dir)


# ── 6. Resolve code-signing identity ─────────────────────────────────────────
def resolve_signing_identity(ctx: BuildContext) -> None:
    if ctx.no_sign:
        log("Skipping code-signing identity resolution (--no-sign)")
        return
    log("Resolving macOS code-signing identity")
    output = run_text(["security", "find-identity", "-v", "-p", "codesigning"], check=False)
    identity = _first_identity(output, "Developer ID Application:")
    if identity:
        ctx.app_signing_id = identity
        os.environ["APPLE_SIGNING_IDENTITY"] = identity
        info(f"Identity: {identity}")
    else:
        info("No 'Developer ID Application' cert in Keychain — building unsigned")
        info("Install a Developer ID cert to enable signing, or pass --no-sign to silence this")


# ── 7. Tauri app bundle ──────────────────────────────────────────────────────
def build_tauri(ctx: BuildContext) -> None:
    log(f"Building Tauri app bundle ({ctx.build_mode})")
    npx = require_tool("npx", "install Node.js (npx)")
    cmd = [npx, "tauri", "build"]
    if ctx.build_mode == "debug":
        cmd.append("--debug")
    cmd += ["--", "--locked"]
    run(cmd, cwd=ctx.desktop_dir)
    if not ctx.built_app.is_dir():
        raise die(f"Tauri build finished but {ctx.built_app} does not exist")
    info(f"Bundle ready at {ctx.built_app}")


# ── 8. Verify the .app artifact ──────────────────────────────────────────────
def verify_app(ctx: BuildContext) -> None:
    log("Verifying .app artifact")
    problems = verify.verify_macos(ctx.built_app, ctx.root, require_signature=ctx.is_signed)
    if problems:
        for problem in problems:
            info(f"  - {problem}")
        raise die("artifact verification failed — see problems above")
    info("Verification OK")


def _raise_pkg_timeout(pkg: Path, expanded: Path) -> None:
    """Expand → bump postinstall timeout 600→3600 → flatten (slow venv installs)."""
    shutil.rmtree(expanded, ignore_errors=True)
    run(["pkgutil", "--expand", str(pkg), str(expanded)])
    package_info = expanded / "PackageInfo"
    package_info.write_text(package_info.read_text().replace('timeout="600"', 'timeout="3600"'))
    run(["pkgutil", "--flatten", str(expanded), str(pkg)])
    shutil.rmtree(expanded, ignore_errors=True)


def _stage_scripts(stage: Path, files: list[tuple[Path, str]], executable: set[str]) -> None:
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    for src, name in files:
        shutil.copy(src, stage / name)
        if name in executable:
            os.chmod(stage / name, 0o755)


# ── 9. Wrap .app in .pkg installer ───────────────────────────────────────────
def build_pkg(ctx: BuildContext) -> None:
    if not ctx.build_pkg:
        return
    log("Building .pkg installer")
    require_tool("pkgbuild", "install Xcode Command Line Tools")
    require_tool("productbuild", "install Xcode Command Line Tools")
    assert ctx.bundled_wheel is not None  # set by build_wheel
    wheel = ctx.bundled_wheel
    version = read_canonical(ctx.root)

    installer_scripts = ctx.packaging_dir / "installer-scripts"
    installer_resources = ctx.packaging_dir / "installer-resources"
    distribution_template = ctx.packaging_dir / "Distribution.xml.in"
    (ctx.desktop_dir / "dist").mkdir(parents=True, exist_ok=True)

    component_dir = ctx.tauri_dir / "target" / "pkg-component"
    shutil.rmtree(component_dir, ignore_errors=True)
    component_dir.mkdir(parents=True, exist_ok=True)

    # — Desktop component (BundleIsRelocatable=false; see original script note) —
    app_component_pkg = component_dir / "agentshore-desktop-component.pkg"
    app_component_plist = component_dir / "agentshore-desktop-component.plist"
    app_component_plist.write_text(_COMPONENT_PLIST.format(app_name=APP_NAME))
    app_pkg_args = [
        "pkgbuild",
        "--root", str(ctx.built_app.parent),
        "--component-plist", str(app_component_plist),
        "--install-location", "/Applications",
        "--identifier", APP_BUNDLE_ID,
        "--version", version,
    ]
    if installer_scripts.is_dir():
        scripts_stage = ctx.tauri_dir / "target" / "pkg-scripts"
        _stage_scripts(
            scripts_stage,
            [
                (installer_scripts / "postinstall", "postinstall"),
                (ctx.root / "scripts" / "install-agentshore-venv.sh", "install-agentshore-venv.sh"),
                (wheel, wheel.name),
            ],
            {"postinstall", "install-agentshore-venv.sh"},
        )
        app_pkg_args += ["--scripts", str(scripts_stage)]
        info(f"Desktop scripts: {scripts_stage} (postinstall provisions venv from bundled wheel)")
    run([*app_pkg_args, str(app_component_pkg)])
    _raise_pkg_timeout(app_component_pkg, component_dir / "agentshore-desktop-expanded")
    info(f"Wrote desktop component pkg: {app_component_pkg}")

    # — CLI component (nopayload) —
    cli_component_pkg = component_dir / "agentshore-cli-component.pkg"
    cli_scripts = ctx.tauri_dir / "target" / "pkg-cli-scripts"
    _stage_scripts(
        cli_scripts,
        [
            (installer_scripts / "cli-postinstall", "postinstall"),
            (ctx.root / "scripts" / "install-agentshore-cli.sh", "install-agentshore-cli.sh"),
            (wheel, wheel.name),
        ],
        {"postinstall", "install-agentshore-cli.sh"},
    )
    run([
        "pkgbuild", "--nopayload", "--scripts", str(cli_scripts),
        "--identifier", "ai.agentshore.cli", "--version", version, str(cli_component_pkg),
    ])
    info(f"Wrote CLI component pkg: {cli_component_pkg}")

    # — Timelapse component (nopayload, opt-in) —
    timelapse_component_pkg = component_dir / "agentshore-timelapse-component.pkg"
    timelapse_scripts = ctx.tauri_dir / "target" / "pkg-timelapse-scripts"
    _stage_scripts(
        timelapse_scripts,
        [
            (installer_scripts / "timelapse-postinstall", "postinstall"),
            (ctx.root / "scripts" / "install-timelapse.sh", "install-timelapse.sh"),
        ],
        {"postinstall", "install-timelapse.sh"},
    )
    run([
        "pkgbuild", "--nopayload", "--scripts", str(timelapse_scripts),
        "--identifier", "ai.agentshore.timelapse", "--version", version,
        str(timelapse_component_pkg),
    ])
    _raise_pkg_timeout(timelapse_component_pkg, component_dir / "agentshore-timelapse-expanded")
    info(f"Wrote timelapse component pkg: {timelapse_component_pkg}")

    # — productbuild distribution —
    if not distribution_template.is_file():
        raise die(f"Distribution template missing: {distribution_template}")
    eula_builder = installer_resources / "build-eula-rtf.sh"
    if os.access(eula_builder, os.X_OK):
        info("Regenerating EULA.rtf from LICENSE")
        run_text([str(eula_builder)])
    if not (installer_resources / "EULA.rtf").is_file():
        raise die(f"EULA.rtf missing: {installer_resources / 'EULA.rtf'}")

    distribution_xml = component_dir / "Distribution.xml"
    rendered = (
        distribution_template.read_text()
        .replace("@VERSION@", version)
        .replace("@APP_COMPONENT_PKG@", app_component_pkg.name)
        .replace("@TIMELAPSE_COMPONENT_PKG@", timelapse_component_pkg.name)
        .replace("@CLI_COMPONENT_PKG@", cli_component_pkg.name)
    )
    distribution_xml.write_text(rendered)
    info(f"Rendered distribution: {distribution_xml}")

    installer_id = ""
    if not ctx.no_sign:
        installer_id = _first_identity(
            run_text(["security", "find-identity", "-v"], check=False), "Developer ID Installer:"
        )
    ctx.installer_signing_id = installer_id
    pb_args = [
        "productbuild",
        "--distribution", str(distribution_xml),
        "--resources", str(installer_resources),
        "--package-path", str(component_dir),
    ]
    if installer_id:
        pb_args += ["--sign", installer_id]
        info(f"Installer signing identity: {installer_id}")
    elif ctx.notarize:
        raise die("No 'Developer ID Installer' cert in Keychain — required for --notarize")
    else:
        info("No 'Developer ID Installer' cert found — producing unsigned .pkg")

    pkg_out = ctx.desktop_dir / "dist" / f"{APP_NAME}.pkg"
    run([*pb_args, str(pkg_out)])
    info(f"Wrote {pkg_out}")


# ── 10. Notarize ─────────────────────────────────────────────────────────────
def notarize(ctx: BuildContext) -> None:
    if not ctx.notarize:
        return
    log(f"Notarizing .pkg (keychain profile: {ctx.keychain_profile})")
    require_tool("xcrun", "install Xcode Command Line Tools")
    pkg_out = ctx.desktop_dir / "dist" / f"{APP_NAME}.pkg"
    run([
        "xcrun", "notarytool", "submit", str(pkg_out),
        "--keychain-profile", ctx.keychain_profile, "--wait",
    ])
    run(["xcrun", "stapler", "staple", str(pkg_out)])
    info(f"Notarized + stapled {pkg_out}")


# ── 11. Install ──────────────────────────────────────────────────────────────
def install(ctx: BuildContext) -> None:
    if not ctx.do_install:
        return
    log("Installing to /Applications/")
    pkg_out = ctx.desktop_dir / "dist" / f"{APP_NAME}.pkg"
    if ctx.build_pkg:
        run(["sudo", "installer", "-pkg", str(pkg_out), "-target", "/"])
        info(f"Installed from {pkg_out}")
    else:
        installed = Path("/Applications") / f"{APP_NAME}.app"
        run(["sudo", "cp", "-R", str(ctx.built_app), str(installed)])
        info(f"Copied .app to {installed}")


# ── 12. Reveal ───────────────────────────────────────────────────────────────
def reveal(ctx: BuildContext) -> None:
    log("Build complete")
    info(f".app: {ctx.built_app}")
    pkg_out = ctx.desktop_dir / "dist" / f"{APP_NAME}.pkg"
    if ctx.build_pkg:
        info(f".pkg: {pkg_out}")
        run_ok(["open", "-R", str(pkg_out)])
    else:
        run_ok(["open", "-R", str(ctx.built_app)])


def run_macos(ctx: BuildContext) -> None:
    if ctx.notarize and not ctx.build_pkg:
        raise die("--notarize requires the .pkg (do not pass --no-pkg with --notarize)")
    stop_processes(ctx)
    clean_stale(ctx)
    build_dashboard(ctx)
    build_sidecar(ctx)
    build_frontend(ctx)
    build_wheel(ctx)
    resolve_signing_identity(ctx)
    build_tauri(ctx)
    verify_app(ctx)
    build_pkg(ctx)
    notarize(ctx)
    install(ctx)
    reveal(ctx)


def parse_args(argv: list[str]) -> BuildContext:
    parser = argparse.ArgumentParser(
        prog="buildkit macos",
        description="Build the AgentShore macOS .app/.dmg/.pkg.",
    )
    parser.add_argument("--skip-dashboard", action="store_true", help="reuse dashboard/dist")
    parser.add_argument("--skip-sidecar", action="store_true", help="reuse staged bd sidecar")
    parser.add_argument("--debug", action="store_true", help="debug build instead of release")
    parser.add_argument("--install", action="store_true", help="also install to /Applications")
    parser.add_argument("--no-sign", action="store_true", help="skip Developer ID signing")
    parser.add_argument("--no-pkg", action="store_true", help="skip .pkg wrap (.app + .dmg only)")
    parser.add_argument("--notarize", action="store_true", help="notarize + staple the .pkg")
    parser.add_argument(
        "--keychain-profile", default="agentshore-notary", help="notarytool keychain profile"
    )
    args = parser.parse_args(argv)

    ctx = default_context()
    ctx.build_mode = "debug" if args.debug else "release"
    ctx.skip_dashboard = args.skip_dashboard
    ctx.skip_sidecar = args.skip_sidecar
    ctx.do_install = args.install
    ctx.no_sign = args.no_sign
    ctx.build_pkg = not args.no_pkg
    ctx.notarize = args.notarize
    ctx.keychain_profile = args.keychain_profile
    return ctx


def main(argv: list[str] | None = None) -> int:
    ctx = parse_args(argv or [])
    try:
        run_macos(ctx)
    except BuildError as error:
        return fatal(error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
