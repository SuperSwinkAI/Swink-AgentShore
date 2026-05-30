# AgentShore Desktop — Code Signing & Notarization

This document is the maintainer reference for producing signed, notarized
release builds of the AgentShore Desktop application across macOS and Windows.
It complements `docs/design/desktop/DESIGN.md` §6.5 ("Code signing & trust")
and §7.2 ("Release CI"). It does **not** define the GitHub Actions workflow —
the CI release job is owned by the human maintainer and is out of scope for
the project's automated agents.

## 1. Overview

AgentShore Desktop bundles a Tauri shell around a Python sidecar (the AgentShore
core, frozen with PyInstaller). To ship a release that does not trigger
Gatekeeper or SmartScreen warnings, every distributable artefact must be
signed and — on macOS — notarized by Apple. The pipeline at a glance:

| Stage | macOS | Windows |
| --- | --- | --- |
| Compile | `cargo build --release` (universal2) | `cargo build --release` (x86_64) |
| Sign sidecar | `codesign --options runtime --entitlements entitlements.plist` | `signtool sign /fd sha256 /tr <timestampUrl>` |
| Sign app/installer | Tauri bundler with `signingIdentity` | Tauri bundler with `certificateThumbprint` |
| Notarize | `xcrun notarytool submit … --wait` | _(not applicable)_ |
| Staple | `xcrun stapler staple` | _(not applicable)_ |
| Publish | Upload to GitHub Releases | Upload to GitHub Releases |
| Update manifest | `scripts/generate_update_manifest.py` signs `latest.json` with the Tauri updater key | same |

The signing configuration lives in `desktop/src-tauri/tauri.conf.json`
(`bundle.macOS` and `bundle.windows`). The hardened-runtime entitlements
live in `desktop/src-tauri/entitlements.plist`.

## 2. macOS Developer ID

### 2.1 Certificate

1. Enroll in the Apple Developer Program (~$99/yr).
2. In Xcode → Settings → Accounts → Manage Certificates, create a
   "Developer ID Application" certificate. (For a `.pkg` installer also create
   "Developer ID Installer".)
3. Export both as `.p12` from Keychain Access (right-click → Export). Use a
   strong passphrase.
4. Base64-encode the `.p12` for CI consumption:
   ```sh
   base64 -i DeveloperID.p12 -o DeveloperID.p12.b64
   ```
   The contents of `DeveloperID.p12.b64` become the `APPLE_CERTIFICATE`
   secret; the passphrase becomes `APPLE_CERTIFICATE_PASSWORD`.
5. Capture the identity string Tauri will pass to `codesign`:
   ```sh
   security find-identity -v -p codesigning
   ```
   Use the full string, e.g. `Developer ID Application: AgentShore Labs LLC (TEAMID)`.
   That value becomes `APPLE_SIGNING_IDENTITY`.

### 2.2 Notarization

`notarytool` requires either an Apple ID + app-specific password or an App
Store Connect API key. The API-key path is preferred for CI:

1. App Store Connect → Users and Access → Keys (Team) → Generate API key
   with `Developer` role.
2. Download the `.p8` private key and capture the Key ID and Issuer ID. The
   private key contents become `APPLE_API_KEY`, the Key ID becomes
   `APPLE_API_KEY_ID`, and the Issuer ID becomes `APPLE_API_ISSUER`.
3. If you must fall back to the Apple ID path, generate an app-specific
   password at https://account.apple.com/account/manage and supply
   `APPLE_ID` + `APPLE_PASSWORD` + `APPLE_TEAM_ID`.

### 2.3 Hardened runtime entitlements

`desktop/src-tauri/entitlements.plist` enables four entitlements required by
the embedded Python sidecar:

| Key | Reason |
| --- | --- |
| `com.apple.security.cs.allow-jit` | CPython's regex engine and downstream libraries may JIT |
| `com.apple.security.cs.allow-unsigned-executable-memory` | PyInstaller bootloader maps the frozen archive as RWX |
| `com.apple.security.cs.disable-library-validation` | Allows the bundle to load Python dylibs not signed by the same team |
| `com.apple.security.cs.allow-dyld-environment-variables` | Lets the bootloader set `DYLD_LIBRARY_PATH` for the sidecar |

If a signed build is rejected, inspect `Console.app` for
`com.apple.security.*` denials and add the matching entitlement.

## 3. Windows OV/EV cert

### 3.1 Choosing a certificate

| Type | First-launch UX | Cost (approx) | Notes |
| --- | --- | --- | --- |
| OV (Organization Validation) | SmartScreen warning until enough installs build reputation | $200–$400/yr | Slow ramp; users may see "Windows protected your PC" |
| EV (Extended Validation) | Trusted immediately by SmartScreen | $300–$600/yr | Requires hardware token (FIPS dongle) — limits CI to dedicated runners |

For an automated CI signer, OV is the only option that works without a
hardware key. Vendors: DigiCert, SSL.com, Sectigo. The hardware-token EV path
typically means signing on a maintainer's workstation, not CI.

### 3.2 Provisioning the secret

