"""Build a Tauri-shippable bd sidecar bundle.

Invoke from the repo root:

    python packaging/desktop/build_bd_sidecar.py [--bd PATH] [--out PATH]

Steps:
  1. Resolve the source ``bd`` binary from ``--bd`` or ``shutil.which("bd")``.
  2. Validate that the source exists and is executable.
  3. Copy it into ``<out>/agentshore-bd/`` as ``agentshore-bd`` (or ``.exe`` on
     Windows), preserving metadata via ``copy2``.

Output layout:
  ``<out>/agentshore-bd/agentshore-bd`` (``.exe`` on Windows)

This matches the desktop sidecar bundle pattern and is consumed by Tauri
``bundle.externalBin`` wiring per DESIGN §6.4.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PACKAGING_DIR = Path(__file__).resolve().parent
DEFAULT_DIST = PACKAGING_DIR / "dist"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bd",
        type=Path,
        default=None,
        help="Path to bd binary (default: resolves with shutil.which('bd')).",
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

    source_arg = args.bd if args.bd is not None else shutil.which("bd")
    if source_arg is None:
        print("bd binary not found: set --bd or install bd on PATH", file=sys.stderr)
        raise SystemExit(2)
    source = _validate_source(Path(source_arg))

    bundle_dir = args.out / "agentshore-bd"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    triple = args.target_triple or _resolve_target_triple()
    base_name = "agentshore-bd"
    extension = _extension_for_triple(triple)
    target = bundle_dir / f"{base_name}{extension}"
    target_with_triple = bundle_dir / f"{base_name}-{triple}{extension}"

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
