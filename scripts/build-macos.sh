#!/usr/bin/env bash
# build-macos.sh — Local .pkg build pipeline for the AgentShore desktop app.
#
# Builds AgentShore's package layout (dashboard + sidecar + Tauri 2 shell).
#
# Default flow: build everything, sign the .app and .pkg with any available
# Developer ID certs, and reveal the resulting .pkg in Finder. No install,
# no launch — distribute or double-click the .pkg yourself.
#
# Usage:
#   ./scripts/build-macos.sh                         # build .app/.dmg/.pkg, reveal in Finder
#   ./scripts/build-macos.sh --skip-dashboard        # reuse dashboard/dist
#   ./scripts/build-macos.sh --skip-sidecar          # reuse src-tauri/binaries/agentshore-bd-*
#   ./scripts/build-macos.sh --debug                 # debug build instead of release
#   ./scripts/build-macos.sh --no-pkg                # skip .pkg wrap (only .app + .dmg)
#   ./scripts/build-macos.sh --install               # also install to /Applications/
#   ./scripts/build-macos.sh --notarize              # notarize + staple the .pkg
#   ./scripts/build-macos.sh --keychain-profile NAME # notarization profile (default: agentshore-notary)
#   ./scripts/build-macos.sh --no-sign               # skip macOS Developer ID signing
#   ./scripts/build-macos.sh --help                  # print this header
#
# Pipeline phases:
#   1. Kill running AgentShore desktop / sidecar / dashboard processes
#   2. Clean stale ~/.agentshore session files, old .app bundle
#   3. Build dashboard React lib (mirrors `cd dashboard && npm run build`)
#   4. Build bundled bd sidecar (mirrors `npm run build:tauri-sidecars`)
#   5. Build Tauri frontend (mirrors `npm run build:tauri-frontend`)
#   5b. Build the agentshore Python wheel (`uv build --wheel`) — shipped inside
#       the .pkg so postinstall can provision the managed sidecar venv.
#   6. Resolve macOS code-signing identity (auto-detect Developer ID Application)
#   7. Build Tauri app bundle (`npx tauri build`)
#   8. Verify .app code signature via `codesign --verify --deep --strict`
#   9. Build two component .pkgs via `pkgbuild`:
#      - ai.agentshore.desktop  .app + install-agentshore-venv.sh + bundled wheel
#                            → provisions ~/Library/Application Support/AgentShore/venv
#      - ai.agentshore.cli      nopayload + install-agentshore-cli.sh + bundled wheel
#                            → `uv tool install --force` for ~/.local/bin/agentshore
#      Wrap both via `productbuild --distribution` so Installer.app's
#      Customize panel shows "AgentShore Desktop" (required) and "AgentShore CLI"
#      (opt-out checkbox) as deliberate visible choices. Sign with
#      Developer ID Installer if that cert is in the Keychain
#      (skipped via --no-pkg).
#  10. (optional --notarize) Submit the .pkg via `xcrun notarytool` + staple
#  11. (optional --install) Install to /Applications (`installer -pkg`)
#  12. Reveal the .pkg in Finder via `open -R`
#
# Code signing: when 'Developer ID Application' / 'Developer ID Installer'
# certs are present in your Keychain, the .app and .pkg are signed
# automatically (phases 6 + 9). Pass --no-sign to force unsigned output.
#
# Notarization (--notarize) additionally requires a stored `xcrun notarytool`
# keychain profile (default name: agentshore-notary) and a signed .pkg.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DESKTOP_DIR="$REPO_ROOT/desktop"

APP_NAME="AgentShore"
APP_BUNDLE_ID="ai.agentshore.desktop"