1. Export the cert + chain as `.pfx` (PKCS#12) and base64-encode it:
   ```sh
   base64 -w 0 codesign.pfx > codesign.pfx.b64
   ```
2. The base64 string becomes the `WINDOWS_CERTIFICATE` secret; the export
   passphrase becomes `WINDOWS_CERTIFICATE_PASSWORD`.
3. Capture the cert's SHA1 thumbprint (PowerShell):
   ```powershell
   (Get-PfxCertificate -FilePath codesign.pfx).Thumbprint
   ```
   That value becomes `WINDOWS_CERTIFICATE_THUMBPRINT`.

### 3.3 Timestamp URL

`desktop/src-tauri/tauri.conf.json` defaults `bundle.windows.timestampUrl` to
`http://timestamp.digicert.com`. Override it if your CA recommends a
different timestamp service:

| CA | Timestamp URL |
| --- | --- |
| DigiCert | `http://timestamp.digicert.com` |
| Sectigo | `http://timestamp.sectigo.com` |
| SSL.com | `http://ts.ssl.com` |

The timestamp is what keeps a signed installer trusted after the signing
certificate expires, so it must always be present.

## 4. Tauri updater keypair

Tauri's auto-updater verifies `latest.json` against a public key embedded in
the application. Generate the keypair once and store it long-term:

```sh
cargo install tauri-cli --version "^2"
tauri signer generate -w updater-key.pem
```

This produces a private key (`updater-key.pem`) and prints the public key.
Put the public key in `desktop/src-tauri/tauri.conf.json` at
`plugins.updater.pubkey` (replacing the `TAURI_SIGNING_PUBLIC_KEY`
placeholder). Store the private key off-host (1Password / a hardware key) —
losing it forces every existing install to re-bootstrap.

For CI, hold the private key in the `TAURI_SIGNING_PRIVATE_KEY` secret and
its passphrase in `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`. The
`scripts/generate_update_manifest.py` script consumes the signatures Tauri
emits during `tauri build` and embeds them in `latest.json` for publication
alongside the release artefacts.

## 5. Required CI env vars

The release workflow (when one is added under `.github/workflows/`) must
provide the following variables. They are the contract between this repo's
signing config and any CI implementation:

| Variable | Purpose |
| --- | --- |
| `APPLE_CERTIFICATE` | Base64-encoded Developer ID Application `.p12` |
| `APPLE_CERTIFICATE_PASSWORD` | Passphrase for the above |
| `APPLE_SIGNING_IDENTITY` | Full `Developer ID Application: …` string |
| `APPLE_TEAM_ID` | 10-character team identifier |
| `APPLE_API_KEY` | App Store Connect `.p8` key contents (preferred) |
| `APPLE_API_KEY_ID` | Key ID for the above |
| `APPLE_API_ISSUER` | Issuer ID for the App Store Connect key |
| `APPLE_ID` | Apple ID email (fallback path) |
| `APPLE_PASSWORD` | App-specific password (fallback path) |
| `WINDOWS_CERTIFICATE` | Base64-encoded code-signing `.pfx` |
| `WINDOWS_CERTIFICATE_PASSWORD` | Passphrase for the above |
| `WINDOWS_CERTIFICATE_THUMBPRINT` | SHA1 thumbprint of the cert |
| `TAURI_SIGNING_PRIVATE_KEY` | Tauri updater private key |
| `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | Passphrase for the above |
| `TAURI_SIGNING_PUBLIC_KEY` | Embedded in `tauri.conf.json` (not secret) |

The CI job is expected to run macOS bundle steps on `macos-latest` and
Windows bundle steps on `windows-latest`, then call
`scripts/generate_update_manifest.py` from any runner to assemble the final
`latest.json`.

## 6. Local signed builds

A maintainer can produce a signed build on their own machine by setting the
same env vars before invoking the Tauri CLI:

```sh
# macOS
export APPLE_SIGNING_IDENTITY="Developer ID Application: AgentShore Labs LLC (TEAMID)"
export APPLE_API_KEY=...
export APPLE_API_KEY_ID=...
export APPLE_API_ISSUER=...
cd desktop && npm run tauri -- build

# Windows (PowerShell)
$env:WINDOWS_CERTIFICATE_THUMBPRINT = "ABCDEF..."
cd desktop; npm run tauri -- build
```

If the env vars are absent, the Tauri bundler skips the signing step and
produces an unsigned dev build — useful for iterating locally. The `null`
defaults in `bundle.macOS.signingIdentity` and
`bundle.windows.certificateThumbprint` exist specifically so unsigned dev
builds keep working without modifying the config.

## 7. Secrets handling

* Never commit a `.p12`, `.pfx`, `.cer`, `.mobileprovision`, or
  `entitlements.local.plist` — `desktop/src-tauri/.gitignore` excludes them
  by extension.
* Never paste a base64-encoded certificate into chat or logs. Treat the
  base64 form as equivalent to the private key.
* Rotate the Windows cert at least 30 days before expiry to avoid a release
  gap. Rotate the macOS Developer ID cert at the start of each renewal cycle.
