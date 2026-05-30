# AgentShore Desktop — Design Decisions

Living companion to [LAYER_CAKE.html](LAYER_CAKE.html) (architectural overview)
and [ONBOARDING_STARTUP_MOCKUPS.html](ONBOARDING_STARTUP_MOCKUPS.html) (UI
flow). This document records the resolved design decisions for the AgentShore
desktop app.

When this document and the HTML companions disagree, this document wins.

## Scope

AgentShore Desktop is a Tauri 2 native shell that supervises a AgentShore session
end-to-end: project selection, readiness inspection, GitHub identity and
agent configuration, session lifecycle, dashboard monitoring, and the End
Session Report.

It is a lifecycle control plane, not just a packaged dashboard. The desktop
talks to a versioned machine protocol; no CLI output is ever parsed.

---

## 1. Process and lifecycle

### 1.1 Sidecar model — hybrid: internal Python APIs, RPC-independent contract

**Decision.** The Python sidecar implements each RPC method using internal
AgentShore APIs. The JSON-RPC contract is designed independently so individual
methods can swap call paths over time.

**Rationale.** Importing APIs directly gives real exception types and structured errors; a purpose-built protocol layer decouples Python internals from the desktop.

### 1.2 Process topology — single Python process

**Decision.** One Python process. The sidecar RPC server, the Orchestrator,
the dashboard WebSocket bridge, and AgentShore's existing IPC server all live
in the same asyncio loop as cooperative tasks.

**Rationale.** AgentShore's core loop is already pure asyncio; splitting into a child process duplicates supervision Tauri already provides and adds an internal IPC hop for no benefit.

### 1.3 Sidecar lifecycle — one per app lifetime, project context switches

**Decision.** A single sidecar starts when Tauri launches and lives until
Tauri quits. `project.select(path)` switches the active project context.

**Rationale.** Per-project respawn would add ~1s cold start to every recents click; reusing the idle sidecar is free.

### 1.4 Crash recovery — surface, do not auto-restart

**Decision.** When the sidecar dies, the Rust supervisor emits a
`sidecar.crashed` event. The WebView routes to a recovery screen showing
logs, "Open log file", "Restart sidecar", "Kill all" (agent subprocesses),
and "Quit app."

**Rationale.** Silent auto-restart masks crash loops; a loud failure mode forces real bugs to surface and preserves logs across crashes.

---

## 2. Protocols

### 2.1 Channel split — lifecycle vs in-session

**Decision.** JSON-RPC over stdio owns out-of-session lifecycle commands.
AgentShore's existing WebSocket IPC channel owns in-session commands unchanged.

**Rationale.** Clean split along orchestrator-not-running vs orchestrator-running; zero churn to the dashboard's existing command path.

### 2.2 Control plane — JSON-RPC 2.0 over stdio

**Decision.** Tauri Rust supervisor and Python sidecar talk JSON-RPC 2.0
framed line-by-line over the sidecar's stdin/stdout.

**Rationale.** Tauri's sidecar API natively pipes stdio so process lifecycle == stdio lifecycle; no port allocation, firewall popups, or socket cleanup races.

### 2.3 State plane — existing WebSocket bridge embedded

**Decision.** `DashboardBridge` runs as another asyncio task inside the same
sidecar process. The Tauri WebView connects via `ws://127.0.0.1:<port>/ws`.

**Rationale.** Same code path serves CLI `agentshore dashboard` and the desktop's dashboard tab, so they cannot drift.

### 2.4 Progress — LSP-style `$/progress` notifications

**Decision.** Long-running lifecycle calls accept a `progress_token` in
`params`. The sidecar emits `$/progress` notifications with step/percent/message.
The original request stays open until work completes.

**Rationale.** Idiomatic JSON-RPC; one method per logical operation; maps cleanly to a React `useEffect` listener.

### 2.5 Cancellation — LSP-style `$/cancelRequest`

**Decision.** The shell cancels in-flight calls with `$/cancelRequest`.
The sidecar cooperatively cancels the asyncio task and resolves the original
request with error code `-32800 RequestCancelled`.

**Rationale.** Matches the `$/progress` pattern and avoids SIGKILL'ing the sidecar mid-write (SQLite corruption risk).

### 2.6 Handshake — bundled-together, `build_id` verification

**Decision.** Shell and sidecar both embed a `build_id`. First call is
`app.handshake`; response includes `protocol_version`, `agentshore_version`,
`sidecar_build_id`, and `capabilities`. On `build_id` mismatch, the shell
shows a fatal error and refuses to proceed.

