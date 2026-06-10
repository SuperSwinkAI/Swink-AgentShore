#!/usr/bin/env bash
# install-agentshore-cli.sh — Install the `agentshore` shell command from the
# bundled wheel via `uv tool install`. Runs as the user-context step of
# the AgentShore .pkg's CLI component (Distribution.xml `ai.agentshore.cli`
# choice); can also be run manually during development.
#
# Why this exists as a separate script:
#   .pkg postinstall scripts run with a sparse PATH (typically
#   /usr/bin:/bin:/usr/sbin:/sbin) so per-user uv installs at
#   ~/.local/bin/uv or ~/.cargo/bin/uv aren't found by a bare
#   `command -v uv`. The old single-script install-agentshore-venv.sh did a
#   "best-effort" PATH lookup and silently skipped — leaving the user's
#   CLI weeks-stale despite the desktop sidecar being current. The wizard's
#   CLI choice now drives this script which:
#     1. Searches well-known per-user + system locations for uv.
#     2. Bootstraps uv via the official installer if absent (user-context;
#        no sudo). User opted in by checking the wizard box.
#     3. Runs `uv tool install --force --reinstall` from the bundled wheel.
#     4. Smoke-tests the resulting `agentshore` binary.
#
# Usage:
#   install-agentshore-cli.sh --wheel <path-to-wheel>
#
# Exit codes:
#   0 — CLI installed and verified
#   1 — fatal: wheel missing, uv unavailable + bootstrap failed, or
#       `uv tool install` failed. The wizard's CLI choice's postinstall
#       surfaces non-zero exits to the user via an osascript alert.

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

# ── 1. Locate uv (per-user paths the postinstall PATH misses) ────────────────

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

# ── 2. Bootstrap uv if missing (official installer, user-context) ────────────

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

# ── 3. Install / refresh the agentshore CLI ─────────────────────────────────────

log "Installing agentshore CLI from wheel"
info "Wheel: $WHEEL_PATH"

# The wheel exposes the complete CLI dependency set directly; there are no
# package extras to attach here. Keep this aligned with the Windows helper and
# provisioner binary so all three paths use the same flag set.
# Use plain path (not file:// URI) — uv resolves local paths directly and
# handles spaces/# /% correctly without percent-encoding.
"$UV_BIN" tool install --native-tls --force --reinstall --python 3.12 \
    "$WHEEL_PATH" \
  || die "uv tool install failed"

# ── 4. Smoke test ────────────────────────────────────────────────────────────

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
