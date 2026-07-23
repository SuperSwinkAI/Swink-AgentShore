"""Post-build artifact verification gate.

Run after packaging to make a "false green" impossible: assert the built
artifact contains *exactly* the expected payload (no stale/stray binaries — the
class of bug where a stale agentshore-provisioner got folded into the macOS
.app), that it is validly signed when signing was requested, and that the
embedded version matches the canonical source version.

The expected payload is *derived* from the Tauri config + Cargo manifest rather
than hardcoded, so it stays correct as binaries are added/renamed:
  expected(Contents/MacOS) = {<app bin name>} ∪ {basename(b) for b in externalBin}

CLI: `python -m scripts.buildkit verify --target macos --app <AgentShore.app>
      [--require-signature]`
      `python -m scripts.buildkit verify --target windows --app-dir <stage>/app
      --installer-dir <stage>/installer [--require-signature]`

Windows asymmetry: unlike macOS's single `.app` bundle, the Windows pipeline
(`windows.py:stage_payload`) stages two directories (an "app" dir with the
compiled exe, an "installer" dir with the provisioner/uv/wheel), so
`verify_windows` takes both instead of one path. It also can't check a PE
version resource cross-platform (no `pefile` dependency, no Windows host in
this test suite) or verify Authenticode signatures off a Windows host, so its
version check targets the staged wheel filename instead, and its signature
check is a no-op when `signtool.exe` isn't on PATH (see `verify_windows`).
"""

from __future__ import annotations

import argparse
import json
import plistlib
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from .version import read_canonical, repo_root

_TAURI_CONF = "desktop/src-tauri/tauri.conf.json"
_APP_CARGO = "desktop/src-tauri/Cargo.toml"
_PROVISIONER_CARGO = "desktop/src-tauri/provisioner/Cargo.toml"


def _diff_manifest(actual: set[str], expected: set[str], *, component: str) -> list[str]:
    """Pure exact-match diff: any stray or missing file in a payload directory."""
    problems: list[str] = []
    for stray in sorted(actual - expected):
        problems.append(f"unexpected binary in {component}: {stray!r} (stale/stray payload)")
    for missing in sorted(expected - actual):
        problems.append(f"missing expected binary in {component}: {missing!r}")
    return problems


def expected_macos_payload(root: Path) -> set[str]:
    """The exact file set Contents/MacOS must contain, derived from config."""
    cargo = tomllib.loads((root / _APP_CARGO).read_text(encoding="utf-8"))
    app_bin = cargo["package"]["name"]  # e.g. "agentshore-desktop"
    conf = json.loads((root / _TAURI_CONF).read_text(encoding="utf-8"))
    external = conf.get("bundle", {}).get("externalBin", []) or []
    payload = {app_bin}
    # "binaries/agentshore-bd/agentshore-bd" -> "agentshore-bd"
    payload.update(Path(b).name for b in external)
    return payload


def expected_windows_app_payload(root: Path) -> set[str]:
    """The exact file the staged Windows app directory (`stage/app`) must contain."""
    cargo = tomllib.loads((root / _APP_CARGO).read_text(encoding="utf-8"))
    app_bin = cargo["package"]["name"]  # "agentshore-desktop"
    return {f"{app_bin}.exe"}


def expected_windows_installer_payload(root: Path) -> set[str]:
    """The exact non-wheel files the staged installer directory (`stage/installer`)
    must contain. The bundled wheel is checked separately (`_wheel_version_problems`)
    since its filename is version-dependent."""
    cargo = tomllib.loads((root / _PROVISIONER_CARGO).read_text(encoding="utf-8"))
    provisioner_bin = cargo["package"]["name"]  # "agentshore-provisioner"
    return {f"{provisioner_bin}.exe", "uv.exe"}


def _wheel_version_problems(installer_files: set[str], canonical: str) -> list[str]:
    """Pure check: exactly one staged wheel, named with the canonical version.

    Windows exes don't carry an easily-parsed cross-platform version resource the
    way macOS's Info.plist does, so the staged wheel filename —
    `agentshore-<version>-py3-none-any.whl`, produced by `phases.build_wheel` from
    the same canonical `pyproject.toml` — is the version-bearing artifact this
    gate checks instead.
    """
    wheels = sorted(f for f in installer_files if f.endswith(".whl"))
    if not wheels:
        return ["missing bundled wheel in installer stage"]
    if len(wheels) > 1:
        return [
            f"multiple wheel files staged in installer stage: {wheels} "
            "(stale wheel from a previous build?)"
        ]
    expected_name = f"agentshore-{canonical}-py3-none-any.whl"
    if wheels[0] != expected_name:
        return [
            f"version mismatch: staged wheel is {wheels[0]!r}, expected {expected_name!r} "
            f"(canonical pyproject.toml version is {canonical})"
        ]
    return []


def _macos_embedded_version(app: Path) -> str:
    plist_path = app / "Contents" / "Info.plist"
    with plist_path.open("rb") as fh:
        plist = plistlib.load(fh)
    version = plist.get("CFBundleShortVersionString")
    if not isinstance(version, str) or not version:
        raise ValueError(f"{plist_path}: missing/empty CFBundleShortVersionString")
    return version