**Rationale.** Bundled installs guarantee shell and sidecar ship together; a mismatch means corrupt install or a foreign sidecar — both fatal.

---

## 3. UI architecture

### 3.1 Shell stack — React + Vite + React Router

**Decision.** The desktop shell is a Vite + React + React Router app in
`desktop/src/`. The Tauri WebView origin is `tauri://localhost/`.

**Rationale.** React has the largest ecosystem for form-heavy shell screens and the best AI codegen coverage.

### 3.2 Dashboard rewrite — port to React, fix sync at the protocol layer

**Decision.** Rewrite the dashboard to React components, replacing the
vanilla TS entry. Fix protocol-level sync misalignments during the rewrite
(event ordering, sequence numbers, authoritative snapshots).

**Rationale.** One consistent frontend stack across the desktop; sync issues are protocol-level and must be fixed regardless of framework.

### 3.3 Asset hosting — Tauri-bundled, build-time copy from Python static dir

**Decision.** Vite builds the dashboard into the Python package's static
directory (CLI consumption). Tauri's build step copies those files into Tauri
resources. Only the WebSocket goes to the Python sidecar.

**Rationale.** Idiomatic Tauri (CSP, asset protocol, offline rendering during brief sidecar hiccups, faster cold load).

### 3.4 Shared dashboard — `@agentshore/dashboard` workspace package

**Decision.** `dashboard/` becomes an npm workspace package
`@agentshore/dashboard`, exporting React components. The desktop shell imports
them; the CLI `agentshore dashboard` builds the same package standalone.

**Rationale.** Both surfaces consume one source of truth; protocol sync fixes benefit both automatically.

---

## 4. State and persistence

### 4.1 Setup state — write-through to `agentshore.yaml`

**Decision.** Every identity or agent edit immediately rewrites
`agentshore.yaml`. There is no in-memory draft. `session.start` boots the
orchestrator against the on-disk config.

**Rationale.** Matches existing `agentshore init --force` semantics; no draft-lifecycle bookkeeping in the sidecar.

### 4.2 App-level state — AgentShore state in sidecar, UI state in Tauri

**Decision.** AgentShore-domain state (recents list) lives in the sidecar at
`platformdirs.user_data_dir('agentshore')/recents.json`. UI-only state
(window position, theme, last selected tab) lives in `tauri-plugin-store`.

**Rationale.** Clean ownership: Python knows about projects, Rust knows about the window; recents can be reused by future frontends.

---

## 5. API surface

### 5.1 V1 method list

**Lifecycle** — `app.handshake`

**Recents** — `recents.list`, `recents.touch`, `recents.remove`

**Project** — `project.select`, `project.inspect`, `project.branches`, `project.set_target_branch`, `project.deselect`

**Identities** — `identities.list`, `identities.add`, `identities.update`, `identities.remove`

**Agents** — `agents.list`, `agents.configure`

**Config** — `config.read`, `config.write`

**Session** — `session.start`, `session.status`, `session.stop`

**Archive** — `archive.list`, `archive.fetch_report`, `archive.fetch_logs`

**Notifications (sidecar to shell)** — `$/progress`, `session.completed`, `sidecar.health`, `agent.subprocess_spawned`, `agent.subprocess_exited`

**Notifications (shell to sidecar)** — `$/cancelRequest`

**Out of scope for v1.** In-session commands (pause, resume, drain,
feedback_response, abort_play, adjust_budget, override_play,
verification_response, generate_report). These stay on the
existing WebSocket channel.

---

## 6. Packaging and distribution

### 6.1 Tauri 2

**Decision.** Tauri 2.x for the native shell.

**Rationale.** Current stable major with v2-compatible plugins and the new permissions/capabilities model.

### 6.2 Python sidecar — pkg installer + managed venv

**Decision.** Ship the Python sidecar as a pip-installable wheel. A macOS
`.pkg` installer provisions a managed venv on the user's machine. The `.app`
contains only the Rust supervisor + JS shell; the sidecar lives outside the
bundle. Dev mode falls back to `uv run python -m agentshore.sidecar`.

**Rationale.** A prior PyInstaller frozen-bundle design hit Tauri `externalBin` integration issues; shifting to install-time provisioning keeps the installer small (~50MB), surfaces failures early, and lets agentshore updates ship as a new wheel without re-downloading PyTorch.

### 6.3 Dependency footprint

**Decision.** PyTorch (~700MB CPU wheel) dominates installed size and is
shared across releases — only the agentshore wheel ships in the `.pkg`.
Trimming the venv to sidecar-relevant deps is a follow-up optimization.

### 6.4 Beads CLI — bundled

