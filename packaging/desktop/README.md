# Desktop sidecar packaging

Tooling for the `.pkg` installer that ships the AgentShore desktop shell.
Implements `docs/design/desktop/DESIGN.md` §6.2 (pkg-installer model).

## Architecture

The Tauri `.app` ships a thin Rust supervisor + JS shell only. The Python
sidecar (`agentshore.sidecar`) is **not** bundled inside the `.app`. Instead the
`.pkg` installer provisions a managed venv at a fixed system path and
pip-installs a bundled agentshore wheel into it. The Rust supervisor then
spawns `<venv>/bin/python -m agentshore.sidecar`.

| Platform | Managed venv path                                       |
| -------- | ------------------------------------------------------- |
| macOS    | `$HOME/Library/Application Support/AgentShore/venv`        |
| Linux    | `$HOME/.local/share/agentshore/venv`                       |
| Windows  | `%USERPROFILE%\AppData\Local\AgentShore\venv`              |

Rationale: avoids bundling a 350+MB PyInstaller `--onedir` (CPython +
libtorch + numpy), keeps the installer artifact small, allows agentshore code
updates to ship as a new wheel without re-downloading PyTorch, and shifts
dependency-install failures to install-time UX where they can be surfaced
loudly instead of producing broken-but-runnable `.app` bundles.

## Layout

```
packaging/desktop/
├── README.md               (this file)
├── build_bd_sidecar.py     (build the bundled bd sidecar binary)
└── src-tauri/binaries/     (generated; .gitignored)
```

The bundled `bd` CLI (a single static Go binary) is the only externalBin
that stays inside the `.app`. The Rust supervisor passes its path to the
Python sidecar via `AGENTSHORE_BD_BIN` so the sidecar can shell out to `bd`.

## bd sidecar build

```bash
python packaging/desktop/build_bd_sidecar.py --bd "$(which bd)" --out desktop/src-tauri/binaries
```

Output: `desktop/src-tauri/binaries/agentshore-bd/agentshore-bd` (and a
`agentshore-bd-<target-triple>` copy that Tauri's `externalBin` expects).

## agentshore wheel build

The wheel that the `.pkg` postinstall pip-installs is built from the repo
root with `uv`:

```bash
uv build --wheel
```

Output: `dist/agentshore-<version>-py3-none-any.whl`. The `.pkg` payload
includes that wheel in both component payloads — the desktop sidecar
component (`install-agentshore-venv.sh`) and the CLI component
(`install-agentshore-cli.sh`).

## Installer flow

The `.pkg` is a distribution archive with two user-visible components
declared in `Distribution.xml.in`:

- **AgentShore Desktop** (`ai.agentshore.desktop`) — required; greyed out in
  the wizard's Customize panel.
- **AgentShore CLI** (`ai.agentshore.cli`) — opt-out checkbox; installs the
  shell `agentshore` command via `uv tool install`.

Each component carries its own postinstall:

1. **Desktop postinstall** (`postinstall`) — runs as root, then:
   - Verifies Python 3.12 is present; downloads + installs python.org's
     signed .pkg if not (signature-checked).
   - Hands off to `install-agentshore-venv.sh` under the console user via
     `launchctl asuser` to provision
     `~/Library/Application Support/AgentShore/venv` from the bundled wheel.
   - Smoke-tests `python -c "import agentshore.sidecar"` and schedules a
     one-shot LaunchAgent that waits for Installer to close, then opens
     `/Applications/AgentShore.app` by absolute path.
2. **CLI postinstall** (`cli-postinstall`) — runs as root, then hands
   off to `install-agentshore-cli.sh` under the console user. The helper:
   - Searches well-known per-user locations for `uv`
     (`~/.local/bin/uv`, `~/.cargo/bin/uv`, `/opt/homebrew/bin/uv`,
     `/usr/local/bin/uv`, `/opt/local/bin/uv`) — the .pkg postinstall's
     sparse PATH normally misses these.
   - Bootstraps uv via the official installer if absent.
   - Runs `uv tool install --force --reinstall --from <wheel>
     'agentshore[all]'` so `~/.local/bin/agentshore` tracks the same wheel as
     the desktop sidecar.
   - Surfaces failure via an osascript alert (silent skip was the bug
     this component split was added to fix).

The build pipeline for the `.pkg` artifact lives in
`scripts/build-macos.sh`. Local-only signing uses the
`Developer ID Application` certificate; the `.pkg` itself requires the
`Developer ID Installer` certificate (acquisition tracked separately).

## Build identifier

In the pkg-installer model both halves run unfrozen, so
`agentshore.sidecar.build_id.load_build_info()` returns the sentinel
`"dev"` (no `sys._MEIPASS`). The Rust supervisor's `resolve_build_id()`
falls back to `"dev"` for the same reason, so the handshake build-ids match
by construction.

A future change can re-introduce build-id mismatch detection if version
drift between the installed wheel and the bundled `.app` becomes a real
operational concern. For now the wheel version itself is the authoritative
identifier of what's installed.

## Running the sidecar in development

```bash
python -m agentshore.sidecar
```

The Rust supervisor's `sidecar_command()` falls back to `uv run python -m
agentshore.sidecar` when the managed venv path is absent — running
`npm run tauri:dev` from `desktop/` against a clean clone Just Works.

## Verification

`tests/sidecar/` covers the JSON-RPC handshake and build-id loading.
End-to-end install flow is exercised via the desktop CI pipeline
(`desktop-c8i.7`).