SKIP_DASHBOARD=0
SKIP_SIDECAR=0
BUILD_MODE="release"
DO_INSTALL=0
BUILD_PKG=1
NOTARIZE=0
NO_SIGN=0
KEYCHAIN_PROFILE="agentshore-notary"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-dashboard)  SKIP_DASHBOARD=1; shift ;;
    --skip-sidecar)    SKIP_SIDECAR=1; shift ;;
    --debug)           BUILD_MODE="debug"; shift ;;
    --install)         DO_INSTALL=1; shift ;;
    --no-sign)         NO_SIGN=1; shift ;;
    --no-pkg)          BUILD_PKG=0; shift ;;
    --notarize)        NOTARIZE=1; BUILD_PKG=1; shift ;;
    --keychain-profile) KEYCHAIN_PROFILE="$2"; shift 2 ;;
    --help|-h)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) printf 'Unknown flag: %s\n' "$1" >&2; exit 1 ;;
  esac
done

if [[ "$NOTARIZE" -eq 1 && "$BUILD_PKG" -eq 0 ]]; then
  printf 'error: --notarize requires the .pkg (do not pass --no-pkg with --notarize)\n' >&2
  exit 1
fi

log()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
info() { printf '    %s\n' "$1"; }
die()  { printf 'error: %s\n' "$1" >&2; exit 1; }

# ── 1. Kill running AgentShore processes ────────────────────────────────────────

log "Stopping running AgentShore processes"
KILLED_ANY=0
for proc in "AgentShore Desktop" "agentshore-desktop" "agentshore"; do
  if pgrep -ix "$proc" >/dev/null 2>&1; then
    killall -TERM "$proc" 2>/dev/null && { info "SIGTERM → $proc"; KILLED_ANY=1; } || true
  fi
done
# Orphan sidecar and dashboard subprocesses (CLI mode).
if pgrep -f "agentshore.sidecar" >/dev/null 2>&1; then
  pkill -TERM -f "agentshore.sidecar" 2>/dev/null && { info "SIGTERM → python -m agentshore.sidecar"; KILLED_ANY=1; } || true
fi
if pgrep -f "agentshore dashboard" >/dev/null 2>&1; then
  pkill -TERM -f "agentshore dashboard" 2>/dev/null && { info "SIGTERM → agentshore dashboard"; KILLED_ANY=1; } || true
fi
[[ "$KILLED_ANY" -eq 1 ]] && sleep 1 || true
# Escalate any survivors.
for proc in "AgentShore Desktop" "agentshore-desktop"; do
  killall -KILL "$proc" 2>/dev/null && info "SIGKILL → $proc" || true
done
pkill -KILL -f "agentshore.sidecar" 2>/dev/null && info "SIGKILL → agentshore.sidecar" || true
pkill -KILL -f "agentshore dashboard" 2>/dev/null && info "SIGKILL → agentshore dashboard" || true

# ── 2. Clean stale files ─────────────────────────────────────────────────────

log "Cleaning stale files"

# Per-session sockets and PID files.
if [[ -d "$HOME/.agentshore/sessions" ]]; then
  find "$HOME/.agentshore/sessions" -type s -name "socket.sock" -delete 2>/dev/null || true
  find "$HOME/.agentshore/sessions" -type f -name "dashboard.pid" -delete 2>/dev/null || true
  find "$HOME/.agentshore/sessions" -type f -name "agentshore.pid" -delete 2>/dev/null || true
  info "Removed stale sockets + PID files under ~/.agentshore/sessions/"
fi

# Old bundle in cargo target/.
# Tauri signs via the codesign daemon (runs as root), so the bundle ends up
# root-owned after every build. Use osascript for an authenticated GUI-prompted
# delete — no TTY sudo needed, works from Claude Code and CI-with-display alike.
BUNDLE_DIR="$DESKTOP_DIR/src-tauri/target/$BUILD_MODE/bundle/macos/$APP_NAME.app"
if [[ -d "$BUNDLE_DIR" ]]; then
  if rm -rf "$BUNDLE_DIR" 2>/dev/null; then
    info "Removed old bundle at target/.../bundle/macos/"
  else
    osascript -e "do shell script \"rm -rf '${BUNDLE_DIR}'\" with administrator privileges" \
      && info "Removed old bundle (authenticated via GUI)" \
      || printf '    warning: could not remove old bundle — build may fail\n' >&2
  fi
