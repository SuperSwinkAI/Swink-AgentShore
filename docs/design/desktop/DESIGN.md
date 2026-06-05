# AgentShore Desktop — Design Decisions

Living companion to [LAYER_CAKE.html](LAYER_CAKE.html) (architectural overview)
and [ONBOARDING_STARTUP_MOCKUPS.html](ONBOARDING_STARTUP_MOCKUPS.html) (UI
flow). This document records the resolved design decisions for the AgentShore
desktop app (version 0.2.1).

When this document and the HTML companions disagree, this document wins.

## Scope

AgentShore Desktop is a Tauri 2 native shell (macOS) that supervises a
AgentShore session end-to-end: project selection, readiness inspection, GitHub
identity and agent configuration, session lifecycle, dashboard monitoring, and
the End Session Report.

It is a lifecycle control plane, not just a packaged dashboard. The shell talks
to a versioned machine protocol; no CLI output is ever parsed.

---

## 1. Components and layering

Three layers ship together and are version-matched at install time:

| Layer | Tech | Role |
|-------|------|------|
| Tauri shell | Rust supervisor + WebView | Window, process supervision, JSON-RPC control plane, native dialogs |
| Frontend | React + Vite + React Router | Setup screens, dashboard, ESR; renders inside the WebView |
| Python sidecar | asyncio process (`agentshore.sidecar`) | Implements lifecycle RPC via internal AgentShore APIs; hosts the orchestrator and dashboard WebSocket bridge |

**Why a Python sidecar, not a port to Rust.** The orchestrator core is already
pure asyncio. The sidecar implements each RPC method against internal AgentShore
APIs, so it reuses real exception types and structured errors. A purpose-built
JSON-RPC contract sits in front so individual methods can swap call paths over
time without churning the shell.

**Why the frontend is shared.** The dashboard is an npm workspace package
(`@agentshore/dashboard`, see [../dashboard](../dashboard)). The desktop shell
imports its React components; the CLI `agentshore dashboard` builds the same
package standalone. One source of truth means protocol-sync fixes benefit both
surfaces and they cannot drift.

---

## 2. Process and lifecycle

### 2.1 Single process

One Python process. The sidecar RPC server, the orchestrator, the dashboard
WebSocket bridge, and AgentShore's existing IPC server all run as cooperative
tasks in the same asyncio loop. Splitting into a child process would duplicate
the supervision Tauri already provides and add an internal IPC hop for no
benefit.

### 2.2 One sidecar per app lifetime

A single sidecar starts when Tauri launches and lives until Tauri quits;
`project.select(path)` switches the active project context. Per-project respawn
would add cold-start latency to every recents click for no gain.

### 2.3 Crash recovery — surface, do not auto-restart

When the sidecar dies, the Rust supervisor emits a crash event and the WebView
routes to a recovery screen (logs, open log file, restart sidecar, kill agent
subprocesses, quit). Silent auto-restart masks crash loops; a loud failure mode
forces real bugs to surface and preserves logs.

---

## 3. Protocols

### 3.1 Channel split

JSON-RPC over the sidecar's stdio owns out-of-session lifecycle commands.
AgentShore's existing WebSocket IPC channel owns in-session commands unchanged.
The split is clean along orchestrator-not-running vs orchestrator-running, with
zero churn to the dashboard's command path.

### 3.2 Control plane — JSON-RPC 2.0 over stdio

The Rust supervisor and the sidecar speak JSON-RPC 2.0, framed line-by-line over
stdin/stdout. Tauri natively pipes sidecar stdio, so process lifecycle equals
channel lifecycle — no port allocation, firewall prompts, or socket-cleanup
races. Long-running calls use LSP-style `$/progress` notifications and
`$/cancelRequest` for cooperative cancellation (avoiding a mid-write SIGKILL and
the SQLite corruption risk it carries).

### 3.3 State plane — embedded WebSocket bridge

The dashboard bridge runs as another asyncio task inside the sidecar; the WebView
connects over a loopback WebSocket. This is the same code path as CLI `agentshore
dashboard`, so the two surfaces cannot diverge.

### 3.4 Handshake — `build_id` verification

Shell and sidecar are bundled together. The first call is `app.handshake`, whose
response carries protocol version, agentshore version, sidecar `build_id`, and
capabilities. A `build_id` mismatch means a corrupt install or a foreign sidecar
and is fatal. In the current pkg-installer model the sidecar runs unfrozen, so
both sides report the `dev` sentinel and match by construction; the frozen-bundle
`build_id` path is retained for a future self-contained `.app` variant.

---

## 4. State and persistence

- **Setup state** writes through to `agentshore.yaml` on every identity/agent
  edit — no in-memory draft. `session.start` boots the orchestrator against the
  on-disk config, matching `agentshore init --force` semantics.
- **Domain state** (recents) lives with the sidecar under the platform user-data
  dir so future frontends can reuse it. **UI-only state** (window, theme, last
  tab) lives in Tauri's store. Clean ownership: Python knows projects, Rust knows
  the window.

---

## 5. API surface (v1)

Lifecycle (`app.handshake`), recents, project (select / inspect / branches /
set-target-branch / deselect), identities, agents, config, session (start /
status / stop), and archive (list / fetch report / fetch logs). Sidecar-to-shell
notifications cover progress, session completion, sidecar health, and agent
subprocess spawn/exit; shell-to-sidecar covers cancellation.

