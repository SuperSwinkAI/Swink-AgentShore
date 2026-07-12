# WebView Crash Recovery — Cross-Layer Design Record

Status: **Implemented** (branch `fix/274-webview-crash-recovery`) · Owner: desktop ·
Related: #274 (white-screen renderer failure), `docs/design/desktop/DESIGN.md §2.4`.

> This record captures the non-obvious cross-layer design decisions and the
> alternatives that were considered and rejected. For the operational summary
> (symptom, mechanism, startup precedence) see `desktop/DESIGN.md §2.4`.

## Problem

On macOS, the Tauri desktop app's WKWebView renderer intermittently enters a
white-screen state during long sessions. This looks catastrophic to the user —
but the underlying AgentShore engine is unaffected. Session `3771f874` confirmed
the sidecar continued running, the dashboard bridge was serving live state at
`http://localhost:9411`, and JS inside the frozen WebView was still executing.
The only recovery path available before this fix was closing the app, which
**kills the session** via `CloseRequested` teardown.

The architecture already supports in-place recovery: the engine survives zero
WebSocket clients and `DashboardBridge._replay_to_ws()` (~line 537 in
`src/agentshore/dashboard/bridge.py`) replays a full state snapshot to any new
client. The missing piece was that a WebView reload reset the React app to the
project picker instead of reattaching to the live session.

## Why (the gaps before this fix)

| # | Gap |
|---|-----|
| 1 | No `on_web_content_process_terminate` handler registered — wry's default auto-reload fired silently, React remounted to the picker, and the session appeared lost. |
| 2 | No `current_session()` Tauri command — the React app had no way to ask Rust "is a session running and what is its URL?" on mount. |
| 3 | No `SessionInfoHolder` — the Rust side had no cached `dashboardUrl`/`sessionId` to hand back to a freshly remounted frontend. |
| 4 | No "Reload UI" menu item — users had no in-app manual recovery trigger that worked while the WebView was white. |
| 5 | JS-alive paint wedge undetectable — a compositor stall where JS and the content process remain alive produces no native crash event; no heartbeat mechanism existed to observe it. |

## Design decisions

### 1. No engine auto-restart — the engine survives

The sidecar process is **never** killed or restarted by crash recovery. The
entire fix is on the renderer side. This is both an invariant (killing the
sidecar would lose all session state) and an enabling insight (the bridge replay
path already handles fresh clients without restarting the engine).

### 2. `on_web_content_process_terminate` hook instead of objc2 swizzling

