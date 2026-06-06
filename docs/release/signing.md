# AgentShore Desktop — Code Signing & Notarization

This document is the maintainer runbook for producing signed, notarized
release builds of the AgentShore Desktop application. The shipping pipeline is
**macOS-only** and lives entirely in `scripts/build-macos.sh` — there is no
Windows or Linux signing path, and no GitHub Actions release job. Builds are
cut locally from a maintainer's machine.

It complements `docs/design/desktop/DESIGN.md` §6.5 ("Code signing & trust").

## 1. Overview

AgentShore Desktop bundles a Tauri 2 shell around a Python sidecar (the
AgentShore core, provisioned at install time into a managed venv from a wheel
shipped inside the `.pkg`). To ship a release that does not trigger Gatekeeper
warnings, the `.app` and `.pkg` must be signed with Developer ID certs and —
optionally but recommended for distribution — notarized by Apple.

The current shipping version is **0.2.1** (`desktop/src-tauri/tauri.conf.json`
`version`). `scripts/build-macos.sh` produces, in order:

| Stage | Tool / command | Notes |
| --- | --- | --- |
| Resolve identity | `security find-identity -v -p codesigning` | Auto-detects the first `Developer ID Application` cert |
| Build + sign `.app` | `npx tauri build` (Tauri bundler) | Hardened runtime + entitlements; also emits the `.dmg` (`targets: "all"`) |
| Verify `.app` | `codesign --verify --deep --strict` | Only when an Application cert was found |
| Build `.pkg` | `pkgbuild` + `productbuild` | Three components (see §4); signed with `Developer ID Installer` if present |
| (optional) Notarize | `xcrun notarytool submit --wait` + `xcrun stapler staple` | `--notarize` flag; requires an Installer cert + keychain profile |
| Publish | Upload to GitHub Releases (manual) | `.app`/`.dmg`/`.pkg` |
| Update manifest | `scripts/generate_update_manifest.py` → `latest.json` | Signed with the Tauri updater key; uploaded to the release |

Signing config lives in `desktop/src-tauri/tauri.conf.json` (`bundle.macOS`).
Hardened-runtime entitlements live in
`desktop/src-tauri/entitlements.plist`.

## 2. macOS Developer ID

### 2.1 Certificates

Two Developer ID certificates are used:

- **Developer ID Application** — signs the `.app` (via the Tauri bundler).
- **Developer ID Installer** — signs the `.pkg` (via `productbuild --sign`).
  Required if you intend to notarize; without it the build emits an unsigned
  `.pkg` and `--notarize` fails fast.

To provision them:

1. Enroll in the Apple Developer Program (~$99/yr).
2. In Xcode → Settings → Accounts → Manage Certificates, create both a
   "Developer ID Application" and a "Developer ID Installer" certificate.
3. Confirm they resolve from the login Keychain with `security find-identity`.
   The build script auto-detects the first matching identity of each kind —
   no env var or config edit is required. If no Application cert is present
   the build proceeds **unsigned** (Gatekeeper right-click-Open expected on
   first launch); pass `--no-sign` to skip detection deliberately.

### 2.2 Notarization profile

Notarization is opt-in via the `--notarize` flag and uses a stored `notarytool`
keychain profile (default name `agentshore-notary`). Create it once with
`xcrun notarytool store-credentials`, using either an App Store Connect API key
(preferred) or an Apple ID + app-specific password.

The build then runs `xcrun notarytool submit "$PKG" --keychain-profile
agentshore-notary --wait` followed by `xcrun stapler staple "$PKG"`. Use
`--keychain-profile NAME` on the build script to point at a different profile.

### 2.3 Hardened runtime entitlements

`bundle.macOS.hardenedRuntime` is `true` and the bundler applies
`desktop/src-tauri/entitlements.plist`, which enables four entitlements
required by the embedded Python sidecar:

| Key | Reason |
| --- | --- |
| `com.apple.security.cs.allow-jit` | CPython's regex engine and downstream libraries may JIT |
| `com.apple.security.cs.allow-unsigned-executable-memory` | Python and native dependencies may map executable memory |
| `com.apple.security.cs.disable-library-validation` | Allows loading Python dylibs not signed by the same team |
| `com.apple.security.cs.allow-dyld-environment-variables` | Lets the sidecar set `DYLD_*` for its managed venv |

If a signed build is rejected, inspect `Console.app` for
`com.apple.security.*` denials and add the matching entitlement.

## 3. Tauri updater keypair

Tauri's auto-updater verifies `latest.json` against a public key embedded in
the application. Generate the keypair once with `tauri signer generate` and
store it long-term.

Put the printed public key in `desktop/src-tauri/tauri.conf.json` at
`plugins.updater.pubkey` (replacing the `TAURI_SIGNING_PUBLIC_KEY`
placeholder). Store the private key off-host (1Password / a hardware key) —
losing it forces every existing install to re-bootstrap.

The updater fetches its manifest from
`https://github.com/SuperSwinkAI/Swink-AgentShore/releases/latest/download/latest.json`
(`plugins.updater.endpoints`). After a `tauri build`, sign each artifact with
`tauri signer sign` and assemble the manifest with
`scripts/generate_update_manifest.py`.

`generate_update_manifest.py` is a multi-platform helper and still accepts
`--sig-windows-x64` / `--sig-linux-x64` flags, but only the macOS signatures
are populated for current releases. Upload `latest.json` to the GitHub Release
alongside the `.dmg`/`.pkg`.

## 4. Pkg component layout

`productbuild` wraps three `pkgbuild` components into one distribution
installer (`packaging/desktop/Distribution.xml.in`), each a deliberate choice
in Installer.app's Customize panel:

| Component identifier | Payload | Choice |
| --- | --- | --- |
| `ai.agentshore.desktop` | `.app` + postinstall that provisions the managed sidecar venv from the bundled wheel | Required |
| `ai.agentshore.cli` | nopayload; `uv tool install` of the bundled wheel → `~/.local/bin/agentshore` | Opt-out |
| `ai.agentshore.timelapse` | nopayload; drives `install_timelapse()` (ffmpeg + Node + capture CLI) in the managed venv | Opt-in |

The desktop component pins `BundleIsRelocatable=false` (via an explicit
component plist) so the `.app` always installs to `/Applications`. The desktop
and timelapse components also patch their `pkgbuild` postinstall timeout from
600 s to 3600 s, since first-run provisioning pulls large dependencies.

## 5. Building a signed release

The whole pipeline is `scripts/build-macos.sh` with no flags (per repo
convention, "build" always means this). Useful variants are `--notarize`,
`--no-sign`, and `--no-pkg`.

Signing is automatic whenever the Developer ID certs are in the Keychain
(phases 6 and 9 of the script); no env vars are required. `--notarize`
implies the `.pkg` and requires the Installer cert plus the notarytool
keychain profile. Output lands in `desktop/dist/AgentShore.pkg` and the
Tauri bundle dir; the script reveals the `.pkg` in Finder.

## 6. Secrets handling

* Never commit a `.p12`, `.pfx`, `.cer`, `.p8`, `.mobileprovision`, or the
  Tauri private key. `desktop/src-tauri/.gitignore` excludes cert material by
  extension.
* The notarytool keychain profile and the Developer ID private keys live in
  the login Keychain / off-host secret storage — never in the repo or env
  files committed to it.
* Rotate the Developer ID certs at the start of each renewal cycle to avoid a
  release gap.