In-session commands (pause, resume, drain, feedback, abort/override play, budget
adjust, verification response, report generation) stay on the existing WebSocket
channel and are out of scope for the JSON-RPC control plane.

---

## 6. Packaging and distribution

The single build entry point is `scripts/build-macos.sh` (macOS only). With no
flags it builds, signs, and reveals the installer; flags exist to skip phases,
install, or notarize. Output artifacts: a signed `.app`, a `.dmg`, and a
distribution `.pkg`.

### 6.1 Build pipeline

| Phase | Produces |
|-------|----------|
| Dashboard build | bridge static bundle (served by the WebSocket bridge) + `@agentshore/dashboard` lib bundle |
| `bd` sidecar | bundled `bd` binary staged as a Tauri `externalBin` (`agentshore-bd`) |
| Tauri frontend | shell + dashboard assets, with the dashboard static dir mounted as a Tauri resource |
| Python wheel | `uv build --wheel`, shipped inside the `.pkg` |
| Tauri bundle | the signed `.app` (hardened runtime + `entitlements.plist`, min macOS 13) |
| `pkgbuild` / `productbuild` | the three-component distribution `.pkg` |

### 6.2 Python sidecar — wheel in the .pkg, managed venv

The sidecar ships as a pip-installable wheel embedded in the `.pkg`. A postinstall
step provisions a managed venv (under Application Support) from that wheel, so the
desktop's Python always matches the build the shell shipped from. This replaces an
earlier PyInstaller frozen-bundle design that hit Tauri `externalBin` integration
issues; install-time provisioning surfaces failures early and lets agentshore
updates ship as a new wheel. PyTorch dominates installed size; trimming the venv
to sidecar-relevant deps is a follow-up.

### 6.3 Distribution package — three deliberate choices

`productbuild` wraps three component packages so Installer.app's Customize panel
shows them as explicit user-visible choices:

- **AgentShore Desktop** (required) — the `.app` plus the venv-provisioning
  postinstall.
- **Timelapse Capture** (opt-in) — scripts-only; drives the timelapse install
  recipe (ffmpeg + Node + capture CLI) inside the venv the Desktop component
  provisioned.
- **AgentShore CLI** (opt-out) — scripts-only; `uv tool install` of the same
  bundled wheel for `agentshore` on PATH.

Component install order is Desktop → Timelapse → CLI, because Timelapse depends on
the venv the Desktop component creates. Provisioning postinstalls raise the
default installer timeout to tolerate large dependency downloads.

### 6.4 Beads CLI — bundled

`bd` ships as a Tauri-managed sidecar binary; AgentShore resolves it via an env
var set by the supervisor, falling back to PATH for CLI mode. beads is
non-optional infrastructure, so bundling removes the most common first-run
failure.

### 6.5 Code signing, notarization, and auto-update

When the certs are present in the Keychain, the build auto-signs the `.app` with
Developer ID Application and the `.pkg` with Developer ID Installer; absent certs
produce unsigned output (right-click-Open on first launch). Optional
notarization submits the `.pkg` via `notarytool` and staples it. Auto-update uses
Tauri 2's built-in updater pointed at `latest.json` in GitHub Releases.

See `docs/release/signing.md` for the maintainer procedure.

---

## 7. Repo layout

| Path | Contents |
|------|----------|
| `src/agentshore/` | Python core, including `sidecar/` (JSON-RPC server) |
| `dashboard/` | `@agentshore/dashboard` workspace package |
| `desktop/src-tauri/` | Rust supervisor |
| `desktop/src/` | React shell (Vite + React Router) |
| `packaging/desktop/` | installer scripts, distribution template, EULA |
| `docs/design/desktop/` | design docs |

---

## 8. Non-functional baseline

| Dimension | Target |
|-----------|--------|
| Cold start | App interactive < 3s on NVMe SSD |
| Handshake | Completes < 1s after sidecar spawn |
| Idle / active memory | Sidecar < 250MB idle; sidecar + tracked agents < 1.5GB |
| OS support | macOS 13+ |
| Offline | `project.inspect`, recents, and identity edits work offline |
| Accessibility | Full keyboard navigation in v1; screen-reader support deferred |

---

## 9. Deferred / out of scope for v1

- Telemetry and remote crash reporting (local crash screen only).
- Multi-window (use recents to switch projects).
- Native menus and enumerated keyboard shortcuts.
- Localization (English only).
- Windows / Linux packaging (macOS only today).
- `agentshore.yaml` schema changes (desktop writes the schema the CLI emits).

---

## 10. Onboarding and startup UX flow

- **Re-entry.** Returning users step through all setup screens every session;
  choices pre-populate from `agentshore.yaml` for confirmation. Post-ESR returns
  to the first screen.
- **Lazy preparation.** Config merge, skill install/update, GitHub login
  binding, and beads init run only when the user clicks Start. Setup screens are
  declarative.
- **Rail navigation.** The left rail is freely navigable; the Start button is
  the single completeness gate. The rail hides during the dashboard and ESR.
- **Start gate.** Enabled when a target branch is selected, at least two agent
  runners are enabled, and at least two GitHub identities are assigned;
  everything else has working defaults.
- **Readiness hard blocks (safety only).** Continue is blocked only when the repo
  is the AgentShore source directory or no Git repository exists; all other
  readiness items are informational.
- **Identity sharing.** Runners may share a GitHub login; the Code Review
  anti-bias invariant is enforced by the play engine at runtime, not the setup
  UI.
- **Startup failure.** A failing startup step turns red inline, shows the error,
  and offers a contextual repair action.