def verify_macos(app: Path, root: Path, *, require_signature: bool) -> list[str]:
    """Return a list of human-readable problems (empty list == pass)."""
    problems: list[str] = []
    macos_dir = app / "Contents" / "MacOS"
    if not macos_dir.is_dir():
        return [f"{macos_dir} does not exist — not a valid .app bundle"]

    # 1. Payload manifest — exact match, so any stray binary (e.g. a stale
    #    agentshore-provisioner) or a missing expected binary fails the build.
    expected = expected_macos_payload(root)
    actual = {p.name for p in macos_dir.iterdir() if p.is_file()}
    problems.extend(_diff_manifest(actual, expected, component="Contents/MacOS"))

    # 2. Embedded version must match the canonical source version.
    canonical = read_canonical(root)
    try:
        embedded = _macos_embedded_version(app)
    except (OSError, ValueError) as err:
        problems.append(str(err))
    else:
        if embedded != canonical:
            problems.append(
                f"version mismatch: bundle CFBundleShortVersionString is {embedded}, "
                f"canonical (pyproject.toml) is {canonical}"
            )

    # 3. Signature — only when the build was signed (release/distribution).
    if require_signature:
        result = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            problems.append(
                "codesign --verify failed:\n" + (result.stderr.strip() or result.stdout.strip())
            )

    return problems


def verify_windows(
    app_dir: Path, installer_dir: Path, root: Path, *, require_signature: bool
) -> list[str]:
    """Return a list of human-readable problems (empty list == pass).

    Mirrors `verify_macos`'s three checks (manifest, version, signature) against
    the two directories `windows.py:stage_payload` produces. See the module
    docstring for why the version and signature checks differ from macOS.
    """
    if not app_dir.is_dir():
        return [f"{app_dir} does not exist — staged Windows app payload missing"]
    if not installer_dir.is_dir():
        return [f"{installer_dir} does not exist — staged Windows installer payload missing"]

    problems: list[str] = []

    # 1. App-stage manifest — exact match against the compiled desktop exe.
    app_expected = expected_windows_app_payload(root)
    app_actual = {p.name for p in app_dir.iterdir() if p.is_file()}
    problems.extend(_diff_manifest(app_actual, app_expected, component="app stage"))

    # 2. Installer-stage manifest — provisioner + uv are exact names; the bundled
    #    wheel is version-dependent, so it's excluded here and checked in step 3.
    installer_expected = expected_windows_installer_payload(root)
    installer_actual = {p.name for p in installer_dir.iterdir() if p.is_file()}
    non_wheel_actual = {name for name in installer_actual if not name.endswith(".whl")}
    problems.extend(
        _diff_manifest(non_wheel_actual, installer_expected, component="installer stage")
    )

    # 3. Version — see _wheel_version_problems for why the wheel filename, not a
    #    PE version resource, is the checked artifact.
    canonical = read_canonical(root)
    problems.extend(_wheel_version_problems(installer_actual, canonical))

    # 4. Signature — best-effort. Unlike `codesign`, there's no cross-platform way
    #    to verify an Authenticode signature; `signtool.exe` only exists on a
    #    Windows host with the Windows SDK installed. When it's absent (macOS/CI-
    #    linux, including this test suite) this step is a silent no-op rather than
    #    a false failure — the real check only ever runs on the Windows build host.
    if require_signature:
        signtool = shutil.which("signtool")
        if signtool is not None:
            app_bin = next(iter(app_expected))
            provisioner_bin = next(name for name in installer_expected if name.endswith(".exe"))
            for name, directory in ((app_bin, app_dir), (provisioner_bin, installer_dir)):
                target = directory / name
                if not target.is_file():
                    continue  # already reported as missing in step 1/2
                result = subprocess.run(
                    [signtool, "verify", "/pa", "/v", str(target)],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    problems.append(
                        f"signtool verify failed for {target}:\n"
                        + (result.stderr.strip() or result.stdout.strip())
                    )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify",
        description="Post-build artifact verification gate (manifest + version + signature).",
    )
    parser.add_argument(
        "--target",
        required=True,
        choices=["macos", "windows"],
        help="platform of the artifact under test",
    )
    parser.add_argument("--app", type=Path, help="path to the built AgentShore.app (macOS)")
    parser.add_argument(
        "--app-dir", type=Path, help="path to the staged Windows app directory (stage/app)"
    )
    parser.add_argument(
        "--installer-dir",
        type=Path,
        help="path to the staged Windows installer directory (stage/installer)",
    )
    parser.add_argument(
        "--require-signature",
        action="store_true",
        help="fail unless the artifact carries a valid signature (use for signed builds)",
    )
    args = parser.parse_args(argv)
    root = repo_root()

    if args.target == "macos":
        if args.app is None:
            parser.error("--app is required for --target macos")
        if not args.app.is_dir():
            print(f"verify: {args.app} does not exist", file=sys.stderr)
            return 1
        problems = verify_macos(args.app, root, require_signature=args.require_signature)
        artifact_desc = str(args.app)
    else:
        if args.app_dir is None or args.installer_dir is None:
            parser.error("--app-dir and --installer-dir are required for --target windows")
        if not args.app_dir.is_dir() or not args.installer_dir.is_dir():
            print(
                f"verify: staged directory missing ({args.app_dir}, {args.installer_dir})",
                file=sys.stderr,
            )
            return 1
        problems = verify_windows(
            args.app_dir, args.installer_dir, root, require_signature=args.require_signature
        )
        artifact_desc = f"{args.app_dir}, {args.installer_dir}"

    if problems:
        print(f"Artifact verification FAILED ({len(problems)} problem(s)):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"Artifact verification OK: {artifact_desc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
