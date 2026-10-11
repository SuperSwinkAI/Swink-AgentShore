# Desktop Sidecar Packaging

Tooling for the macOS `.pkg` and Windows `.exe` installers that ship the
AgentShore desktop shell. The shared model is: keep the Tauri app thin, ship a
platform app shell, and provision the Python sidecar into a managed
venv from the exact wheel built with the installer. Neither installer ships
`bd`: AgentShore uses the system `bd` (Homebrew/npm) and `agentshore init`
offers to install or upgrade it through that package manager.

## Architecture

The Tauri desktop bundle ships a Rust supervisor plus JS shell only.
`agentshore.sidecar` is not frozen into the desktop executable. Instead, the
installer provisions a managed venv and pip-installs the bundled AgentShore
wheel into it. The Rust supervisor then spawns:

```text
<venv>/python -m agentshore.sidecar
```

Managed venv paths:

- macOS: `$HOME/Library/Application Support/AgentShore/venv`
- Windows: `%ProgramData%\AgentShore\venv`

No external binaries ship beside the desktop executable. The Python sidecar
resolves `bd` from PATH (with the user's brew/npm dirs overlaid) and refuses
versions below `agentshore.beads.setup.SUPPORTED_BD_VERSION`; newer releases
are accepted with a warning.

## Build Inputs

The Python wheel is built from the repo root with `uv build --wheel`. Both
platform installers include that wheel for the desktop sidecar component and
the optional CLI component.

## macOS Installer

`uv run python -m scripts.buildkit macos` builds the dashboard,
Tauri app, Python wheel, Tauri `.app`, `.dmg`, and distribution `.pkg`.

The `.pkg` has three user-visible choices in `Distribution.xml.in`:

- **AgentShore Desktop** (`ai.agentshore.desktop`) - required.
- **Timelapse Capture** (`ai.agentshore.timelapse`) - opt-in.
- **AgentShore CLI** (`ai.agentshore.cli`) - opt-out.

The desktop postinstall provisions the managed venv from the bundled wheel. The
Timelapse postinstall drives `agentshore.timelapse.setup.install_timelapse()`
from that venv. The CLI postinstall installs `agentshore[all]` from the same
wheel via `uv tool install`.

## Windows Installer

`uv run python -m scripts.buildkit windows` builds the dashboard, Tauri frontend,
Python wheel, Tauri executable, compiled Windows provisioner, regenerates
`EULA.rtf`, optionally Authenticode-signs the app/provisioner/setup executables
(via the `scripts/buildkit/_win_signing.ps1` carve-out), and emits an Inno Setup
wizard `.exe`.

For local installer QA, pass `-SelfSign` to create or reuse a current-user
self-signed code-signing certificate. Add `-TrustSelfSignedCertificate` when the
build machine should trust that certificate for local signature verification, or
`-SetupSelfSignedCertificateOnly` to provision the certificate without building.
Self-signed installers are never public release artifacts.

The Windows wizard is machine-wide (`PrivilegesRequired=admin`) and installs
the desktop app under:

```text
%ProgramFiles%\AgentShore
```

It uses `packaging/desktop/windows/AgentShore.iss.in` and mirrors the macOS
component defaults:

- **AgentShore Desktop** - required.
- **Timelapse Capture (optional)** - unchecked by default.
- **AgentShore CLI** - checked by default.

Bundled Windows install-time payloads:

- `agentshore-provisioner.exe` owns Windows post-install provisioning. It uses
  direct process execution, timeout-aware logging, and stable exit codes.
- `uv.exe` is copied from the build host and pinned by the build script to the
  expected baseline before packaging.
- The bundled wheel is installed into `%ProgramData%\AgentShore\venv`.
- The pinned `bd.exe` dependency is provisioned into
  `%ProgramData%\AgentShore\bin`.

Provisioning logs are written under `%ProgramData%\AgentShore\install-logs`.

The installer removes the previous internal per-user desktop install under
`%LocalAppData%\Programs\AgentShore` and the previous per-user managed venv
under `%LocalAppData%\AgentShore\venv` from the provisioner during install.

## Build Identifier

In this installer model both halves run unfrozen, so
`agentshore.sidecar.build_id.load_build_info()` returns `"dev"` without
`sys._MEIPASS`. The Rust supervisor's `resolve_build_id()` falls back to
`"dev"` for the same reason. The wheel version is the authoritative installed
runtime version.

## Development

When the managed venv is absent, the Rust supervisor falls back to
the checkout's `.venv` Python, then `uv run python -m agentshore.sidecar` if
no `.venv` exists, so `npm run tauri:dev` works against a clean checkout.

## Verification

`tests/sidecar/` covers the JSON-RPC handshake and build-id loading.
`tests/packaging/test_windows_installer.py` guards the Windows installer
component defaults and build-script staging contract.