fi

# Previously-installed copy in /Applications.
INSTALLED_APP="/Applications/$APP_NAME.app"
if [[ -d "$INSTALLED_APP" && "$DO_INSTALL" -eq 1 ]]; then
  sudo rm -rf "$INSTALLED_APP" && info "Removed installed app from /Applications/" || true
fi

# pkgbuild receipts left by prior --pkg installs.
for receipt in "$APP_BUNDLE_ID" "ai.agentshore.app" "ai.agentshore.cli"; do
  if pkgutil --pkgs 2>/dev/null | grep -q "^${receipt}$"; then
    sudo pkgutil --forget "$receipt" >/dev/null 2>&1 && info "Forgot pkg receipt: $receipt" || true
  fi
done

# ── 3. Dashboard React lib (the bundle the bridge serves) ────────────────────
#
# Two builds are required:
#   - `npm run build`     → bridge static bundle at src/agentshore/dashboard/static/
#                           (mounted by Tauri as `dashboard-static` resource)
#   - `npm run build:lib` → vite-lib bundle at dashboard/dist/ with sprite
#                           PNGs emitted via Rollup emitFile (consumed by the
#                           desktop's `@agentshore/dashboard` workspace import)
# Both write under dashboard/; run `build:lib` second so dist/ ends in the
# bundled-lib state (bridge `npm run build` leaves raw tsc output in dist/
# that the desktop vite build cannot resolve — see desktop-rbn).

if [[ "$SKIP_DASHBOARD" -eq 0 ]]; then
  log "Building dashboard bridge static"
  (cd "$REPO_ROOT/dashboard" && npm run build)
  log "Building dashboard lib bundle (dist/)"
  (cd "$REPO_ROOT/dashboard" && npm run build:lib)
else
  log "Skipping dashboard build (--skip-dashboard)"
fi

# ── 4. Bundled bd sidecar binary ─────────────────────────────────────────────

if [[ "$SKIP_SIDECAR" -eq 0 ]]; then
  log "Building bundled bd sidecar binary"
  (cd "$DESKTOP_DIR" && npm run build:tauri-sidecars)
else
  log "Skipping sidecar binary build (--skip-sidecar)"
fi

# ── 5. Tauri frontend ────────────────────────────────────────────────────────

log "Building Tauri frontend"
(cd "$DESKTOP_DIR" && npm run build:tauri-frontend)

# ── 5b. AgentShore Python wheel (shipped inside the .pkg) ───────────────────────
#
# The .pkg's postinstall step provisions ~/Library/Application Support/AgentShore
# /venv from this wheel so the desktop's Python sidecar is always the version
# this build was cut from. Without it, a fresh .pkg ships only the Tauri
# shell — Python fixes never reach the runtime (desktop-vlx1 follow-up).

log "Building agentshore python wheel"
WHEEL_STAGE_DIR="$DESKTOP_DIR/src-tauri/target/agentshore-wheel"
rm -rf "$WHEEL_STAGE_DIR" && mkdir -p "$WHEEL_STAGE_DIR"
command -v uv >/dev/null 2>&1 || die "uv not found — install uv (https://docs.astral.sh/uv/) and retry"
(cd "$REPO_ROOT" && uv build --wheel --out-dir "$WHEEL_STAGE_DIR" >/dev/null)
BUNDLED_WHEEL="$(ls -t "$WHEEL_STAGE_DIR"/agentshore-*-py3-none-any.whl 2>/dev/null | head -1 || true)"
[[ -n "$BUNDLED_WHEEL" && -f "$BUNDLED_WHEEL" ]] \
  || die "uv build did not produce a wheel under $WHEEL_STAGE_DIR"
info "Wheel: $(basename "$BUNDLED_WHEEL")"

