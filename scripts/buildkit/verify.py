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
"""

from __future__ import annotations

import argparse
import json
import plistlib
import subprocess
import sys
import tomllib
from pathlib import Path

from .version import read_canonical, repo_root

_TAURI_CONF = "desktop/src-tauri/tauri.conf.json"
_APP_CARGO = "desktop/src-tauri/Cargo.toml"


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
    for stray in sorted(actual - expected):
        problems.append(f"unexpected binary in Contents/MacOS: {stray!r} (stale/stray payload)")
    for missing in sorted(expected - actual):
        problems.append(f"missing expected binary in Contents/MacOS: {missing!r}")

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
    else:  # windows verification not yet ported
        print("verify: --target windows not yet implemented", file=sys.stderr)
        return 2

    if problems:
        print(f"Artifact verification FAILED ({len(problems)} problem(s)):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"Artifact verification OK: {args.app}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
