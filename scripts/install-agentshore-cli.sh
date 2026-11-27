#!/usr/bin/env bash

set -uo pipefail

WHEEL_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --wheel) WHEEL_PATH="$2"; shift 2 ;;
    --help|-h)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      if [[ -z "$WHEEL_PATH" ]]; then
        WHEEL_PATH="$1"; shift
      else
        printf 'Unknown argument: %s\n' "$1" >&2; exit 1
      fi
      ;;
  esac
done

log()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
info() { printf '    %s\n' "$1"; }
die()  { printf 'error: %s\n' "$1" >&2; exit 1; }

[[ -n "$WHEEL_PATH" && -f "$WHEEL_PATH" ]] \
  || die "Missing or invalid --wheel path: ${WHEEL_PATH:-<empty>}"


log "Locating uv"
UV_BIN=""
for candidate in \
    "$HOME/.local/bin/uv" \
    "$HOME/.cargo/bin/uv" \
    "/opt/homebrew/bin/uv" \
    "/usr/local/bin/uv" \
    "/opt/local/bin/uv"; do
  if [[ -x "$candidate" ]]; then
    UV_BIN="$candidate"
    break
  fi
done

if [[ -z "$UV_BIN" ]] && command -v uv >/dev/null 2>&1; then
  UV_BIN="$(command -v uv)"
fi


if [[ -z "$UV_BIN" ]]; then
  log "uv not found in standard locations — bootstrapping via official installer"
  if /usr/bin/curl --fail --location --silent --show-error \
       --connect-timeout 30 --max-time 300 \
       https://astral.sh/uv/install.sh | sh; then
    if [[ -x "$HOME/.local/bin/uv" ]]; then
      UV_BIN="$HOME/.local/bin/uv"
      info "uv installed at $UV_BIN"
    else
      die "uv install script ran but uv binary missing at $HOME/.local/bin/uv"
    fi
  else
    die "uv installer download failed (curl exit non-zero)"
  fi
fi

info "Using uv: $UV_BIN"


log "Installing agentshore CLI from wheel"
info "Wheel: $WHEEL_PATH"

"$UV_BIN" tool install --native-tls --force --reinstall --python 3.12 \
    "$WHEEL_PATH" \
  || die "uv tool install failed"


log "Verifying agentshore command"
AGENTSHORE_BIN=""
if [[ -x "$HOME/.local/bin/agentshore" ]]; then
  AGENTSHORE_BIN="$HOME/.local/bin/agentshore"
elif command -v agentshore >/dev/null 2>&1; then
  AGENTSHORE_BIN="$(command -v agentshore)"
fi

[[ -n "$AGENTSHORE_BIN" ]] \
  || die "agentshore command not found after install — check 'uv tool list' output"

VERSION="$("$AGENTSHORE_BIN" --version 2>&1 | head -1 || true)"
log "Installed CLI:"
info "  binary:  $AGENTSHORE_BIN"
info "  version: $VERSION"