**Decision.** Ship `bd` as a Tauri-managed sidecar binary. AgentShore resolves
via `AGENTSHORE_BD_BIN` env var (set by Rust supervisor), falling back to PATH
for CLI mode.

**Rationale.** beads is non-optional infrastructure; bundling removes the most common first-run failure (~5-10MB per platform).

### 6.5 Code signing and auto-update

**Decision.** Tauri 2's built-in updater pointed at `latest.json` in GitHub
Releases. macOS code-signed and notarized via Apple Developer ID; Windows
signed with an OV or EV cert.

**Rationale.** First-launch UX requires signing on both platforms; auto-update keeps installations current.

See `docs/release/signing.md` for the maintainer procedure.

---

## 7. Repo layout

```
AgentShore/
├── src/agentshore/                 # Python core
├── dashboard/                   # @agentshore/dashboard workspace package
├── desktop/
│   ├── src-tauri/               # Rust supervisor
│   ├── src/                     # React shell (Vite + React Router)
│   └── package.json             # depends on @agentshore/dashboard
└── docs/design/desktop/         # design docs
```

---

## 8. Non-functional baseline

| Dimension       | Target                                                              |
|-----------------|---------------------------------------------------------------------|
| Cold start      | App interactive < 3s on macOS 14 / Win 11 on NVMe SSD               |
| Handshake       | Completes < 1s after sidecar spawn                                  |
| Idle memory     | Sidecar < 250MB                                                     |
| Active memory   | Sidecar + tracked agent subprocesses < 1.5GB                        |
| OS support      | macOS 13+ (universal binary), Windows 11 primary + Win 10 best-effort |
| Offline         | `project.inspect`, recents, identity edits work offline             |
| Accessibility   | Full keyboard navigation in v1; full screen-reader support deferred |

---

## 9. Deferred / out of scope for v1

- **Telemetry and remote crash reporting.** Only the local crash screen for
  now. No outbound reporting unless explicitly added.
- **Multi-window.** Single window. Use recents to switch projects, not
  multiple windows.
- **Native menus and keyboard shortcuts.** Platform-conventional menu bars
  with file/edit/help groupings; specific shortcuts not enumerated.
- **Tauri capabilities ACL.** Minimum required (sidecar IPC, dialog,
  `fs:read` for project paths, `shell:open` for log files); enumerated in
  `tauri.conf.json` during implementation.
- **Localization.** English only.
- **Legacy `mount(root, opts)` adapter.** Removed once the React dashboard
  rewrite (§3.2) lands and CLI consumers migrate.
- **`agentshore.yaml` schema changes.** None anticipated; desktop writes the
  same schema the CLI emits.
- **Sandbox / hardened runtime.** macOS hardened runtime entitlements TBD
  during signing setup; Tauri sandbox opt-in deferred.

---

## 10. Onboarding and startup UX flow

### 10.1 Re-entry — always step through all setup screens

**Decision.** Returning users always step through all five setup screens on
every session. Choices pre-populate from `agentshore.yaml` for confirmation.
Post-ESR, the user returns to screen 1 and steps through the full flow.

### 10.2 Preparation — lazy, on Start click

**Decision.** Config merge, skill install/update, GitHub login binding, and
beads initialisation run when the user clicks "Start session" on screen 7.
Setup screens 2-6 are declarative only.

### 10.3 Rail navigation — freely navigable, hidden during session and ESR

**Decision.** The left rail is a freely navigable menu; users can jump to
any setup screen in any order. The Start button is the single completeness
gate. The rail is hidden during the dashboard and ESR surfaces.

### 10.4 Start button gate — minimum viable configuration

**Decision.** Start is enabled when: a target branch is selected, at least
two agent runners are enabled, and at least two GitHub identities are
assigned. Everything else has working defaults.

### 10.5 Seed file — dependent field on screen 7

**Decision.** The seed file input appears on screen 7 as a dependent field,
shown when the user selects "Seed project" as the startup action.

### 10.6 Readiness — hard blocks for safety only

**Decision.** Screen 2 hard-blocks Continue only for safety-critical
conditions: the repo is the AgentShore source directory, or no Git repository
exists. All other items are informational.

### 10.7 Identity sharing — anti-bias enforced at play time

**Decision.** Multiple runners may share the same GitHub login. The Code
Review anti-bias invariant is enforced by the play engine at runtime, not
by the setup UI.

### 10.8 Startup failure — inline repair on screen 8

**Decision.** When a startup step fails on screen 8, the failing step turns
red inline, shows the error, and offers a contextual repair action (Retry,
or a link to the relevant setup screen).