# ── 6. Resolve macOS code-signing identity ───────────────────────────────────
#
# Tauri 2 reads APPLE_SIGNING_IDENTITY from the environment when
# `bundle.macOS.signingIdentity` is null in tauri.conf.json. We auto-detect
# the first 'Developer ID Application' cert in the login Keychain and export
# it so `tauri build` signs the .app with hardened runtime + entitlements.
# No identity in Keychain → build proceeds unsigned (Gatekeeper right-click-
# Open expected on first launch). Pass --no-sign to suppress auto-detect.

APP_SIGNING_ID=""
if [[ "$NO_SIGN" -eq 0 ]]; then
  log "Resolving macOS code-signing identity"
  APP_SIGNING_ID="$(security find-identity -v -p codesigning 2>/dev/null \
    | awk -F'"' '/Developer ID Application:/ {print $2; exit}')"
  if [[ -n "$APP_SIGNING_ID" ]]; then
    export APPLE_SIGNING_IDENTITY="$APP_SIGNING_ID"
    info "Identity: $APP_SIGNING_ID"
  else
    info "No 'Developer ID Application' cert in Keychain — building unsigned"
    info "Install a Developer ID cert to enable signing, or pass --no-sign to silence this"
  fi
else
  log "Skipping code-signing identity resolution (--no-sign)"
fi

# ── 7. Tauri app bundle ──────────────────────────────────────────────────────

log "Building Tauri app bundle ($BUILD_MODE)"
if [[ "$BUILD_MODE" == "debug" ]]; then
  (cd "$DESKTOP_DIR" && npx tauri build --debug)
else
  (cd "$DESKTOP_DIR" && npx tauri build)
fi

BUILT_APP="$DESKTOP_DIR/src-tauri/target/$BUILD_MODE/bundle/macos/$APP_NAME.app"
[[ -d "$BUILT_APP" ]] || die "Tauri build finished but $BUILT_APP does not exist"
info "Bundle ready at $BUILT_APP"

# ── 8. Verify .app code signature ────────────────────────────────────────────

if [[ -n "$APP_SIGNING_ID" ]]; then
  log "Verifying .app code signature"
  codesign --verify --deep --strict --verbose=2 "$BUILT_APP" \
    || die ".app signature verification failed — check codesign output above"
  info "Signature OK"
fi

# ── 9. Wrap .app in .pkg installer ───────────────────────────────────────────

