# AgentShore Desktop - Code Signing & Installer Builds

This document is the maintainer runbook for producing desktop release builds of
AgentShore. The signed, notarized release path is still macOS-first and lives in
`scripts/build-macos.sh`. Windows now has a local user-level installer build
script at `scripts/build-windows.ps1`, but Authenticode signing and CI release
automation are not wired yet.

It complements `docs/design/desktop/DESIGN.md` section 6.5, "Code signing &
trust".

## 1. Overview

AgentShore Desktop bundles a Tauri 2 shell around a Python sidecar. The
AgentShore core is provisioned at install time into a managed venv from a wheel
shipped inside the platform installer.

The current shipping version is `0.2.1`
(`desktop/src-tauri/tauri.conf.json` version).

macOS `scripts/build-macos.sh` produces:

| Stage | Tool / command | Notes |
| --- | --- | --- |
| Resolve identity | `security find-identity -v -p codesigning` | Auto-detects the first Developer ID Application cert |
| Build and sign `.app` | `npx tauri build` | Hardened runtime plus entitlements; also emits the `.dmg` |
| Verify `.app` | `codesign --verify --deep --strict` | Only when an Application cert was found |
| Build `.pkg` | `pkgbuild` plus `productbuild` | Three components; signed with Developer ID Installer if present |
| Notarize, optional | `xcrun notarytool submit --wait` plus `xcrun stapler staple` | Requires Installer cert and notarytool profile |
| Publish | Upload manually to GitHub Releases | `.app`, `.dmg`, `.pkg` |
| Update manifest | `scripts/generate_update_manifest.py` to `latest.json` | Signed with the Tauri updater key |

Windows `scripts\build-windows.ps1` produces:

| Stage | Tool / command | Notes |
| --- | --- | --- |
| Build dashboard | `npm run build`, `npm run build:lib` | Same dashboard artifacts as macOS |
| Build sidecar | `npm run build:tauri-sidecars` | Stages `agentshore-bd.exe` |
| Build wheel | `uv build --wheel` | Bundled into the installer |
| Build app executable | `npx tauri build --no-bundle -- --locked` | Inno wraps the executable instead of Tauri NSIS/MSI |
| Build `.exe` wizard | Inno Setup 6 `ISCC.exe` | Emits `desktop\dist\AgentShoreSetup-<version>-x64.exe` |

## 2. macOS Developer ID

Two Developer ID certificates are used:

- **Developer ID Application** signs the `.app` through the Tauri bundler.
- **Developer ID Installer** signs the `.pkg` through `productbuild --sign`.
  It is required for notarization.

To provision them:

1. Enroll in the Apple Developer Program.
2. In Xcode Settings, Accounts, Manage Certificates, create both Developer ID
   certificate types.
3. Confirm they resolve from the login Keychain with `security find-identity`.

If no Application cert is present, the macOS build proceeds unsigned. Pass
`--no-sign` to skip detection deliberately.

## 3. Notarization

Notarization is opt-in via `--notarize` and uses a stored `notarytool`
keychain profile. The default profile name is `agentshore-notary`.

Create the profile once with `xcrun notarytool store-credentials`, using either
an App Store Connect API key or an Apple ID plus app-specific password.

The build then runs:

```bash
xcrun notarytool submit "$PKG" --keychain-profile agentshore-notary --wait
xcrun stapler staple "$PKG"
```

Use `--keychain-profile NAME` to choose a different profile.

## 4. Hardened Runtime

macOS signing config lives in `desktop/src-tauri/tauri.conf.json`
(`bundle.macOS`). Hardened-runtime entitlements live in
`desktop/src-tauri/entitlements.plist`.

The app enables entitlements needed by the embedded Python sidecar:

| Key | Reason |
| --- | --- |
| `com.apple.security.cs.allow-jit` | Python and downstream libraries may JIT |
| `com.apple.security.cs.allow-unsigned-executable-memory` | Python/native deps may map executable memory |
| `com.apple.security.cs.disable-library-validation` | Allows loading Python dylibs not signed by the same team |
| `com.apple.security.cs.allow-dyld-environment-variables` | Lets the sidecar set `DYLD_*` for its managed venv |

## 5. Tauri Updater Keypair

Tauri's auto-updater verifies `latest.json` against a public key embedded in
the app. Generate the keypair once with `tauri signer generate`.

Put the public key in `desktop/src-tauri/tauri.conf.json` at
`plugins.updater.pubkey`. Store the private key off-host. Losing it forces
existing installs to re-bootstrap.

The updater fetches:

```text
https://github.com/SuperSwinkAI/Swink-AgentShore/releases/latest/download/latest.json
```

After a Tauri build, sign each artifact with `tauri signer sign` and assemble
the manifest with `scripts/generate_update_manifest.py`.

## 6. Installer Component Layout

Both platform installers expose the same deliberate choices:

| Component | Payload | Default |
| --- | --- | --- |
| AgentShore Desktop | Desktop app plus managed sidecar venv from bundled wheel | Required |
| Timelapse Capture | Runs `install_timelapse()` for ffmpeg, Node, capture CLI, and browser deps | Opt-in |
| AgentShore CLI | Installs `agentshore[all]` from the bundled wheel via `uv tool install` | Opt-out |

The Windows installer is per-user (`PrivilegesRequired=lowest`) and installs
the desktop app under `%LocalAppData%\Programs\AgentShore`. The managed venv is
under `%LocalAppData%\AgentShore\venv`, matching the Rust supervisor's Windows
lookup path.

## 7. Secrets Handling

- Never commit `.p12`, `.pfx`, `.cer`, `.p8`, `.mobileprovision`, or the Tauri
  private updater key.
- `desktop/src-tauri/.gitignore` excludes certificate material by extension.
- Developer ID private keys and notarization profiles live in Keychain or
  off-host secret storage.
- Windows Authenticode signing is not implemented yet; add it before treating
  the Windows `.exe` as a trusted public release artifact.
