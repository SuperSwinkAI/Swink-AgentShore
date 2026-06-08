# Desktop Sidecar Packaging

Tooling for the macOS `.pkg` and Windows `.exe` installers that ship the
AgentShore desktop shell. The shared model is: keep the Tauri app thin, ship a
platform app shell, and provision the Python sidecar into a managed
venv from the exact wheel built with the installer. macOS bundles the pinned
`bd` sidecar beside the desktop executable; Windows provisions the pinned `bd`
dependency during the managed venv install to avoid shipping an unsigned extra
binary.

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

On macOS, the bundled `bd` CLI is the only external binary that stays beside
the desktop executable. The Rust supervisor passes its path to the Python
sidecar via `AGENTSHORE_BD_BIN`. On Windows, `install-agentshore-venv.ps1`
drives `agentshore.beads.setup.provision_bd()` after installing the wheel.

## Build Inputs

`packaging/desktop/build_bd_sidecar.py` builds the bundled `bd` binary into
`desktop/src-tauri/binaries/`. By default it **downloads the pinned `bd`
release** for the build host's OS/arch from the beads GitHub releases and
verifies its SHA-256 against the checksum table in the script before bundling —
so the shipped `.app` is reproducible and version-correct regardless of what
`bd` (if any) is on the build machine's PATH. Pass `--bd PATH` to bundle a
local binary instead (offline/CI builds).

The version (`PINNED_BD_VERSION`) and checksums are kept in lockstep with the
runtime pin (`agentshore.beads.setup.REQUIRED_BD_VERSION`);
`tests/sidecar/test_bd_sidecar.py` fails if they drift. To bump bd, update both
constants and refresh the checksums from the release's `checksums.txt`.

The Python wheel is built from the repo root with `uv build --wheel`. Both
platform installers include that wheel for the desktop sidecar component and
the optional CLI component.

## macOS Installer

`scripts/build-macos.sh` builds the dashboard, bd sidecar, Tauri app, Python
wheel, Tauri `.app`, `.dmg`, and distribution `.pkg`.

The `.pkg` has three user-visible choices in `Distribution.xml.in`:

- **AgentShore Desktop** (`ai.agentshore.desktop`) - required.
- **Timelapse Capture** (`ai.agentshore.timelapse`) - opt-in.
- **AgentShore CLI** (`ai.agentshore.cli`) - opt-out.

The desktop postinstall provisions the managed venv from the bundled wheel. The
Timelapse postinstall drives `agentshore.timelapse.setup.install_timelapse()`
from that venv. The CLI postinstall installs `agentshore[all]` from the same
wheel via `uv tool install`.

## Windows Installer

`scripts/build-windows.ps1` builds the dashboard, Tauri frontend, Python wheel,
Tauri executable, regenerates `EULA.rtf`, optionally Authenticode-signs the app
and setup executable, and emits an Inno Setup wizard `.exe`.

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

Bundled Windows helper scripts:

- `scripts/install-agentshore-venv.ps1` provisions
  `%ProgramData%\AgentShore\venv` from the bundled wheel and provisions `bd`.
- `scripts/install-agentshore-cli.ps1` installs `agentshore` from the bundled
  wheel with `uv tool install --force --reinstall --python 3.12`.
- `scripts/install-timelapse.ps1` drives the canonical timelapse installer from
  the managed venv.
- `scripts/run-windows-installer-step.ps1` wraps helper execution and writes
  logs under `%LocalAppData%\AgentShore\install-logs`.

The installer removes the previous internal per-user desktop install under
`%LocalAppData%\Programs\AgentShore` and the previous per-user managed venv
under `%LocalAppData%\AgentShore\venv` during install.

## Build Identifier

In this installer model both halves run unfrozen, so
`agentshore.sidecar.build_id.load_build_info()` returns `"dev"` without
`sys._MEIPASS`. The Rust supervisor's `resolve_build_id()` falls back to
`"dev"` for the same reason. The wheel version is the authoritative installed
runtime version.

## Development

When the managed venv is absent, the Rust supervisor falls back to
`uv run python -m agentshore.sidecar`, so `npm run tauri:dev` from `desktop/`
works against a clean checkout.

## Verification

`tests/sidecar/` covers the JSON-RPC handshake and build-id loading.
`tests/packaging/test_windows_installer.py` guards the Windows installer
component defaults and build-script staging contract.