PKG_OUT="$DESKTOP_DIR/dist/$APP_NAME.pkg"
INSTALLER_SCRIPTS_DIR="$REPO_ROOT/packaging/desktop/installer-scripts"
INSTALLER_RESOURCES_DIR="$REPO_ROOT/packaging/desktop/installer-resources"
DISTRIBUTION_TEMPLATE="$REPO_ROOT/packaging/desktop/Distribution.xml.in"
if [[ "$BUILD_PKG" -eq 1 ]]; then
  log "Building .pkg installer"
  command -v pkgbuild >/dev/null 2>&1 || die "pkgbuild not found — install Xcode Command Line Tools"
  command -v productbuild >/dev/null 2>&1 || die "productbuild not found — install Xcode Command Line Tools"
  mkdir -p "$DESKTOP_DIR/dist"
  APP_VERSION="$(grep '"version"' "$DESKTOP_DIR/src-tauri/tauri.conf.json" | head -1 | sed 's/.*: *"\(.*\)".*/\1/' | tr -d ',')"

  # ── 9a. Build component .pkgs via pkgbuild ────────────────────────────────
  # Two component pkgs:
  #   - ai.agentshore.desktop  (the .app + sidecar venv postinstall)
  #   - ai.agentshore.cli      (nopayload — scripts-only; `uv tool install`
  #                          of the bundled wheel under the console user)
  # productbuild wraps both into a Distribution archive whose Customize
  # panel shows them as separate user-visible choices (Install CLI is
  # opt-out; the desktop is required).
  COMPONENT_PKG_DIR="$DESKTOP_DIR/src-tauri/target/pkg-component"
  rm -rf "$COMPONENT_PKG_DIR" && mkdir -p "$COMPONENT_PKG_DIR"

  # — Desktop component —
  APP_COMPONENT_PKG="$COMPONENT_PKG_DIR/agentshore-desktop-component.pkg"
  APP_PKG_ARGS=(
    --component "$BUILT_APP"
    --install-location "/Applications"
    --identifier "$APP_BUNDLE_ID"
    --version "$APP_VERSION"
  )
  if [[ -d "$INSTALLER_SCRIPTS_DIR" ]]; then
    SCRIPTS_STAGE_DIR="$DESKTOP_DIR/src-tauri/target/pkg-scripts"
    rm -rf "$SCRIPTS_STAGE_DIR" && mkdir -p "$SCRIPTS_STAGE_DIR"
    cp "$INSTALLER_SCRIPTS_DIR/postinstall" "$SCRIPTS_STAGE_DIR/postinstall"
    cp "$REPO_ROOT/scripts/install-agentshore-venv.sh" \
       "$SCRIPTS_STAGE_DIR/install-agentshore-venv.sh"
    cp "$BUNDLED_WHEEL" "$SCRIPTS_STAGE_DIR/"
    chmod 0755 "$SCRIPTS_STAGE_DIR/postinstall" \
               "$SCRIPTS_STAGE_DIR/install-agentshore-venv.sh"
    APP_PKG_ARGS+=(--scripts "$SCRIPTS_STAGE_DIR")
    info "Desktop scripts: $SCRIPTS_STAGE_DIR (postinstall provisions venv from bundled wheel)"
  fi
  pkgbuild "${APP_PKG_ARGS[@]}" "$APP_COMPONENT_PKG"
  # pkgbuild's default postinstall timeout is 600 s.  A fresh pip install of
  # agentshore (torch + playwright ≈ 150 MB) can exceed that on a slow
  # connection and the installer will kill the postinstall before the open
  # command fires.  Expand → patch → flatten to raise the limit to 3600 s.
  EXPANDED_PKG_DIR="$COMPONENT_PKG_DIR/agentshore-desktop-expanded"
  rm -rf "$EXPANDED_PKG_DIR"
  pkgutil --expand "$APP_COMPONENT_PKG" "$EXPANDED_PKG_DIR"
  /usr/bin/sed -i '' 's/timeout="600"/timeout="3600"/g' \
      "$EXPANDED_PKG_DIR/PackageInfo"
  pkgutil --flatten "$EXPANDED_PKG_DIR" "$APP_COMPONENT_PKG"
  rm -rf "$EXPANDED_PKG_DIR"
  info "Wrote desktop component pkg: $APP_COMPONENT_PKG"

  # — CLI component (nopayload, scripts + wheel) —
  CLI_COMPONENT_PKG="$COMPONENT_PKG_DIR/agentshore-cli-component.pkg"
  CLI_SCRIPTS_STAGE_DIR="$DESKTOP_DIR/src-tauri/target/pkg-cli-scripts"
  rm -rf "$CLI_SCRIPTS_STAGE_DIR" && mkdir -p "$CLI_SCRIPTS_STAGE_DIR"
  cp "$INSTALLER_SCRIPTS_DIR/cli-postinstall" "$CLI_SCRIPTS_STAGE_DIR/postinstall"
  cp "$REPO_ROOT/scripts/install-agentshore-cli.sh" \
     "$CLI_SCRIPTS_STAGE_DIR/install-agentshore-cli.sh"
  cp "$BUNDLED_WHEEL" "$CLI_SCRIPTS_STAGE_DIR/"
  chmod 0755 "$CLI_SCRIPTS_STAGE_DIR/postinstall" \
             "$CLI_SCRIPTS_STAGE_DIR/install-agentshore-cli.sh"
  pkgbuild --nopayload \
           --scripts "$CLI_SCRIPTS_STAGE_DIR" \
           --identifier "ai.agentshore.cli" \
           --version "$APP_VERSION" \
           "$CLI_COMPONENT_PKG"
  info "Wrote CLI component pkg: $CLI_COMPONENT_PKG"

  # ── 9b. Wrap components in distribution pkg via productbuild ──────────────
  [[ -f "$DISTRIBUTION_TEMPLATE" ]] \
    || die "Distribution template missing: $DISTRIBUTION_TEMPLATE"

  # Regenerate EULA.rtf from LICENSE so the installer always reflects the
  # current legal source of truth. The generator also applies bold/large
  # styling to the risk-acknowledgement block.
  EULA_BUILDER="$INSTALLER_RESOURCES_DIR/build-eula-rtf.sh"
  if [[ -x "$EULA_BUILDER" ]]; then
    info "Regenerating EULA.rtf from LICENSE"
    "$EULA_BUILDER" >/dev/null
  fi
  [[ -f "$INSTALLER_RESOURCES_DIR/EULA.rtf" ]] \
    || die "EULA.rtf missing: $INSTALLER_RESOURCES_DIR/EULA.rtf"

  DISTRIBUTION_XML="$COMPONENT_PKG_DIR/Distribution.xml"
  sed -e "s|@VERSION@|$APP_VERSION|g" \
      -e "s|@APP_COMPONENT_PKG@|$(basename "$APP_COMPONENT_PKG")|g" \
      -e "s|@CLI_COMPONENT_PKG@|$(basename "$CLI_COMPONENT_PKG")|g" \
      "$DISTRIBUTION_TEMPLATE" > "$DISTRIBUTION_XML"
  info "Rendered distribution: $DISTRIBUTION_XML"

  INSTALLER_SIGNING_ID=""
  if [[ "$NO_SIGN" -eq 0 ]]; then
    INSTALLER_SIGNING_ID="$(security find-identity -v 2>/dev/null \
      | awk -F'"' '/Developer ID Installer:/ {print $2; exit}')"
  fi

  PB_ARGS=(
    --distribution "$DISTRIBUTION_XML"
    --resources "$INSTALLER_RESOURCES_DIR"
    --package-path "$COMPONENT_PKG_DIR"
  )
  if [[ -n "$INSTALLER_SIGNING_ID" ]]; then
    PB_ARGS+=(--sign "$INSTALLER_SIGNING_ID")
    info "Installer signing identity: $INSTALLER_SIGNING_ID"
  elif [[ "$NOTARIZE" -eq 1 ]]; then
    die "No 'Developer ID Installer' cert in Keychain — required for --notarize"
  else
    info "No 'Developer ID Installer' cert found — producing unsigned .pkg"
  fi
  productbuild "${PB_ARGS[@]}" "$PKG_OUT"
  info "Wrote $PKG_OUT"
