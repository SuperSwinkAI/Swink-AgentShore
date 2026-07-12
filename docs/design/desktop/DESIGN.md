# AgentShore Desktop — Design Decisions

Resolved design decisions for the AgentShore desktop app (version 0.2.1).
`ONBOARDING_STARTUP_MOCKUPS.html` remains a visual reference for the setup flow;
this Markdown file is authoritative when they disagree.

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

### 2.3 Sidecar crash recovery — surface, do not auto-restart

When the **sidecar process** dies, the Rust supervisor emits a crash event and
the WebView routes to a recovery screen (logs, open log file, restart sidecar,
kill agent subprocesses, quit). Silent auto-restart masks crash loops; a loud
failure mode forces real bugs to surface and preserves logs.

This is distinct from a **renderer (WebView) crash** — where the WKWebView
paint or compositor fails and the window turns white while the sidecar and
engine continue running unaffected. See §2.4.

### 2.4 Renderer crash recovery (#274)

**Symptom.** The WKWebView renderer intermittently produces a white screen
during long sessions. Session `3771f874` confirmed the engine was unaffected:
the sidecar kept running, ESR HTML was generated (1.28 MB on disk),
`$/esr_ready` fired, and the dashboard rendered in a browser at
`http://localhost:9411` — while the Tauri window was white. JS continued
running (`useEffect` fired). This is a renderer/compositor paint failure, not
a backend failure.

**Principle: rebuild the renderer, not the engine.** The sidecar is never
killed by crash recovery. Window-close / ⌘Q still terminates the session
(unchanged). In-place recovery only — detached sidecar (surviving full app
quit) is explicitly deferred (§9).

**Mechanism.**

1. *Detection:* two complementary signals — `on_web_content_process_terminate`
   (native WKWebView hook; fires when the renderer process dies) and an
   rAF-gated JS heartbeat watchdog (covers the JS-alive paint wedge where the
   content process is alive but `requestAnimationFrame` stalls). Either signal,
   plus the manual "Reload UI" menu item (§5.1), calls `reload_ui()`.
2. *Reload:* `WebviewWindow::reload()` remounts the React app with no sidecar
   teardown. A reload never emits `session.stop` — `CloseRequested` /
   `ExitRequested` / `Exit` are the only teardown triggers; `reload()` reaches
   none of them. "Reload UI" is handled inline in Rust so it works while the
   WebView is white.
3. *Reattach:* on mount, `App.tsx` calls the `current_session()` Tauri command,
   which returns `{ active: bool, dashboardUrl: string|null, sessionId:
   string|null }` from `ActivityHolder::is_active()` + `SessionInfoHolder`.
   When `active && dashboardUrl`, the router navigates directly to
   `/session/dashboard` (bypassing the project picker), and the WebSocket
   reconnects to the live bridge endpoint.
4. *Bridge full-state replay:* `DashboardBridge._replay_to_ws()` sends a full
   state snapshot to the fresh WebSocket client; the dashboard re-populates
   without a new `session.start`.

**Startup precedence:** fatal handshake error → sidecar-crash screen →
session reattach → project picker. The reattach path activates when
`current_session()` returns `active: true` at startup (§10), before
`ChooseProjectScreen` renders — a no-flash splash gate prevents the picker
from flickering while the reattach probe resolves.

**Heartbeat caveat.** rAF gating means the beat stops during a compositor
stall (the only JS-observable signal of the paint wedge), but if JS *and* rAF
remain live the watchdog sees no missed beats and the terminate hook does not
fire. The "Reload UI" menu item (`CmdOrCtrl+R`) is the guaranteed recovery
floor. Both signals are needed for complete coverage.

**Watchdog stands down at `$/esr_ready`.** The watchdog also disarms as soon
as the engine emits `$/esr_ready` — not just at the later `session.completed`
— because the gap between the two can be tens of seconds of pure backend
bookkeeping (e.g. timelapse render finalization, up to 60s) with no live
dashboard left to protect. Without this, a normal session end could trip a
false "Dashboard not responding" dialog. See `webview-crash-recovery.md` §4a.

For non-obvious design decisions and rejected alternatives, see
`docs/design/webview-crash-recovery.md`. See also: Dashboard DESIGN
§Reconnection and §Session discovery (`docs/design/dashboard/DESIGN.md`);
IPC DESIGN §Command-In Transport (`docs/design/ipc/DESIGN.md`).

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
set-target-branch / deselect), identities, agents (list / configure /
`agents.check_auth`), config, session (start / status / stop), and archive (list
/ fetch report / fetch logs). Sidecar-to-shell notifications cover progress,
session completion, sidecar health, and agent subprocess spawn/exit;
shell-to-sidecar covers cancellation.

`agents.check_auth` probes each configured CLI agent's **backend** auth — the
model-provider session the agent harness uses (e.g. the Codex CLI's cached
`chatgpt.com` token), which carries a TTL and is independent of the GitHub
identity token `identities.check_access` validates. With no `agent_type` it
probes every enabled CLI agent; with `{"agent_type": "codex"}` it probes one.
It never raises — config-load and per-probe failures come back as
error-status rows so the setup screen always renders. The agents/identities
setup screen calls it to draw a per-agent backend-auth badge backed by the same
probe the `check_agent_auth` launch phase runs, so a green badge provably means
the launch gate will pass.

