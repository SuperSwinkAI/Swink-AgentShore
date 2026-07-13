"""Build a Tauri-shippable bd sidecar bundle.

Invoke from the repo root:

    python packaging/desktop/build_bd_sidecar.py [--bd PATH] [--out PATH]

Default (no ``--bd``): download the pinned ``bd`` release for the build host's
OS/arch from the beads GitHub releases, verify its SHA-256 against the checksum
table baked into this file, extract the binary, and bundle it. This makes the
shipped ``.app`` reproducible and version-correct regardless of what (if any)
``bd`` happens to be on the build machine's PATH.

With ``--bd PATH``: skip the download and bundle the given local binary verbatim
(used by CI/offline builds and by the unit tests).

Steps:
  1. Resolve the source ``bd`` binary — download the pinned release, or use
     ``--bd`` if given.
  2. Validate that the source exists and is executable.
  3. Copy it into ``<out>/agentshore-bd/`` as ``agentshore-bd`` (or ``.exe`` on
     Windows), preserving metadata via ``copy2``.

Output layout:
  ``<out>/agentshore-bd/agentshore-bd`` (``.exe`` on Windows)

This matches the desktop sidecar bundle pattern and is consumed by Tauri
``bundle.externalBin`` wiring per DESIGN §6.4.

Version pin & change control: ``PINNED_BD_VERSION`` and the ``PINNED_CHECKSUMS``
table below are the supply-chain anchor for the bundled binary. They are kept in
lockstep with the runtime pin (``agentshore.beads.setup.REQUIRED_BD_VERSION``);
``tests/sidecar/test_bd_sidecar.py`` fails if they drift. To bump bd: update
both, then refresh the checksums from the release's ``checksums.txt``.

Extraction and asset-name logic is delegated to ``agentshore.beads.setup`` so
the two code paths stay in sync — this file keeps only the pinned-checksum
verification (a build-time-only concern) and the orchestration.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import cast

PACKAGING_DIR = Path(__file__).resolve().parent
DEFAULT_DIST = PACKAGING_DIR / "dist"

# Pinned bd release the desktop bundle ships. Kept equal to the runtime pin in
# agentshore.beads.setup.REQUIRED_BD_VERSION (enforced by a unit test). Bumping
# this REQUIRES refreshing PINNED_CHECKSUMS from the new release's checksums.txt.
PINNED_BD_VERSION = "1.1.0"

# SHA-256 of each release archive, copied verbatim from the release's
# checksums.txt. Keyed by asset filename. Only the desktop-relevant targets are
# listed (darwin/linux/windows); add rows here if a new build host is needed.
PINNED_CHECKSUMS: dict[str, str] = {
    "beads_1.1.0_darwin_arm64.tar.gz": (
        "c42e24d83b258f7ba9f52a6d2d5f6b055869dfe7807165055988b12e7ea8c564"
    ),
    "beads_1.1.0_darwin_amd64.tar.gz": (
        "5d7d30fdadcf012b7e0c1933a62cdfaef106e2561509b904e50a6733621cf8da"
    ),
    "beads_1.1.0_linux_amd64.tar.gz": (
        "b0f3dd607c3fb989ee08d0a6854fba80d0402971eb108f9af6170bc14d491a34"
    ),
    "beads_1.1.0_linux_arm64.tar.gz": (
        "e64eb6f5f998c9eae3ef9ec786f5f1c907ab3ed04fe220ebf265ca9952e21b2f"
    ),
    "beads_1.1.0_windows_amd64.zip": (
        "12b1d37344d3b1543301e21a2b9ed3ab6ae009f0418441f3de5f762b40769a6b"
    ),
    "beads_1.1.0_windows_arm64.zip": (
        "c2f3fc50407896336bafaab74e955f632fdfb487579fd24e44d766ab05792f7f"
    ),
}

_RELEASE_URL = "https://github.com/gastownhall/beads/releases/download/v{version}/{asset}"

# Imports from agentshore.beads.setup are lazy so this script runs in a bare
# interpreter (e.g. unit tests load it by path). When importable:
#   - main() asserts REQUIRED_BD_VERSION == PINNED_BD_VERSION (no silent drift)
#   - _release_asset_name and _extract_bd delegate to the shared implementations
_RUNTIME_BD_VERSION: str | None = None
_BEADS_SETUP_AVAILABLE = False
try:
    from agentshore.beads.setup import REQUIRED_BD_VERSION as _RUNTIME_BD_VERSION
    from agentshore.beads.setup import _extract_bd as _extract_bd_impl

    _BEADS_SETUP_AVAILABLE = True
except Exception:  # pragma: no cover - import path depends on the build env
    pass


def _extension_for_triple(triple: str) -> str:
    """Sidecar binary extension for the *target* triple, not the build host.

    Tauri names the sidecar after the target platform, so a Windows target
    gets ``.exe`` even when cross-built from Linux/macOS (and a Linux target
    gets no extension even when built on a Windows host).
    """
    return ".exe" if "windows" in triple else ""


def _resolve_target_triple() -> str:
    try:
        result = subprocess.run(
            ["rustc", "--print", "host-tuple"],
            check=True,
            capture_output=True,
            text=True,
        )
        triple = result.stdout.strip()
        if triple:
            return triple
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    raise SystemExit("Unable to resolve target triple via `rustc --print host-tuple`.")


def _validate_source(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_file():
        print(f"bd binary not found: {resolved}", file=sys.stderr)
        raise SystemExit(2)
    # The POSIX exec bit doesn't exist on Windows (os.access(X_OK) is True for
    # any readable file), so the check is meaningful only off-Windows.
    if not sys.platform.startswith("win") and not os.access(resolved, os.X_OK):
        print(f"bd binary is not executable: {resolved}", file=sys.stderr)
        raise SystemExit(2)
    return resolved


def _release_asset_name(version: str, system: str, machine: str) -> str:
    """Map the build host's (system, machine) to a beads release asset name.

    The mapping logic mirrors ``agentshore.beads.setup._beads_release_asset``
    exactly. The tests in ``test_bd_sidecar.py`` pin both sides so they cannot
    drift independently.
    """
    os_map = {"darwin": "darwin", "linux": "linux", "windows": "windows"}
    arch_map = {
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86_64": "amd64",
        "amd64": "amd64",
    }
    os_key = os_map.get(system.lower())
    arch_key = arch_map.get(machine.lower())
    if os_key is None or arch_key is None:
        raise SystemExit(
            f"No pinned bd release for host '{system}/{machine}'. "
            "Pass --bd PATH to bundle a local binary instead."
        )
    ext = "zip" if os_key == "windows" else "tar.gz"
    return f"beads_{version}_{os_key}_{arch_key}.{ext}"


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "agentshore-build-bd-sidecar"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310 (pinned host)
            return cast("bytes", resp.read())
    except OSError as exc:
        raise SystemExit(f"Failed to download bd release from {url}: {exc}") from exc


def _verify_checksum(asset: str, data: bytes) -> None:
    expected = PINNED_CHECKSUMS.get(asset)
    if expected is None:
        raise SystemExit(
            f"No pinned checksum for asset '{asset}'. Add it to PINNED_CHECKSUMS "
            "from the release's checksums.txt before building."
        )
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise SystemExit(
            f"Checksum mismatch for {asset}:\n  expected {expected}\n  got      {actual}\n"
            "Refusing to bundle an unverified bd binary."
        )


def _extract_bd(asset: str, data: bytes, dest_dir: Path) -> Path:
    """Extract the bd binary from a downloaded archive into *dest_dir*.

    Delegates to ``agentshore.beads.setup._extract_bd`` when available so the
    extraction logic stays in one place. Falls back to an inline implementation
    for bare-interpreter contexts (CI cross-builds without the agentshore package).
    """
    kind = "zip" if asset.endswith(".zip") else "tar.gz"
    binary_name = "bd.exe" if asset.endswith(".zip") else "bd"
    target = dest_dir / binary_name

    if _BEADS_SETUP_AVAILABLE:
        _extract_bd_impl(data, kind, binary_name, target)
    else:
        # Bare-interpreter fallback.
        import io
        import tarfile
        import zipfile
        from pathlib import PurePosixPath

        if kind == "zip":
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                member = next(
                    (n for n in zf.namelist() if PurePosixPath(n).name == binary_name), None
                )
                if member is None:
                    raise SystemExit(f"No '{binary_name}' entry found inside {asset}")
                target.write_bytes(zf.read(member))
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
                member = next(
                    (n for n in tf.getnames() if PurePosixPath(n).name == binary_name), None
                )
                if member is None:
                    raise SystemExit(f"No '{binary_name}' entry found inside {asset}")
                extracted = tf.extractfile(member)
                if extracted is None:
                    raise SystemExit(f"'{member}' in {asset} is not a regular file")
                target.write_bytes(extracted.read())

    if not sys.platform.startswith("win"):
        os.chmod(target, 0o755)
    return target


def _fetch_pinned_bd(version: str, stage_dir: Path) -> Path:
    """Download, verify, and extract the pinned bd binary into *stage_dir*."""
    asset = _release_asset_name(version, platform.system(), platform.machine())
    url = _RELEASE_URL.format(version=version, asset=asset)
    print(f"Downloading pinned bd {version}: {url}", file=sys.stderr)
    data = _download(url)
    _verify_checksum(asset, data)
    print(f"Verified {asset} ({len(data)} bytes) against pinned SHA-256", file=sys.stderr)
    return _extract_bd(asset, data, stage_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bd",
        type=Path,
        default=None,
        help="Path to a local bd binary to bundle. Default: download the pinned release.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_DIST,
        help="Output directory root (default: packaging/desktop/dist).",
    )
    parser.add_argument(
        "--target-triple",
        type=str,
        default=None,
        help="Target triple suffix for Tauri sidecar naming.",
    )
    args = parser.parse_args(argv)

    if _RUNTIME_BD_VERSION is not None and _RUNTIME_BD_VERSION != PINNED_BD_VERSION:
        raise SystemExit(
            f"bd version pin drift: this script bundles {PINNED_BD_VERSION!r} but "
            f"agentshore.beads.setup.REQUIRED_BD_VERSION is {_RUNTIME_BD_VERSION!r}. "
            "Update PINNED_BD_VERSION + PINNED_CHECKSUMS to match, then rebuild."
        )

    bundle_dir = args.out / "agentshore-bd"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    triple = args.target_triple or _resolve_target_triple()
    base_name = "agentshore-bd"
    extension = _extension_for_triple(triple)
    target = bundle_dir / f"{base_name}{extension}"
    target_with_triple = bundle_dir / f"{base_name}-{triple}{extension}"

    with tempfile.TemporaryDirectory(prefix="agentshore-bd-") as tmp:
        if args.bd is not None:
            source = _validate_source(args.bd)
        else:
            source = _validate_source(_fetch_pinned_bd(PINNED_BD_VERSION, Path(tmp)))

        shutil.copy2(source, target)
        shutil.copy2(source, target_with_triple)

    if not sys.platform.startswith("win"):
        os.chmod(target, 0o755)
        os.chmod(target_with_triple, 0o755)

    print(
        f"bd-sidecar bundle: {target} (tauri target: {target_with_triple})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