fi

# ── 10. (optional) Notarize via xcrun notarytool ─────────────────────────────

if [[ "$NOTARIZE" -eq 1 ]]; then
  log "Notarizing .pkg (keychain profile: $KEYCHAIN_PROFILE)"
  command -v xcrun >/dev/null 2>&1 || die "xcrun not found — install Xcode Command Line Tools"
  xcrun notarytool submit "$PKG_OUT" \
    --keychain-profile "$KEYCHAIN_PROFILE" \
    --wait
  xcrun stapler staple "$PKG_OUT"
  info "Notarized + stapled $PKG_OUT"
fi

# ── 11. (optional) Install ───────────────────────────────────────────────────

if [[ "$DO_INSTALL" -eq 1 ]]; then
  log "Installing to /Applications/"
  if [[ "$BUILD_PKG" -eq 1 ]]; then
    sudo installer -pkg "$PKG_OUT" -target /
    info "Installed from $PKG_OUT"
  else
    sudo cp -R "$BUILT_APP" "$INSTALLED_APP"
    info "Copied .app to $INSTALLED_APP"
  fi
fi

# ── 12. Reveal artifacts in Finder ───────────────────────────────────────────

log "Build complete"
info ".app: $BUILT_APP"
if [[ "$BUILD_PKG" -eq 1 ]]; then
  info ".pkg: $PKG_OUT"
  open -R "$PKG_OUT"
else
  open -R "$BUILT_APP"
fi
