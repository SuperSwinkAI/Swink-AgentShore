#!/usr/bin/env bash
# install-timelapse.sh — Provision the optional timelapse-capture toolchain
# (ffmpeg, Node.js 24+, the timelapse-capture CLI) by driving the canonical
# Python installer in AgentShore's managed sidecar venv. Runs as the
# user-context step of the .pkg's Timelapse component (Distribution.xml
# `ai.agentshore.timelapse` choice); can also be run manually.
#
# Why reuse the venv Python instead of re-scripting brew/npm here:
#   agentshore.timelapse.setup.install_timelapse is the single source of truth
#   for the install recipe (ffmpeg + Node 24 + `npm i -g` the release tarball +
#   `doctor` verify). The desktop app already calls it at runtime via the
#   sidecar; this just lets the installer pre-provision it. The managed venv is
#   always present because the (required) Desktop component provisions it
#   earlier in the same install run.
#
# PATH note: .pkg postinstall scripts run with a sparse PATH, so Homebrew
# (/opt/homebrew/bin, /usr/local/bin) and the node/npm it installs aren't on
# it. We prepend the standard Homebrew bins so the Python installer's
# `shutil.which("brew"/"node"/"npm")` lookups resolve.
#
# Usage:
#   install-timelapse.sh
#
# Exit codes:
#   0 — timelapse toolchain installed and verified (or already present)
#   1 — managed venv missing, or the Python installer reported failure. The
#       component's postinstall surfaces non-zero via an osascript alert but
#       does not fail the wizard.

set -uo pipefail

case "${1:-}" in
  --help|-h)
    grep '^#' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
esac

log()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
info() { printf '    %s\n' "$1"; }
die()  { printf 'error: %s\n' "$1" >&2; exit 1; }

# Homebrew / common tool locations the sparse postinstall PATH omits.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

VENV_PY="$HOME/Library/Application Support/AgentShore/venv/bin/python"
[[ -x "$VENV_PY" ]] \
  || die "managed venv python missing at $VENV_PY (Desktop component must install first)"

log "Provisioning timelapse-capture toolchain via managed venv"
info "Python: $VENV_PY"

# install_timelapse() defaults cwd to the user's home; it shells out to brew
# (ffmpeg, node) and npm — which is why this script must run as the console
# user (Homebrew refuses to run as root), not in postinstall's root context.
"$VENV_PY" - <<'PY'
import asyncio
import sys

from agentshore.timelapse.setup import install_timelapse

result = asyncio.run(install_timelapse())
print(result.message)
sys.exit(0 if result.success else 1)
PY
