"""Windows desktop build pipeline (`python -m scripts.buildkit windows`).

Windows parity for the macOS pipeline: builds the Tauri exe + the standalone
provisioner crate, signs them (Authenticode, via the _win_signing.ps1 carve-out),
stages the installer payload, and compiles the Inno Setup machine-wide installer.
The cross-platform phases (dashboard/frontend/wheel) come from phases.py.

It is only ever exercised on Windows; this module must still IMPORT on any OS
(no Windows-only Python imports at top level) so the test suite and ruff can
load it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

from . import verify
from ._proc import BuildError, die, fatal, info, log, require_tool, run, run_text
from .context import BuildContext, default_context
from .phases import build_dashboard, build_frontend, build_wheel
from .version import read_canonical

# Pin uv for reproducible installer provisioning.
PINNED_UV_VERSION = "uv 0.8.11"

_SIGN_HELPER = Path(__file__).resolve().parent / "_win_signing.ps1"


def _stage_dir(ctx: BuildContext) -> Path:
    return ctx.tauri_dir / "target" / "windows-installer"


def _windows_tauri_config(ctx: BuildContext) -> Path:
    return ctx.packaging_dir / "windows" / "tauri.windows-installer.conf.json"


def assert_signing_options(args: argparse.Namespace) -> None:
    if args.no_sign and args.self_sign:
        raise die("Use either --no-sign or --self-sign, not both.")
    if args.trust_self_signed and not args.self_sign:
        raise die("--trust-self-signed-certificate requires --self-sign.")
    if args.setup_self_signed_only and not args.self_sign:
        raise die("--setup-self-signed-certificate-only requires --self-sign.")
    if args.self_sign and args.certificate_thumbprint:
        raise die("Use either --self-sign or --certificate-thumbprint, not both.")


def _signing_params(args: argparse.Namespace) -> list[str]:
    params: list[str] = []
    if args.self_sign:
        params.append("-SelfSign")
    if args.trust_self_signed:
        params.append("-TrustSelfSignedCertificate")
    if args.self_signed_subject:
        params += ["-SelfSignedCertificateSubject", args.self_signed_subject]
    if args.sign_tool:
        params += ["-SignTool", args.sign_tool]
    if args.certificate_thumbprint:
        params += ["-CertificateThumbprint", args.certificate_thumbprint]
    if args.timestamp_url:
        params += ["-TimestampUrl", args.timestamp_url]
    return params


def _powershell() -> str:
    return require_tool("powershell", "this command must run on Windows")


def setup_self_signed_cert(args: argparse.Namespace) -> None:
    run(
        [
            _powershell(),
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(_SIGN_HELPER),
            "-Action",
            "SetupCert",
            *_signing_params(args),
        ]
    )
    log("Self-signed certificate setup complete")


def _sign_file(file: Path, args: argparse.Namespace) -> int:
    """Sign a file via the helper. Returns 0 (signed), 2 (no cert/tool), or raises."""
    result = subprocess.run(
        [
            _powershell(),
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(_SIGN_HELPER),
            "-Action",
            "Sign",
            "-File",
            str(file),
            *_signing_params(args),
        ]
    )
    if result.returncode not in (0, 2):
        raise die(f"signing failed for {file} (exit {result.returncode})")
    return result.returncode


def _sign_required_or_fail(file: Path, args: argparse.Namespace, *, required: bool) -> None:
    code = _sign_file(file, args)
    if code == 2 and required:
        raise die(
            "Release Windows builds must be Authenticode-signed to reduce SmartScreen/AV "
            "heuristics. Install signtool.exe and a current-user code-signing certificate, "
            "pass --certificate-thumbprint, or intentionally pass --no-sign for local testing."
        )
    if code == 2:
        log("Skipping Authenticode signing for debug build (no signtool/cert)")


def resolve_uv() -> str:
    uv = require_tool("uv", f"install {PINNED_UV_VERSION} and retry")
    version = run_text([uv, "--version"]).strip()
    if not version.startswith(PINNED_UV_VERSION):
        raise die(
            f"Expected {PINNED_UV_VERSION} for reproducible Windows installer "
            f"provisioning, got '{version}'."
        )
    info(f"Using uv: {uv} ({version})")
    return uv


def stop_processes() -> None:
    log("Stopping running AgentShore desktop processes")
    taskkill = shutil.which("taskkill")
    if not taskkill:
        return
    for name in ("AgentShore.exe", "agentshore-desktop.exe"):
        subprocess.run(
            [taskkill, "/F", "/IM", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _cargo_env(args: argparse.Namespace, **extra: str) -> dict[str, str]:
    env = {**os.environ, **extra}
    if args.allow_no_revocation_check:
        # Avast HTTPS scanning breaks cargo TLS cert revocation (Schannel).
        env["CARGO_HTTP_CHECK_REVOKE"] = "false"
    return env


def build_tauri_exe(ctx: BuildContext, args: argparse.Namespace) -> Path:
    log(f"Building Tauri executable ({ctx.build_mode})")
    _clear_stale_setups(ctx)
    npx = require_tool("npx", "install Node.js (npx)")
    cmd = [npx, "tauri", "build"]
    if ctx.build_mode == "debug":
        cmd.append("--debug")
    # Windows provisions bd at install time, so build the exe with externalBin:[]
    # (the windows config) and skip bundling.
    cmd += ["--no-bundle", "--config", str(_windows_tauri_config(ctx)), "--", "--locked"]
    run(cmd, cwd=ctx.desktop_dir, env=_cargo_env(args, AGENTSHORE_SKIP_BD_SIDECAR="1"))
    app_exe = ctx.target_dir / "agentshore-desktop.exe"
    if not app_exe.is_file():
        raise die(f"Tauri build finished but {app_exe} does not exist")
    return app_exe


def build_provisioner(ctx: BuildContext, args: argparse.Namespace) -> Path:
    log(f"Building Windows provisioner ({ctx.build_mode})")
    cmd = ["cargo", "build"]
    if ctx.build_mode == "release":
        cmd.append("--release")
    cmd += ["-p", "agentshore-provisioner", "--locked"]
    run(cmd, cwd=ctx.tauri_dir, env=_cargo_env(args))
    provisioner_exe = ctx.target_dir / "agentshore-provisioner.exe"
    if not provisioner_exe.is_file():
        raise die(f"Provisioner build finished but {provisioner_exe} does not exist")
    return provisioner_exe


def _clear_stale_setups(ctx: BuildContext) -> None:
    output_dir = ctx.desktop_dir / "dist"
    if not output_dir.is_dir():
        return
    for setup in output_dir.glob("AgentShoreSetup-*.exe"):
        try:
            setup.unlink()
            info(f"Removed stale setup artifact: {setup.name}")
        except OSError as err:
            raise die(
                f"Could not remove stale setup artifact {setup}. Close any open installer "
                f"windows or security-scanner handles, then retry. {err}"
            ) from err


def stage_payload(ctx: BuildContext, app_exe: Path, provisioner_exe: Path, uv: str) -> None:
    log("Staging installer payload")
    assert ctx.bundled_wheel is not None
    stage = _stage_dir(ctx)
    app_stage = stage / "app"
    installer_stage = stage / "installer"
    output_dir = ctx.desktop_dir / "dist"
    shutil.rmtree(stage, ignore_errors=True)
    app_stage.mkdir(parents=True, exist_ok=True)
    installer_stage.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy(app_exe, app_stage / "agentshore-desktop.exe")
    shutil.copy(ctx.bundled_wheel, installer_stage / ctx.bundled_wheel.name)
    shutil.copy(provisioner_exe, installer_stage / "agentshore-provisioner.exe")
    shutil.copy(uv, installer_stage / "uv.exe")


def verify_payload(ctx: BuildContext, args: argparse.Namespace) -> None:
    """Post-stage verification gate — the Windows analogue of macos.verify_app.

    Runs against the staged app/installer directories (before compile_inno wraps
    them), the equivalent phase position to macOS's post-Tauri-build, pre-.pkg
    gate. See verify.verify_windows for what it checks and how it differs from
    the macOS gate.
    """
    log("Verifying staged installer payload")
    stage = _stage_dir(ctx)
    require_signature = not args.no_sign and ctx.build_mode != "debug"
    problems = verify.verify_windows(
        stage / "app", stage / "installer", ctx.root, require_signature=require_signature
    )
    if problems:
        for problem in problems:
            info(f"  - {problem}")
        raise die("artifact verification failed — see problems above")
    info("Verification OK")


def run_eula_generator(ctx: BuildContext, uv: str) -> None:
    log("Regenerating EULA.rtf from LICENSE")
    builder = ctx.packaging_dir / "installer-resources" / "build-eula-rtf.py"
    license_source = ctx.root / "LICENSE"
    license_path = ctx.packaging_dir / "installer-resources" / "EULA.rtf"
    if not builder.is_file():
        raise die(f"EULA generator missing: {builder}")
    if not license_source.is_file():
        raise die(f"LICENSE missing: {license_source}")
    run([uv, "--native-tls", "run", "python", str(builder), str(license_source), str(license_path)])


def resolve_iscc(args: argparse.Namespace) -> str:
    if args.iscc:
        path = Path(args.iscc)
        if not path.is_file():
            raise die(f"ISCC.exe not found: {args.iscc}")
        return str(path.resolve())
    found = shutil.which("iscc.exe") or shutil.which("iscc")
    if found:
        return found
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Inno Setup 6" / "ISCC.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise die("Inno Setup 6 compiler not found. Install Inno Setup 6 or pass --iscc <path>.")


def compile_inno(ctx: BuildContext, args: argparse.Namespace) -> Path:
    log("Compiling Inno Setup installer")
    iscc = resolve_iscc(args)
    version = read_canonical(ctx.root)
    assert ctx.bundled_wheel is not None
    stage = _stage_dir(ctx)
    output_dir = ctx.desktop_dir / "dist"
    template = ctx.packaging_dir / "windows" / "AgentShore.iss.in"
    license_path = ctx.packaging_dir / "installer-resources" / "EULA.rtf"
    icon = ctx.tauri_dir / "icons" / "icon.ico"

    iss_out = stage / "AgentShore.iss"
    shutil.copy(template, iss_out)
    run(
        [
            iscc,
            f"/DAppVersion={version}",
            f"/DStageDir={stage}",
            f"/DOutputDir={output_dir}",
            f"/DWheelFileName={ctx.bundled_wheel.name}",
            "/DUvFileName=uv.exe",
            "/DProvisionerFileName=agentshore-provisioner.exe",
            f"/DLicenseFile={license_path}",
            f"/DIconFile={icon}",
            str(iss_out),
        ]
    )
    setup_out = output_dir / f"AgentShoreSetup-{version}-x64.exe"
    if not setup_out.is_file():
        raise die(f"Inno Setup completed but expected installer is missing: {setup_out}")
    return setup_out


def run_windows(ctx: BuildContext, args: argparse.Namespace) -> None:
    assert_signing_options(args)
    if args.setup_self_signed_only:
        setup_self_signed_cert(args)
        return

    uv = resolve_uv()
    stop_processes()
    build_dashboard(ctx)
    log("Skipping bundled bd sidecar binary")
    info("Windows installer provisions bd during install via the managed sidecar venv.")
    build_frontend(ctx)
    build_wheel(ctx, uv_global_args=("--native-tls",))

    app_exe = build_tauri_exe(ctx, args)
    provisioner_exe = build_provisioner(ctx, args)

    if not args.no_sign:
        required = ctx.build_mode != "debug"
        _sign_required_or_fail(app_exe, args, required=required)
        _sign_required_or_fail(provisioner_exe, args, required=required)
    else:
        log("Skipping Authenticode signing (--no-sign)")

    stage_payload(ctx, app_exe, provisioner_exe, uv)
    verify_payload(ctx, args)
    run_eula_generator(ctx, uv)
    setup_out = compile_inno(ctx, args)

    if not args.no_sign:
        _sign_file(setup_out, args)  # best-effort; exe/provisioner already gated above

    log("Build complete")
    info(f"Installer: {setup_out}")

    if args.install:
        log("Launching installer")
        subprocess.run([str(setup_out)])


def parse_args(argv: list[str]) -> tuple[BuildContext, argparse.Namespace]:
    parser = argparse.ArgumentParser(
        prog="buildkit windows",
        description="Build the AgentShore Windows Inno Setup installer.",
    )
    parser.add_argument(
        "--skip-dashboard", action="store_true", help="reuse dashboard build outputs"
    )
    parser.add_argument("--debug", action="store_true", help="debug build instead of release")
    parser.add_argument(
        "--install", action="store_true", help="launch the installer after building"
    )
    parser.add_argument("--iscc", default="", help="explicit path to ISCC.exe")
    parser.add_argument("--no-sign", action="store_true", help="skip Authenticode signing")
    parser.add_argument("--self-sign", action="store_true", help="use a local self-signed dev cert")
    parser.add_argument(
        "--trust-self-signed-certificate",
        dest="trust_self_signed",
        action="store_true",
        help="trust the self-signed cert in CurrentUser\\Root (requires --self-sign)",
    )
    parser.add_argument(
        "--setup-self-signed-certificate-only",
        dest="setup_self_signed_only",
        action="store_true",
        help="create/trust the self-signed cert and exit (requires --self-sign)",
    )
    parser.add_argument(
        "--self-signed-subject",
        dest="self_signed_subject",
        default="CN=AgentShore Local Dev Code Signing",
        help="subject for the local dev self-signed certificate",
    )
    parser.add_argument(
        "--sign-tool", dest="sign_tool", default="", help="explicit path to signtool.exe"
    )
    parser.add_argument(
        "--certificate-thumbprint",
        dest="certificate_thumbprint",
        default="",
        help="SHA-1 thumbprint of the Authenticode code-signing certificate",
    )
    parser.add_argument(
        "--timestamp-url",
        dest="timestamp_url",
        default="http://timestamp.digicert.com",
        help="RFC 3161 timestamp server URL",
    )
    parser.add_argument(
        "--allow-no-revocation-check",
        dest="allow_no_revocation_check",
        action="store_true",
        help="disable cargo TLS revocation checks (Avast HTTPS scanning machines only)",
    )
    args = parser.parse_args(argv)

    ctx = default_context()
    ctx.build_mode = "debug" if args.debug else "release"
    ctx.skip_dashboard = args.skip_dashboard
    ctx.no_sign = args.no_sign
    return ctx, args


def main(argv: list[str] | None = None) -> int:
    ctx, args = parse_args(argv or [])
    try:
        run_windows(ctx, args)
    except BuildError as error:
        return fatal(error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
