"""Generate latest.json update manifest for the Tauri auto-updater.

The Tauri updater fetches this manifest from:
  https://github.com/SuperSwinkAI/Swink-AgentShore/releases/latest/download/latest.json

Upload the generated file to each GitHub Release so existing installs can
discover new versions automatically.

Usage::

    python scripts/generate_update_manifest.py \\
        --version 1.0.0 \\
        --notes "Bug fixes and performance improvements." \\
        --tag v1.0.0 \\
        --sig-darwin-x64 "$(cat AgentShore.Desktop_1.0.0_x64.app.tar.gz.sig)" \\
        --sig-darwin-aarch64 "$(cat AgentShore.Desktop_1.0.0_aarch64.app.tar.gz.sig)" \\
        --sig-windows-x64 "$(cat AgentShore.Desktop_1.0.0_x64-setup.exe.sig)" \\
        --sig-linux-x64 "$(cat AgentShore.Desktop_1.0.0_amd64.AppImage.sig)" \\
        --output latest.json

Signatures are produced by ``tauri signer sign``::

    tauri signer sign \\
        --private-key "$TAURI_SIGNING_PRIVATE_KEY" \\
        <artifact-path>

The private key ($TAURI_SIGNING_PRIVATE_KEY) lives in the CI secret store.
The matching public key is embedded in desktop/src-tauri/tauri.conf.json
under plugins.updater.pubkey (set TAURI_SIGNING_PUBLIC_KEY env var in CI to
substitute the placeholder value at build time).

Generate a fresh keypair with::

    tauri signer generate -w ~/.tauri/agentshore.key
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO = "SuperSwinkAI/Swink-AgentShore"
_BASE_URL = f"https://github.com/{_REPO}/releases/download"

_PLATFORM_META: dict[str, dict[str, str]] = {
    "darwin-x86_64": {
        "artifact": "AgentShore.Desktop_{version}_x64.app.tar.gz",
    },
    "darwin-aarch64": {
        "artifact": "AgentShore.Desktop_{version}_aarch64.app.tar.gz",
    },
    "windows-x86_64": {
        "artifact": "AgentShore.Desktop_{version}_x64-setup.exe",
    },
    "linux-x86_64": {
        "artifact": "AgentShore.Desktop_{version}_amd64.AppImage",
    },
}


def _artifact_url(tag: str, version: str, platform: str) -> str:
    artifact = _PLATFORM_META[platform]["artifact"].format(version=version)
    return f"{_BASE_URL}/{tag}/{artifact}"


def generate_manifest(
    *,
    version: str,
    notes: str,
    tag: str,
    pub_date: str,
    signatures: dict[str, str],
) -> dict[str, object]:
    """Return a Tauri v2 update manifest dict.

    Only platforms that have a non-empty signature entry are included so a
    partial release (e.g. macOS-only) produces a valid manifest.
    """
    platforms: dict[str, dict[str, str]] = {}
    for platform, sig in signatures.items():
        if sig:
            platforms[platform] = {
                "url": _artifact_url(tag, version, platform),
                "signature": sig,
            }

    return {
        "version": version,
        "notes": notes,
        "pub_date": pub_date,
        "platforms": platforms,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate latest.json for the Tauri auto-updater.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", required=True, help="Semver version string (e.g. 1.2.3)")
    parser.add_argument("--notes", required=True, help="Release notes (markdown supported)")
    parser.add_argument("--tag", required=True, help="Git tag (e.g. v1.2.3)")
    parser.add_argument(
        "--pub-date",
        default=None,
        help="ISO-8601 publication date; defaults to current UTC time",
    )
    parser.add_argument("--sig-darwin-x64", default="", metavar="SIG", dest="sig_darwin_x64")
    parser.add_argument(
        "--sig-darwin-aarch64", default="", metavar="SIG", dest="sig_darwin_aarch64"
    )
    parser.add_argument("--sig-windows-x64", default="", metavar="SIG", dest="sig_windows_x64")
    parser.add_argument("--sig-linux-x64", default="", metavar="SIG", dest="sig_linux_x64")
    parser.add_argument(
        "--output",
        default="-",
        help="Output path; use - (default) to write to stdout",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    pub_date = args.pub_date or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    signatures: dict[str, str] = {
        "darwin-x86_64": args.sig_darwin_x64,
        "darwin-aarch64": args.sig_darwin_aarch64,
        "windows-x86_64": args.sig_windows_x64,
        "linux-x86_64": args.sig_linux_x64,
    }

    manifest = generate_manifest(
        version=args.version,
        notes=args.notes,
        tag=args.tag,
        pub_date=pub_date,
        signatures=signatures,
    )

    payload = json.dumps(manifest, indent=2) + "\n"

    if args.output == "-":
        sys.stdout.write(payload)
    else:
        Path(args.output).write_text(payload, encoding="utf-8")
        print(f"Written to {args.output}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
