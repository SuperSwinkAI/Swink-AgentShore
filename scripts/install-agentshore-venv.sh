#!/usr/bin/env bash
# install-agentshore-venv.sh — Provision the AgentShore desktop sidecar's managed
# Python venv. Runs as the .pkg's postinstall step; can also be run manually
# during development to set up the production sidecar launch path locally.
#
# Behavior:
#   1. Locate Python 3.12 (required). Prefers `python3.12` on PATH; falls
#      back to `python3` if it reports a 3.12.x version. Exits with a clear
#      error if neither is available — automatic provisioning lives in the
#      .pkg's postinstall (which has root) so it can install the python.org
#      pkg system-wide before this user-context helper runs.
#   2. Create (or replace) a per-user venv at the platform-managed path.
#      On macOS that's $HOME/Library/Application Support/AgentShore/venv.
#      No sudo required.
#   3. `pip install <wheel>` into the venv. The wheel path is taken from
#      $1 (or --wheel <path>); defaults to ./dist/agentshore-*-py3-none-any.whl
#      relative to the repo root for local development use.
#   4. Smoke-test: `python -c "import agentshore.sidecar"`.
#
# Shell `agentshore` CLI: installed by the separate `install-agentshore-cli.sh`
# script as part of the .pkg's CLI choice (Distribution.xml
# `ai.agentshore.cli`). Keeping the two scripts apart lets the wizard's
# "AgentShore CLI" checkbox cleanly opt out of touching the user's
# `~/.local/bin/agentshore`.
#
# Usage:
#   ./scripts/install-agentshore-venv.sh                    # auto-detect wheel in dist/
#   ./scripts/install-agentshore-venv.sh path/to/agentshore.whl
#
# Per-user install means the .pkg's postinstall must run as the logged-in
# user, not root — handled via the `install-agentshore-venv` LaunchAgent the
# postinstall script schedules (planned follow-up). Manual local use just
# needs the current shell.

set -euo pipefail

VENV_PATH="$HOME/Library/Application Support/AgentShore/venv"
REQUIRED_PY="3.12"
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

# ── 1. Locate Python 3.12 ────────────────────────────────────────────────────

log "Locating Python ${REQUIRED_PY}"
PYTHON_BIN=""

# python.org's macos11 pkg lands here; the .pkg's postinstall may have just
# installed it before invoking this helper, so check before relying on PATH.
PYTHON_ORG_FRAMEWORK_BIN="/Library/Frameworks/Python.framework/Versions/${REQUIRED_PY}/bin/python${REQUIRED_PY}"

# Prefer python3.12 by name.
if command -v "python${REQUIRED_PY}" >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v "python${REQUIRED_PY}")"
elif [[ -x "$PYTHON_ORG_FRAMEWORK_BIN" ]]; then
  PYTHON_BIN="$PYTHON_ORG_FRAMEWORK_BIN"
fi

# Fall back to python3 if it self-reports as 3.12.
if [[ -z "$PYTHON_BIN" ]] && command -v python3 >/dev/null 2>&1; then
  version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
  if [[ "$version" == "$REQUIRED_PY" ]]; then
    PYTHON_BIN="$(command -v python3)"
  fi
fi

if [[ -z "$PYTHON_BIN" ]]; then
  die "Python ${REQUIRED_PY} not found. Install it from https://www.python.org/downloads/macos/ and re-run."
fi
info "Using $PYTHON_BIN"

# ── 2. Resolve wheel path ────────────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "$WHEEL_PATH" ]]; then
  # Auto-detect newest wheel in dist/.
  if [[ -d "$REPO_ROOT/dist" ]]; then
    # shellcheck disable=SC2012  # `ls -t` is the easiest "newest" sort here
    WHEEL_PATH="$(ls -t "$REPO_ROOT/dist"/agentshore-*-py3-none-any.whl 2>/dev/null | head -1 || true)"
  fi
fi

[[ -n "$WHEEL_PATH" && -f "$WHEEL_PATH" ]] \
  || die "No agentshore wheel found. Pass --wheel <path>, or run 'uv build --wheel' first."

info "Wheel: $WHEEL_PATH"

# ── 3. Locate uv (prefer over pip — 10-100x faster resolver + parallel downloads) ──

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
if [[ -n "$UV_BIN" ]]; then
  info "Using uv: $UV_BIN"
else
  info "uv not found — falling back to pip (slower)"
fi

# ── 4. Create / replace managed venv ─────────────────────────────────────────

log "Provisioning managed venv at $VENV_PATH"

VENV_PARENT="$(dirname "$VENV_PATH")"
if [[ ! -d "$VENV_PARENT" ]]; then
  mkdir -p "$VENV_PARENT" || die "Cannot create $VENV_PARENT (need sudo?)"
  info "Created parent dir $VENV_PARENT"
fi

if [[ -d "$VENV_PATH" ]]; then
  info "Removing existing venv"
  rm -rf "$VENV_PATH"
fi

if [[ -n "$UV_BIN" ]]; then
  "$UV_BIN" venv --python "$PYTHON_BIN" "$VENV_PATH"
else
  "$PYTHON_BIN" -m venv "$VENV_PATH"
fi
info "Created venv with $PYTHON_BIN"

VENV_PY="$VENV_PATH/bin/python"
[[ -x "$VENV_PY" ]] || die "venv python missing at $VENV_PY"

# ── 5. Install agentshore wheel ─────────────────────────────────────────────────

log "Installing agentshore wheel"
if [[ -n "$UV_BIN" ]]; then
  VIRTUAL_ENV="$VENV_PATH" "$UV_BIN" pip install "$WHEEL_PATH"
else
  "$VENV_PY" -m pip install --upgrade pip wheel >/dev/null
  "$VENV_PY" -m pip install "$WHEEL_PATH"
fi

# ── 6. Smoke test ────────────────────────────────────────────────────────────

log "Verifying agentshore.sidecar import"
"$VENV_PY" -c "import agentshore.sidecar; print('agentshore.sidecar OK')" \
  || die "agentshore.sidecar import failed in venv — wheel may be malformed"

AGENTSHORE_VERSION="$("$VENV_PY" -m pip show agentshore | grep '^Version' | awk '{print $2}')"
log "Installed managed venv:"
info "  python:  $VENV_PY"
info "  agentshore: $AGENTSHORE_VERSION"
info "  launch:  \"$VENV_PY\" -m agentshore.sidecar"