In-session commands (pause, resume, drain, feedback, abort/override play, budget
adjust, verification response, report generation) stay on the existing WebSocket
channel and are out of scope for the JSON-RPC control plane.

### 5.1 Native menu bar

`build_app_menu` (`desktop/src-tauri/src/lib.rs`) builds the standard-app menu
bar. Custom items emit a `menu:<id>` Tauri event; the session-scoped items
(Stop Session, Adjust Budget) are handled by `SessionDashboardScreen`, and the
app-global items by the `AppMenu` controller (`desktop/src/components/AppMenu.tsx`,
mounted in `App.tsx` outside the route table so it works on every screen). Items
stay enabled — React decides what to do for the current state rather than keeping
enabled-state synced over IPC.

- **File** — Adjust Budget…, Stop Session, Close Window.
- **Edit / View / Window** — predefined items (undo/redo/clipboard; fullscreen;
  minimize/maximize). The **View** submenu adds one custom item: **Reload UI**
  (`CmdOrCtrl+R`) — calls `reload_main_webview` inline in Rust so it works
  while the WebView is white (§2.4).
- **Help** — Documentation / Release Notes / Report an Issue (opened in the
  browser via the OS opener, inline in Rust), a Keyboard Shortcuts cheat-sheet,
  Open Log Folder (the `open_log_folder` command reveals
  `<project>/.agentshore/logs`, falling back to `~/.config/swink/agentshore`),
  and Copy Diagnostics (Rust assembles `{app, version, os, arch}`; React renders
  a copyable dialog).
- **Preferences…** (Cmd+,) — placeholder dialog today; scope is being researched
  separately (see `docs/design/preferences-menu-decisions.md`). UI-shell prefs
  belong in `tauri-plugin-store`/`ui-state.json`, never in `agentshore.yaml`.
- **Check for Updates…** — manual check plus a silent check on launch that
  prompts only when an update exists; install reuses the `restart_sidecar`
  command to relaunch.

Placement follows platform convention: on macOS the leading App menu is built
explicitly (About / Check for Updates / Preferences / Services / Hide / Quit) so
those two items land there; on Windows/Linux Preferences sits in File and Check
for Updates in Help. Decisions captured in `docs/design/desktop-menu-bar-decisions.md`.

---

## 6. Packaging and distribution

The single macOS build entry point is `uv run python -m scripts.buildkit macos`
(the cross-platform build spine in `scripts/buildkit/`, run from the repo root).
With no flags it builds, signs, verifies, and reveals the installer; flags exist
to skip phases, install, or notarize. Output artifacts: a signed `.app`, a
`.dmg`, and a distribution `.pkg`.

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
desktop's Python always matches the build the shell shipped from. Install-time
provisioning surfaces failures early and lets AgentShore updates ship as a new
wheel. PyTorch dominates installed size; trimming the venv to sidecar-relevant
deps is a follow-up.

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
Tauri 2's built-in updater pointed at `latest.json` in GitHub Releases, surfaced
through a Check for Updates… menu item and a silent check on launch (§5.1).

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
- A real Preferences/Settings window (a placeholder ships today — §5.1), a
  system tray, and an enriched View/Window menu (the native menu bar and a
  keyboard-shortcuts cheat-sheet now ship — §5.1).
- Localization (English only).
- Windows / Linux packaging (macOS only today).
- `agentshore.yaml` schema changes (desktop writes the schema the CLI emits).

---

## 10. Onboarding and startup UX flow

- **Re-entry.** Returning users step through all setup screens every session;
  choices pre-populate from `agentshore.yaml` for confirmation. Post-ESR returns
  to the first screen.
- **Lazy preparation.** All bringup work runs only when the user clicks Start;
  setup screens are declarative. `session.start` runs seven canonical phases,
  each emitting `running`/`ok`/`failed` `$/progress` events that the Screen 8
  checklist mirrors: `config_merge` → `check_agent_auth` → `install_skills` →
  `init_beads` → `bind_ipc` → `start_bridge` → `first_snapshot`. The first
  failing phase short-circuits the runner.
- **Backend-auth launch gate.** `check_agent_auth` runs right after
  `config_merge` (so the merged config is in hand) and probes each configured
  CLI agent's backend session via the shared `auth_probe` core. A definitively
  expired session (e.g. the Codex CLI's cached `chatgpt.com` token) fails the
  phase with a `codex login` remediation message, short-circuiting bringup
  before anything expensive boots; transient probe failures are logged and
  tolerated. This is the same check the setup screen surfaces via
  `agents.check_auth`, and it is independent of the GitHub-identity preflight.
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
- **Session reattach.** When the app launches or the WebView reloads and
  `current_session()` returns `active: true`, the shell navigates directly to
  `/session/dashboard` — skipping the setup rail entirely. The bridge replays a
  full state snapshot to the reconnecting WebSocket client; no new
  `session.start` is issued. This is the startup path taken after a renderer
  crash recovery (§2.4).
