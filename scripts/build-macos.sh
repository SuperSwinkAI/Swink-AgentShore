#!/usr/bin/env bash
# build-macos.sh — thin shim over the cross-platform build spine.
#
# All build logic lives in the Python spine (scripts/buildkit). This shim only
# bootstraps `uv` and hands off to `python -m scripts.buildkit macos`, which
# owns the full pipeline: clean → dashboard → sidecar → frontend → wheel →
# sign → tauri build → verify → pkg → notarize → install → reveal.
#
# Usage (flags unchanged — forwarded verbatim to the spine):
#   ./scripts/build-macos.sh                  # build .app/.dmg/.pkg, reveal in Finder
#   ./scripts/build-macos.sh --skip-dashboard # reuse dashboard/dist
#   ./scripts/build-macos.sh --skip-sidecar   # reuse staged bd sidecar
#   ./scripts/build-macos.sh --debug          # debug build instead of release
#   ./scripts/build-macos.sh --no-pkg         # skip .pkg wrap (only .app + .dmg)
#   ./scripts/build-macos.sh --no-sign        # skip macOS Developer ID signing
#   ./scripts/build-macos.sh --install        # also install to /Applications/
#   ./scripts/build-macos.sh --notarize       # notarize + staple the .pkg
#   ./scripts/build-macos.sh --keychain-profile NAME  # notary profile
#   ./scripts/build-macos.sh --help           # full flag help (from the spine)
#
# See docs/design/build-pipeline-unification.md for the architecture.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  printf 'error: uv not found — install uv (https://docs.astral.sh/uv/) and retry\n' >&2
  exit 1
fi

cd "$REPO_ROOT"
exec uv run python -m scripts.buildkit macos "$@"