Tauri 2.11.2 exposes a public `Builder::on_web_content_process_terminate`
handler (backed by wry's `webViewWebContentProcessDidTerminate:`). Registering
it gives us logging, reattach signaling, and the reload trigger — without
touching the Objective-C runtime.

**Rejected:** using `objc2` / `with_webview` to swizzle
`webViewWebContentProcessDidTerminate:` on the WKNavigationDelegate. This would
clobber wry's own navigation delegate, breaking link opens, CSP handling, and
any other navigation callbacks wry registers. The public Tauri API is the
right abstraction boundary.

Note: registering our own handler *replaces* wry's default auto-reload. We must
therefore call `WebviewWindow::reload()` ourselves inside the handler — the
behavior is identical but we now log it and trigger the reattach path.

### 3. No `beforeunload` / `unload` handler

A WebView reload is intentionally just a bridge disconnect. There is nothing to
clean up on the JS side — React's component tree is destroyed by the reload and
rebuilt from scratch. Adding a `beforeunload` handler would risk:
- racing with the reload, producing spurious teardown events,
- accidentally emitting `session.stop` if the handler fires on a genuine app
  quit as well.

The teardown invariant is hard: `session.stop` (and all lifecycle teardown) is
reachable only via `CloseRequested` / `ExitRequested` / `Exit` in the Tauri
event loop. `WebviewWindow::reload()` triggers none of these. This was audited
across `desktop/src-tauri/src/lib.rs` before the fix landed.

### 4. Heartbeat alone is insufficient

The rAF-gated JS heartbeat (`invoke("ui_heartbeat")` fired every 2 s, gated on
`requestAnimationFrame`) is necessary but not sufficient:

- **What it covers:** the JS-alive paint wedge — compositor stuck, content
  process alive, rAF stalls, so beats stop. This is undetectable by any native
  hook.
- **What it misses:** if the compositor is stuck but rAF keeps firing (possible
  in some wedge scenarios), the watchdog sees no missed beats and the terminate
  hook does not fire.

The "Reload UI" menu item (`CmdOrCtrl+R`, handled inline in Rust) is therefore
the **guaranteed recovery floor** for all scenarios, including those neither
detector catches. Phase 1 ships this floor; the watchdog is Phase 2 hardening.

### 4a. Watchdog stands down at `$/esr_ready`, not `session.completed` (Phase 2 fix)

The watchdog originally disarmed only on the `session.completed` notification.
But `session.completed` is not the first "session is basically over" signal —
tracing the actual shutdown sequence:

1. `stop_inner` generates the ESR HTML and fires `$/esr_ready`
   (`src/agentshore/core/mixins/drain.py`). The desktop already reacts to this
   by navigating to `/session/esr` (`App.tsx`).
2. Only *after* that does the natural-exit supervisor run
   `stop_timelapse_capture` (`src/agentshore/sidecar/session_lifecycle.py`),
   which polls `await_output` for up to `max_polls=60` at
   `poll_interval_seconds=1.0` — **up to 60 s** waiting for timelapse MP4
   render finalization (`src/agentshore/timelapse/__init__.py`).
3. Only after *that* does `session.completed` fire, which is what previously
   disarmed the watchdog.

On any session with timelapse capture enabled where render finalization
exceeds 10 s, this produced a false "Dashboard not responding" trip for a
session that had already finished successfully — the ESR screen was showing
and the window was fully responsive the entire time.

**Fix:** `WebviewHeartbeat` gained an `esr_ready: AtomicBool`, set true when
`dispatch_line` (`sidecar.rs`) observes `$/esr_ready` (and, defensively, also
on `session.completed`, covering shutdown paths that skip the ESR-ready
callback). The watchdog's trip predicate — extracted as the pure,
unit-tested `should_declare_wedge()` — requires `!esr_ready` in addition to
`enabled && active`. The flag resets to `false` on the next `session.start`
so a subsequent session is fully re-armed. No JS changes were needed; both
notifications were already parsed by the frontend.

### 5. "Reload UI" menu item handled inline in Rust

The menu item must work while the WebView is white — meaning the React app
(`AppMenu.tsx`) is not running. Standard AgentShore menu items emit a
`menu:<id>` Tauri event and rely on React's `AppMenu` controller to handle
them. That path is unavailable during a white-screen.

"Reload UI" is therefore handled in `on_menu_event` in Rust directly:
`"reload_ui" => reload_main_webview(app)`. No JS event listener is involved.

**Rejected:** routing it through `AppMenu.tsx` like other items. That would
make the most-needed recovery trigger unreachable in exactly the failure case
it exists for.

### 6. Reattach precedence

On mount (and after reload), `App.tsx` runs four probes in priority order:

1. **Fatal handshake error** — surfaces the fatal-error screen; no reattach.
2. **Sidecar-crash** — surfaces the crash-recovery screen (§2.3); no reattach.
3. **Session reattach** — `current_session()` returns `active: true`; navigate
   to `/session/dashboard`, skip the picker.
4. **Project picker** — default; no active session.

**Why reattach comes after crash:** a sidecar crash with `active: true` still
in `ActivityHolder` (a brief race window at crash time) must not silently try
to reconnect to a dead sidecar. The crash signal is authoritative; reattach
only runs when the sidecar is confirmed healthy.

### 7. No-flash splash gate

While the reattach probe is in-flight, the `/` route renders the immersive
splash (`desktop-shell--immersive`) rather than `ChooseProjectScreen`. This
prevents a visible picker flash before the router redirects to the dashboard.

**Rejected:** rendering the picker optimistically and replacing it on success.
This produces a visible layout jump (picker → dashboard) and could briefly
expose the picker's "start session" affordances while a session is already
running.

### 8. `current_session()` shape

The Tauri command returns `{ active: bool, dashboardUrl: string|null, sessionId:
string|null }`. `active` comes from `ActivityHolder::is_active()` (the
established alive signal); `dashboardUrl` and `sessionId` come from a new
`SessionInfoHolder` populated at `session.start` RPC success and cleared at
`session.stop` / `session.completed`.

**Important guard:** `SessionInfoHolder` is populated only when the
`session.start` RPC response contains no embedded `"error"` key. In the
sidecar JSON-RPC path, RPC-level errors come back as `Ok(json!({"error":…}))`;
the Rust `jsonrpc_call` function would otherwise see an `Ok` result and
spuriously cache a `dashboardUrl` for a session that never started.

### 9. Deferred: detached sidecar (survive full app quit)

Allowing the sidecar to survive ⌘Q and reattach after the app relaunches was
considered and explicitly deferred. Concerns:

- An orphaned sidecar with no supervising Tauri process accumulates agent
  dispatches and API spend with no human visibility into the window.
- The sidecar today terminates itself when its stdio pipe closes (Tauri quit).
  Detaching requires active keepalive, a new discovery protocol for a restarted
  app, and a session-ownership handoff model — meaningfully higher risk.
- Phase 1 already removes the main user-visible harm (#274): the session is now
  recoverable from a renderer crash without closing the app.

## Cross-layer file map

| Layer | Key files |
|-------|-----------|
| Rust | `desktop/src-tauri/src/lib.rs` (`reload_ui`, `current_session`, `ui_heartbeat`, `SessionInfoHolder`, `WebviewHeartbeat` incl. `esr_ready`, `should_declare_wedge`, `on_web_content_process_terminate`, "Reload UI" menu item) |
| Rust | `desktop/src-tauri/src/sidecar.rs` (`SessionInfoHolder` populate/clear + `WebviewHeartbeat.esr_ready` set in `dispatch_line` on `$/esr_ready` / `session.completed`) |
| Frontend | `desktop/src/rpc/sessionClient.ts` (`CurrentSessionInfo`, `currentSession()`) |
| Frontend | `desktop/src/services/sessionContext.tsx` (`sessionReattaching` state) |
| Frontend | `desktop/src/App.tsx` (reattach effect, no-flash gate, heartbeat effect) |
| Python | `src/agentshore/sidecar/session_lifecycle.py` (ESR local-snapshot fix — fold-in from #274 scope) |
| Python | `src/agentshore/dashboard/bridge.py` (`_replay_to_ws`, ~line 537) |

## Related design records

- `docs/design/desktop/DESIGN.md §2.3` — sidecar crash recovery (the distinct case)
- `docs/design/desktop/DESIGN.md §2.4` — operational summary (symptom + mechanism)
- `docs/design/desktop/DESIGN.md §5.1` — menu bar inventory ("Reload UI" item)
- `docs/design/desktop/DESIGN.md §10` — startup UX flow (session reattach path)
- `docs/design/dashboard/DESIGN.md` — bridge reconnection and session discovery
- `docs/design/ipc/DESIGN.md` — IPC command-in transport (socket path, `discover_ipc_endpoint`)
