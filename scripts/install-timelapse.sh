#!/usr/bin/env bash

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

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

VENV_PY="$HOME/Library/Application Support/AgentShore/venv/bin/python"
[[ -x "$VENV_PY" ]] \
  || die "managed venv python missing at $VENV_PY (Desktop component must install first)"

log "Provisioning timelapse-capture toolchain via managed venv"
info "Python: $VENV_PY"

"$VENV_PY" - <<'PY'
import asyncio
import sys

from agentshore.timelapse.setup import install_timelapse

result = asyncio.run(install_timelapse())
print(result.message)
sys.exit(0 if result.success else 1)
PY
