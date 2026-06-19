# Desktop Getting Started

Use this path when you want the packaged macOS desktop app or installer. If you want a terminal-first workflow, use the [CLI getting-started guide](getting-started-cli.md).

## What The Desktop Package Includes

The macOS desktop build packages:

- The AgentShore Tauri app.
- The browser dashboard assets used by the app.
- The Python AgentShore wheel and managed runtime environment.
- The bundled `bd` sidecar used for beads-backed project state.
- Optional timelapse capture support.
- An optional `agentshore` command-line component.

The shipping build command is:

```bash
uv run python -m scripts.buildkit macos
```

It produces the `.app`, `.dmg`, and `.pkg` artifacts under `desktop/dist/`.

## Install With The `.pkg`

Open `desktop/dist/AgentShore.pkg` and follow the installer choices:

- **Desktop App** is required and installs `AgentShore.app` into `/Applications`.
- **Command-line tool** is selected by default and installs the `agentshore` shell command.
- **Timelapse Capture** is optional and is not selected by default. You can install it later from the app start screen if you skip it during package install.

The package provisions AgentShore's managed Python environment at:

```text
/Library/Application Support/AgentShore/venv
```

That managed environment is what the packaged app uses when it launches the AgentShore sidecar.

## Install With The `.dmg` Or `.app`

Open the generated `.dmg` and drag `AgentShore.app` into `/Applications`, or run the generated `.app` directly for local verification. The `.pkg` path is preferred for production installs because it also provisions the managed runtime and optional CLI/timelapse components.

For development builds launched from the checkout, the app can fall back to the repository `.venv` when the packaged runtime is not installed.

## First Launch

On first launch, choose the project repository you want AgentShore to manage. The app runs readiness checks and guides you through the same core setup as `agentshore init`:

- GitHub access and local repository checks.
- Agent CLI availability checks.
- Agent identity and trusted-author configuration.
- Target branch, budget, and session settings.

The agents/identities setup screen also shows a per-agent **backend auth** badge. This is separate from the GitHub identity check: it reflects the session each CLI agent uses to reach its model provider (for Codex, its cached `chatgpt.com` login, which carries a TTL and can expire between runs). The same check runs as a startup phase when you click Start — if an agent's backend session has expired, startup stops and points you at the failing agent with a fix hint (for example, run `codex login` in a terminal, then start again).

The app writes the project configuration to `agentshore.yaml` in the selected repository. You can later adjust the same project from either the desktop app or the CLI.

## Menus And Updates

The desktop app has a standard native menu bar:

- **File** — Adjust Budget…, Stop Session, and Close Window for the running session.
- **Help** — Documentation, Release Notes, and Report an Issue (open in your browser); a **Keyboard Shortcuts** cheat-sheet; **Open Log Folder** (reveals the active project's `.agentshore/logs`); and **Copy Diagnostics** (app version and platform, for bug reports).
- **Preferences…** (Cmd+,) — a placeholder today; desktop preferences are coming.

The app **checks for updates on launch** and only prompts you when a newer version exists; you can also check on demand via **Check for Updates…** (in the application menu on macOS, the Help menu elsewhere). Accepting an update downloads, installs, and restarts the app.

## Gatekeeper, Signing, And Notarization

Release builds are Developer ID signed when the required certificates are available. Local builds may be ad-hoc signed or unsigned depending on your machine configuration, so macOS Gatekeeper prompts are expected for local-only artifacts.

Notarization is an explicit release step. See [`release/signing.md`](release/signing.md) for the Apple credential and signing profile requirements.

## Build It Yourself

From the AgentShore checkout:

```bash
uv sync --group dev
uv run python -m scripts.buildkit macos
```

The build spine validates the app payload before finishing, including version mirrors, bundled binaries, and signature state. Use the generated artifacts in `desktop/dist/` for local installation or release packaging.
