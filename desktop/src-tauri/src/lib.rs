use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::Path;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
#[cfg(not(test))]
use tauri::menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem, SubmenuBuilder};
use tauri::{AppHandle, Manager, WindowEvent};
#[cfg(not(test))]
use tauri::{Emitter, Runtime};
use tauri_plugin_store::StoreExt;

pub mod activity;
pub mod install_layout;
pub mod jsonrpc_stdio;
pub mod readiness;
pub mod sidecar;
mod sidecar_env;
mod sidecar_pid;
mod sidecar_runtime;

const UI_STATE_STORE_PATH: &str = "ui-state.json";
const UI_STATE_KEY: &str = "ui_state";

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
struct WindowState {
    x: i32,
    y: i32,
    width: u32,
    height: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct UiState {
    theme: String,
    last_selected_tab: String,
    window: Option<WindowState>,
    // `#[serde(default)]` is load-bearing: a pre-existing `ui-state.json` lacks
    // this field, and without the default the whole struct would fail to
    // deserialize and silently reset theme/tab/window. `false` == carousel unseen.
    #[serde(default)]
    onboarding_completed: bool,
}

impl Default for UiState {
    fn default() -> Self {
        Self {
            theme: "system".to_string(),
            last_selected_tab: "home".to_string(),
            window: None,
            onboarding_completed: false,
        }
    }
}

#[derive(Default)]
struct UiStateHolder {
    state: Mutex<UiState>,
}

/// One-shot latch: set once the user has approved a quit while a session was
/// running, so the re-entrant close/exit that follows the confirmation (window
/// `destroy()` → `ExitRequested`, or `app.exit()`) proceeds without re-prompting.
/// Also set by the explicit in-app Quit buttons (recovery / fatal-error
/// screens), which are themselves a deliberate quit and shouldn't double-prompt.
#[derive(Default)]
struct QuitConfirmed(AtomicBool);

impl QuitConfirmed {
    fn get(&self) -> bool {
        self.0.load(Ordering::SeqCst)
    }

    fn set(&self) {
        self.0.store(true, Ordering::SeqCst);
    }
}

/// Whether quitting now needs the running-session confirmation prompt: a
/// session is live and the user hasn't already approved this quit. Pure so the
/// gate logic is unit-testable without a running Tauri app.
fn quit_requires_confirmation(session_active: bool, already_confirmed: bool) -> bool {
    session_active && !already_confirmed
}

/// Whether the heartbeat watchdog should declare a paint wedge right now. Pure
/// so the trip logic is unit-testable without a running watchdog thread.
///
/// `shutdown_in_progress` (true once either `session.draining` or
/// `$/esr_ready`/`session.completed` has been observed) suppresses the trip
/// even while `active` is still true — the whole window from drain-start
/// onward is backend bookkeeping (ESR HTML generation, timelapse render
/// finalization, etc.) with no live dashboard left to protect, and a real,
/// busy shutdown can legitimately pause the render loop for many seconds
/// during it.
fn should_declare_wedge(
    enabled: bool,
    active: bool,
    shutdown_in_progress: bool,
    last_beat_ms: i64,
    now_ms: i64,
    threshold_ms: i64,
) -> bool {
    enabled
        && active
        && !shutdown_in_progress
        && last_beat_ms != 0
        && now_ms.saturating_sub(last_beat_ms) > threshold_ms
}

/// Whether the debounced watchdog trip is confirmed: the stale condition has
/// held for at least `confirm_threshold` consecutive polls. Pure so the
/// debounce boundary is unit-testable without the watchdog thread.
fn wedge_confirmed(consecutive_stale_polls: u32, confirm_threshold: u32) -> bool {
    consecutive_stale_polls >= confirm_threshold
}

/// Cached session info populated on a successful `session.start` RPC, cleared
/// on `session.stop` and `session.completed`. Drives `current_session()` and
/// the reattach path after a WebView reload.
#[derive(Debug, Clone)]
struct SessionInfo {
    dashboard_url: String,
    session_id: String,
}

#[derive(Default)]
struct SessionInfoHolder(Mutex<Option<SessionInfo>>);

/// Serialized response for the `current_session` Tauri command. camelCase
/// mirrors the frontend's `CurrentSessionInfo` type in `rpc/sessionClient.ts`.
#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct CurrentSessionInfo {
    active: bool,
    dashboard_url: Option<String>,
    session_id: Option<String>,
}

/// Phase 2: rAF-gated heartbeat state. `enabled` is set true by the first
/// `ui_heartbeat` call and cleared when the watchdog fires or the session ends.
/// `last_beat_ms` is a UNIX-epoch millisecond timestamp stamped each beat.
/// `esr_ready` is set true once the engine emits `$/esr_ready` (or, defensively,
/// `session.completed`) — from that point there is no more live-dashboard work
/// for the watchdog to protect, even though `ActivityHolder` can stay active for
/// up to another ~60s while backend bookkeeping (e.g. timelapse render
/// finalization) finishes. `draining` is set true once the engine emits
/// `session.draining`, fired at drain start — well before `esr_ready`, which
/// only arrives after (unbounded, O(plays)) ESR HTML generation completes.
/// Both reset false on the next `session.start`.
struct WebviewHeartbeat {
    last_beat_ms: AtomicI64,
    enabled: AtomicBool,
    esr_ready: AtomicBool,
    draining: AtomicBool,
}

impl Default for WebviewHeartbeat {
    fn default() -> Self {
        Self {
            last_beat_ms: AtomicI64::new(0),
            enabled: AtomicBool::new(false),
            esr_ready: AtomicBool::new(false),
            draining: AtomicBool::new(false),
        }
    }
}

/// Re-entrancy guard: prevents the heartbeat watchdog and the
/// content-process-terminate hook from surfacing the wedge dialog
/// simultaneously.
#[cfg_attr(test, allow(dead_code))]
#[derive(Default)]
struct WedgeDialogActive(AtomicBool);

/// Caller context for `declare_webview_wedged` — informs the log message.
#[allow(dead_code)]
enum WebviewWedgeMode {
    Heartbeat,
    ProcessTerminate,
}

struct SidecarHolder {
    // Arc so callers can clone the supervisor OUT of the holder lock and run
    // (possibly hours-long) blocking RPCs without holding the lock — holding
    // it across a wedged ``session.stop`` deadlocked the window-close
    // teardown and left the app running headless (#155).
    supervisor: Arc<sidecar::SidecarSupervisor>,
}

/// Optional supervisor handle. When the supervisor failed to start
/// (handshake mismatch, spawn error, …) ``Option<SidecarHolder>`` is
/// ``None`` and the shell routes to the fatal-error screen instead of
/// dispatching JSON-RPC calls.
type SidecarHolderState = Mutex<Option<SidecarHolder>>;

/// Fatal shell-state populated when the supervisor fails. Read by the
/// React shell via ``get_fatal_shell_state`` on mount; non-empty value
/// triggers the /fatal-error route (DESIGN §2.6).
#[derive(Default)]
struct FatalShellState {
    info: Mutex<Option<sidecar::SupervisorStartError>>,
}

fn read_ui_state(app: &AppHandle) -> UiState {
    let store = match app.store(UI_STATE_STORE_PATH) {
        Ok(store) => store,
        Err(_) => return UiState::default(),
    };

    match store.get(UI_STATE_KEY) {
        Some(value) => serde_json::from_value::<UiState>(value).unwrap_or_default(),
        None => UiState::default(),
    }
}

fn persist_ui_state(app: &AppHandle, state: &UiState) -> Result<(), String> {
    let store = app.store(UI_STATE_STORE_PATH).map_err(|e| e.to_string())?;
    store.set(UI_STATE_KEY, json!(state));
    store.save().map_err(|e| e.to_string())
}

fn with_ui_state<R>(app: &AppHandle, f: impl FnOnce(&mut UiState) -> R) -> Result<R, String> {
    let holder = app.state::<UiStateHolder>();
    let mut guard = holder.state.lock().map_err(|e| e.to_string())?;
    Ok(f(&mut guard))
}

#[cfg_attr(test, allow(dead_code))]
fn capture_window_state(app: &AppHandle) -> Option<WindowState> {
    let window = app.get_webview_window("main")?;
    let position = window.outer_position().ok()?;
    let size = window.outer_size().ok()?;
    Some(WindowState {
        x: position.x,
        y: position.y,
        width: size.width,
        height: size.height,
    })
}

#[cfg_attr(test, allow(dead_code))]
fn update_window_state(app: &AppHandle) {
    let Some(window_state) = capture_window_state(app) else {
        return;
    };

    let updated = with_ui_state(app, |state| {
        state.window = Some(window_state.clone());
        state.clone()
    });
    if let Ok(state) = updated {
        let _ = persist_ui_state(app, &state);
    }
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
fn load_ui_state(app: AppHandle) -> Result<UiState, String> {
    let state = read_ui_state(&app);
    with_ui_state(&app, |current| {
        *current = state.clone();
    })?;
    Ok(state)
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
fn set_ui_theme(app: AppHandle, theme: String) -> Result<UiState, String> {
    let trimmed = theme.trim();
    if trimmed.is_empty() {
        return Err("theme must not be empty".to_string());
    }
    let next = with_ui_state(&app, |state| {
        state.theme = trimmed.to_string();
        state.clone()
    })?;
    persist_ui_state(&app, &next)?;
    Ok(next)
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
fn set_onboarding_completed(app: AppHandle, completed: bool) -> Result<UiState, String> {
    let next = with_ui_state(&app, |state| {
        state.onboarding_completed = completed;
        state.clone()
    })?;
    persist_ui_state(&app, &next)?;
    Ok(next)
}

// Read a UTF-8 text file from disk. v1 trusts the caller-supplied path — the
// sidecar produces the archive paths the desktop hands back here. A future
// hardening pass can restrict this to an archive-root allowlist.
fn read_text_file_impl(path: PathBuf) -> Result<String, String> {
    std::fs::read_to_string(path).map_err(|e| e.to_string())
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
fn read_text_file(path: String) -> Result<String, String> {
    read_text_file_impl(PathBuf::from(path))
}

fn with_supervisor<R>(
    app: &AppHandle,
    f: impl FnOnce(&sidecar::SidecarSupervisor) -> R,
) -> Result<R, String> {
    // Clone the Arc out under a short-lived lock, then run `f` OUTSIDE it. `f` is
    // a blocking RPC that can run minutes (session.start) to hours (session.stop
    // drain); holding the lock that long blockaded shutdown on window close and
    // left the app running headless (#155).
    let supervisor = {
        let state = app.state::<SidecarHolderState>();
        let guard = state.lock().map_err(|e| e.to_string())?;
        match guard.as_ref() {
            Some(holder) => Arc::clone(&holder.supervisor),
            None => {
                return Err("sidecar unavailable (shell is in fatal-error state)".to_string());
            }
        }
    };
    Ok(f(&supervisor))
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
async fn jsonrpc_call(
    app: AppHandle,
    method: String,
    params: Option<Value>,
) -> Result<Value, String> {
    let method_for_hook = method.clone();
    // Run the blocking supervisor call OFF the main thread. Tauri runs sync
    // commands on the UI thread, so a long RPC (session.start bringup) froze the
    // window and stalled $/progress; spawn_blocking keeps the event loop pumping.
    let app_for_call = app.clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        with_supervisor(&app_for_call, |sup| sup.call(method, params))
    })
    .await
    .map_err(|e| format!("sidecar call task failed: {e}"))??;
    // desktop-bzr2: hold an NSProcessInfo activity assertion while a session is
    // alive so App Nap can't throttle the backgrounded UI. Acquire on
    // session.start, release on session.stop; natural exit (session.completed) is
    // released in sidecar's notification handler.
    //
    // Guard: RPC errors arrive wrapped in Ok(json!({"error":...})) — check for
    // the absence of an embedded "error" key rather than result.is_ok(), which
    // is always true at this point (transport errors propagated earlier via `??`).
    if let Ok(ref rpc_value) = result {
        if rpc_value.get("error").is_none() {
            let holder = app.state::<activity::ActivityHolder>();
            match method_for_hook.as_str() {
                "session.start" => {
                    holder.acquire("AgentShore session active");
                    // Re-arm the ESR-ready/draining gates for the new session
                    // (heartbeat watchdog, #274 follow-up) — a prior session's
                    // disarm must not suppress wedge detection for this one.
                    let heartbeat = app.state::<WebviewHeartbeat>();
                    heartbeat.esr_ready.store(false, Ordering::SeqCst);
                    heartbeat.draining.store(false, Ordering::SeqCst);
                    // Cache session info for current_session() + reattach (#274).
                    // dashboardUrl shape: http://{host}:{port}/ (mirrors
                    // StartingProgressRoute.tsx dashboardUrlFromEndpoint ~58-69).
                    let dashboard_url = rpc_value.get("ipc_endpoint").and_then(|ep| {
                        let host = ep.get("host").and_then(Value::as_str)?;
                        let port = ep.get("port").and_then(Value::as_u64)?;
                        Some(format!("http://{}:{}/", host, port))
                    });
                    let session_id = rpc_value
                        .get("session_id")
                        .and_then(Value::as_str)
                        .map(str::to_string);
                    if let (Some(url), Some(id)) = (dashboard_url, session_id) {
                        if let Ok(mut guard) = app.state::<SessionInfoHolder>().0.lock() {
                            *guard = Some(SessionInfo {
                                dashboard_url: url,
                                session_id: id,
                            });
                        }
                    }
                }
                "session.stop" => {
                    holder.release();
                    if let Ok(mut guard) = app.state::<SessionInfoHolder>().0.lock() {
                        *guard = None;
                    }
                }
                _ => {}
            }
        }
    }
    result
}

/// Hand a path or URL to the OS default handler — the browser for an https
/// URL, Finder/Explorer for a folder. Shared by the
/// `open_path_in_default_app` / `open_log_folder` commands and the Help-menu
/// URL items (Documentation / Release Notes / Report an Issue). Spawns the
/// platform opener detached; rejects an empty target.
fn spawn_open(target: &str) -> Result<(), String> {
    if target.trim().is_empty() {
        return Err("target must not be empty".to_string());
    }
    #[cfg(target_os = "macos")]
    let mut cmd = std::process::Command::new("open");
    #[cfg(target_os = "linux")]
    let mut cmd = std::process::Command::new("xdg-open");
    #[cfg(target_os = "windows")]
    let mut cmd = {
        let mut c = std::process::Command::new("cmd");
        c.args(["/C", "start", ""]);
        c
    };
    cmd.arg(target);
    cmd.spawn().map(|_| ()).map_err(|e| e.to_string())
}

// DESIGN §1.4 — Recovery Screen actions. These commands trust the caller-
// supplied path (the recovery screen plumbs it from the SidecarCrashedPayload
// the supervisor emitted, never from a user-typed input).
#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
fn open_path_in_default_app(path: String) -> Result<(), String> {
    spawn_open(&path)
}

/// Resolve the folder the Help > Open Log Folder item reveals. With a project
/// path, AgentShore writes per-session NDJSON to ``<project>/.agentshore/logs``
/// (the ``log_dir`` config default); without one — no project selected yet —
/// fall back to the global AgentShore home ``~/.config/swink/agentshore``.
/// Pure so the path logic is unit-testable; the caller creates and opens it.
fn resolve_log_folder(project_path: Option<&str>, home: Option<&Path>) -> Option<PathBuf> {
    if let Some(project) = project_path {
        let trimmed = project.trim();
        if !trimmed.is_empty() {
            return Some(PathBuf::from(trimmed).join(".agentshore").join("logs"));
        }
    }
    home.map(|h| h.join(".config").join("swink").join("agentshore"))
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
fn open_log_folder(project_path: Option<String>) -> Result<(), String> {
    // HOME is the norm on macOS/Linux; USERPROFILE is the Windows fallback.
    let home = std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from);
    let folder = resolve_log_folder(project_path.as_deref(), home.as_deref())
        .ok_or_else(|| "could not resolve a log folder".to_string())?;
    // Create it so the opener doesn't fail on a project that hasn't logged yet.
    let _ = std::fs::create_dir_all(&folder);
    spawn_open(&folder.to_string_lossy())
}

/// Diagnostics payload for the Help > Copy Diagnostics item. Assembled in Rust
/// (which owns the bundle version + build target) and emitted to the React
/// shell, which renders it in a copyable dialog.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct Diagnostics {
    app: String,
    version: String,
    os: String,
    arch: String,
}

fn collect_diagnostics(version: &str) -> Diagnostics {
    Diagnostics {
        app: "AgentShore".to_string(),
        version: version.to_string(),
        os: std::env::consts::OS.to_string(),
        arch: std::env::consts::ARCH.to_string(),
    }
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
fn restart_sidecar(app: AppHandle) -> Result<(), String> {
    // Full app restart — the simplest way to reliably re-spawn the sidecar and
    // re-run the handshake (in-place child respawn is out of scope).
    app.restart()
}

/// Reload the main WebView in-place without touching the session.
///
/// A WebView reload resets the React app to its root route. The `current_session`
/// command lets the app reattach to the still-running engine on mount, so this is
/// teardown-free. Called by the `reload_ui` command and the "Reload UI" menu item
/// inline so it works even while the WebView shows a white screen.
///
/// Invariant: must never trigger `session.stop` or any teardown path (audited:
/// teardown is only reachable via CloseRequested/ExitRequested/Exit).
#[cfg_attr(test, allow(dead_code))]
fn reload_main_webview(app: &AppHandle) -> Result<(), String> {
    app.get_webview_window("main")
        .ok_or_else(|| "main webview window not found".to_string())?
        .reload()
        .map_err(|e| e.to_string())
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
fn reload_ui(app: AppHandle) -> Result<(), String> {
    reload_main_webview(&app)
}

/// Returns current session state for the frontend reattach path (#274).
/// `active` mirrors `ActivityHolder::is_active()`; `dashboardUrl`/`sessionId`
/// come from `SessionInfoHolder` (populated on successful `session.start`).
/// If a session is active but no info was cached yet, returns
/// `{active:true, dashboardUrl:null, sessionId:null}`.
#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
fn current_session(app: AppHandle) -> CurrentSessionInfo {
    let active = app.state::<activity::ActivityHolder>().is_active();
    let (dashboard_url, session_id) = match app.state::<SessionInfoHolder>().0.lock() {
        Ok(guard) => match guard.as_ref() {
            Some(info) => (
                Some(info.dashboard_url.clone()),
                Some(info.session_id.clone()),
            ),
            None => (None, None),
        },
        Err(_) => (None, None),
    };
    CurrentSessionInfo {
        active,
        dashboard_url,
        session_id,
    }
}

/// Stamp the heartbeat timestamp and arm the watchdog (Phase 2). Called by the
/// React app on mount and every 2s via a rAF-gated interval. A missed rAF beat
/// (compositor stall / paint wedge) is what stops the call and arms the watchdog.
#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
fn ui_heartbeat(app: AppHandle) {
    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64;
    let beat = app.state::<WebviewHeartbeat>();
    beat.last_beat_ms.store(now_ms, Ordering::SeqCst);
    beat.enabled.store(true, Ordering::SeqCst);
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
fn quit_app(app: AppHandle) -> Result<(), String> {
    // Explicit in-app Quit (recovery / fatal-error screens): the user already
    // chose to quit, so latch confirmation to skip the native prompt.
    app.state::<QuitConfirmed>().set();
    app.exit(0);
    Ok(())
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
fn tracked_agent_pids(app: AppHandle) -> Vec<sidecar::TrackedAgent> {
    with_supervisor(&app, |sup| sup.tracked_agents()).unwrap_or_default()
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
fn kill_all_agents(app: AppHandle) -> Vec<sidecar::TrackedAgent> {
    with_supervisor(&app, |sup| sup.kill_all_agents()).unwrap_or_default()
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
fn get_fatal_shell_state(app: AppHandle) -> Result<Option<sidecar::SupervisorStartError>, String> {
    let state = app.state::<FatalShellState>();
    let guard = state.info.lock().map_err(|e| e.to_string())?;
    Ok(guard.clone())
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
fn set_last_selected_tab(app: AppHandle, tab: String) -> Result<UiState, String> {
    let trimmed = tab.trim();
    if trimmed.is_empty() {
        return Err("tab must not be empty".to_string());
    }
    let next = with_ui_state(&app, |state| {
        state.last_selected_tab = trimmed.to_string();
        state.clone()
    })?;
    persist_ui_state(&app, &next)?;
    Ok(next)
}

/// Show the async "a session is still running" confirmation. Non-blocking:
/// the caller has already prevented the close/exit, and `on_choice(true)` is
/// invoked on the "Quit" button (false on "Cancel"). Async (not `blocking_show`)
/// because this runs on the main thread, where a blocking dialog would deadlock
/// the event loop the dialog itself needs to pump.
#[cfg_attr(test, allow(dead_code))]
fn prompt_quit_confirmation<F: FnOnce(bool) + Send + 'static>(app: &AppHandle, on_choice: F) {
    use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

    app.dialog()
        .message(
            "A AgentShore session is still running. Quitting now force-stops it \
             immediately — in-flight plays are killed and no end-of-session report \
             is written. Quit anyway?",
        )
        .title("Quit AgentShore?")
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom(
            "Quit".to_string(),
            "Cancel".to_string(),
        ))
        .show(on_choice);
}

/// Show the native "dashboard not responding" recovery dialog (Phase 2, #274).
///
/// Called when the heartbeat watchdog trips (JS-alive paint wedge) or the
/// content-process-terminate hook fires. Re-entrant calls are suppressed by
/// `WedgeDialogActive`. Three choices:
///
/// - "Reload UI" → in-place reload; React reattaches via `current_session`.
/// - "Ignore" → dismiss; no action (the user-facing escape hatch for a
///   false-positive trip over an already-fine screen).
/// - "Stop session" → emits `menu:stop_session` for React to drain the session.
///
/// The heartbeat watchdog is disarmed on any outcome; it re-arms when the next
/// `ui_heartbeat` call arrives (i.e. once JS is running again post-reload).
#[cfg(not(test))]
fn declare_webview_wedged(app: &AppHandle, _mode: WebviewWedgeMode) {
    use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};

    let wedge_active = app.state::<WedgeDialogActive>();
    if wedge_active.0.swap(true, Ordering::SeqCst) {
        // Another dialog is already showing; drop this trip.
        return;
    }

    let app_mt = app.clone();
    let _ = app.run_on_main_thread(move || {
        let app_action = app_mt.clone();
        app_mt
            .dialog()
            .message(
                "The AgentShore dashboard is not responding. \
                 The session is still running in the background.",
            )
            .title("Dashboard not responding")
            .buttons(MessageDialogButtons::YesNoCancelCustom(
                "Reload UI".to_string(),
                "Ignore".to_string(),
                "Stop session".to_string(),
            ))
            .show_with_result(move |result| {
                use tauri_plugin_dialog::MessageDialogResult;
                // Disarm the watchdog and the re-entrancy guard on any outcome;
                // the watchdog re-arms when the next ui_heartbeat arrives.
                app_action
                    .state::<WebviewHeartbeat>()
                    .enabled
                    .store(false, Ordering::SeqCst);
                app_action
                    .state::<WedgeDialogActive>()
                    .0
                    .store(false, Ordering::SeqCst);

                // Map positional Yes/No/Cancel AND Custom(label) for the
                // YesNoCancelCustom variant (Linux rfd may return positional;
                // macOS/Windows may return Custom).
                let action = match &result {
                    MessageDialogResult::Yes => "reload",
                    MessageDialogResult::No => "ignore",
                    MessageDialogResult::Cancel => "stop",
                    MessageDialogResult::Custom(label) => match label.as_str() {
                        "Reload UI" => "reload",
                        "Ignore" => "ignore",
                        "Stop session" => "stop",
                        _ => "",
                    },
                    _ => "",
                };
                match action {
                    "reload" => {
                        let _ = reload_main_webview(&app_action);
                    }
                    "ignore" => {}
                    "stop" => {
                        let _ = app_action.emit("menu:stop_session", ());
                    }
                    _ => {}
                }
            });
    });
}

/// Tear down agent subprocesses and the Python sidecar before the shell exits.
/// Order matters: kill the AGENT subprocesses (Claude / Codex / Antigravity CLIs and
/// anything they spawned) FIRST. If we drop the supervisor before killing the
/// agents, the sidecar's tracked-PID map disappears and the agent subprocesses
/// are reparented to launchd, burning API tokens silently (desktop-ieql).
/// How long the quit path may spend on graceful teardown before the watchdog
/// hard-exits the process. Generous enough for taskkill sweeps and final
/// stats persistence; short enough that a wedged teardown can never leave the
/// app running headless (#155).
const TEARDOWN_WATCHDOG_DEADLINE: std::time::Duration = std::time::Duration::from_secs(10);

/// Arm a detached watchdog thread that hard-exits the process after
/// [`TEARDOWN_WATCHDOG_DEADLINE`]. Called ONLY from the quit path
/// (`ExitRequested` fall-through / `Exit`) — never from `session.stop`, whose
/// graceful drain is allowed to take as long as it needs while the window is
/// open. If teardown finishes first, the run loop returns and the process
/// exits normally, taking the watchdog thread with it; the watchdog only ever
/// fires when teardown has stalled past the deadline (#155: a blocked mutex
/// or hung kill must degrade to a hard exit, not a headless orchestrator).
#[cfg_attr(test, allow(dead_code))]
fn arm_teardown_watchdog() {
    std::thread::spawn(|| {
        std::thread::sleep(TEARDOWN_WATCHDOG_DEADLINE);
        eprintln!(
            "[agentshore-desktop] teardown exceeded {TEARDOWN_WATCHDOG_DEADLINE:?}; hard-exiting"
        );
        std::process::exit(0);
    });
}

#[cfg_attr(test, allow(dead_code))]
fn shutdown_sidecar_and_agents(app_handle: &AppHandle) {
    // Take the supervisor OUT of the holder under a short-lived lock, then do
    // all the killing outside it. In-flight RPC threads may still hold Arc
    // clones of the supervisor, so its Drop impl is NOT guaranteed to run
    // here — kill_sidecar() is the explicit teardown and Drop is only the
    // backstop (#155).
    let supervisor: Option<Arc<sidecar::SidecarSupervisor>> = {
        let sidecar_state: tauri::State<'_, SidecarHolderState> = app_handle.state();
        let taken = match sidecar_state.lock() {
            Ok(mut guard) => guard.take().map(|holder| holder.supervisor),
            Err(_) => None,
        };
        taken
    };
    if let Some(sup) = supervisor {
        // Kill the AGENT subprocesses first (see doc comment above), then
        // the Python sidecar tree.
        let _ = sup.kill_all_agents();
        sup.kill_sidecar();
    }
    // desktop-bzr2: release the App Nap assertion if a session was still running
    // at quit time, so it can't linger in pmset past app exit.
    let activity_state: tauri::State<'_, activity::ActivityHolder> = app_handle.state();
    activity_state.release();
}

#[cfg_attr(test, allow(dead_code))]
fn attach_window_persistence(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let app_handle = app.clone();
        window.on_window_event(move |event| match event {
            WindowEvent::Moved(_) | WindowEvent::Resized(_) => {
                update_window_state(&app_handle);
            }
            // Red close button / ⌘W. Confirm before force-killing a live
            // session; ⌘Q and the app-menu Quit are handled by ExitRequested.
            WindowEvent::CloseRequested { api, .. } => {
                let session_active = app_handle.state::<activity::ActivityHolder>().is_active();
                let already_confirmed = app_handle.state::<QuitConfirmed>().get();
                if quit_requires_confirmation(session_active, already_confirmed) {
                    api.prevent_close();
                    let app = app_handle.clone();
                    prompt_quit_confirmation(&app_handle, move |quit| {
                        if quit {
                            app.state::<QuitConfirmed>().set();
                            if let Some(window) = app.get_webview_window("main") {
                                let _ = window.destroy();
                            }
                        }
                    });
                }
            }
            _ => {}
        });
    }
}

/// First-launch window geometry: 90% of the monitor in both dimensions,
/// centered. Inputs and outputs are in **logical** units (CSS-pixel
/// equivalents). Tauri's set_size(Logical) is the only safe path on
/// HiDPI macOS; set_size(Physical) doesn't divide by scale_factor
/// before writing to NSWindow, so passing raw physical pixels produces
/// a window scale_factor× too big. Pure so it stays unit-testable.
fn default_window_rect(
    monitor_size_logical: (f64, f64),
    monitor_pos_logical: (f64, f64),
) -> (f64, f64, f64, f64) {
    let (mw, mh) = monitor_size_logical;
    let (mx, my) = monitor_pos_logical;
    let width = mw * 0.9;
    let height = mh * 0.9;
    let x = mx + (mw - width) / 2.0;
    let y = my + (mh - height) / 2.0;
    (x, y, width, height)
}

#[cfg_attr(test, allow(dead_code))]
fn apply_restored_window_state(app: &AppHandle) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };

    let restored = with_ui_state(app, |state| state.window.clone())
        .ok()
        .flatten();
    let monitor = window.current_monitor().ok().flatten();
    let scale = monitor.as_ref().map(|m| m.scale_factor()).unwrap_or(1.0);

    let used_restored = if let Some(restored) = restored {
        // Persisted values are what outer_size()/outer_position() returned
        // (true physical pixels on macOS Tao). Convert to logical for
        // set_size — set_size(Physical(...)) on macOS doesn't divide by
        // scale_factor before writing to NSWindow, so feeding it physical
        // pixels paints a window scale_factor× too big (and each
        // restore→capture cycle compounds the inflation).
        let logical_w = restored.width as f64 / scale;
        let logical_h = restored.height as f64 / scale;
        let logical_x = restored.x as f64 / scale;
        let logical_y = restored.y as f64 / scale;

        // Sanity-clamp: if the persisted state is larger than the current
        // monitor in either dimension, treat it as corrupt and fall through
        // to the 90%-default. Covers state inflated by the prior cycle of
        // broken set/get pairs before this fix landed.
        let fits = match monitor.as_ref() {
            Some(m) => {
                let mw = m.size().width as f64 / scale;
                let mh = m.size().height as f64 / scale;
                logical_w <= mw && logical_h <= mh
            }
            None => true,
        };

        if fits {
            let _ = window.set_size(tauri::Size::Logical(tauri::LogicalSize::new(
                logical_w, logical_h,
            )));
            let _ = window.set_position(tauri::Position::Logical(tauri::LogicalPosition::new(
                logical_x, logical_y,
            )));
            true
        } else {
            false
        }
    } else {
        false
    };

    if !used_restored {
        if let Some(monitor) = monitor.as_ref() {
            let scale = monitor.scale_factor();
            let size = monitor.size();
            let pos = monitor.position();
            let (x, y, width, height) = default_window_rect(
                (size.width as f64 / scale, size.height as f64 / scale),
                (pos.x as f64 / scale, pos.y as f64 / scale),
            );
            let _ = window.set_size(tauri::Size::Logical(tauri::LogicalSize::new(width, height)));
            let _ =
                window.set_position(tauri::Position::Logical(tauri::LogicalPosition::new(x, y)));
        }
    }

    // The conf-defined size is a small placeholder; this is the first
    // chance to show the window at its real (restored or 90%-of-screen)
    // dimensions without a visible resize flash.
    let _ = window.show();
}

fn resolve_bundled_sidecar_path(command: &Path) -> std::io::Result<PathBuf> {
    let exe_dir = std::env::current_exe()?
        .parent()
        .map(PathBuf::from)
        .unwrap_or_default();

    // Tauri's bundler places `externalBin` alongside the main executable on
    // every platform — on macOS that's Contents/MacOS/, not Contents/Resources/.
    // The old ../Resources/ lookup didn't exist, so bd subcommands silently fell
    // through to whatever bd was on PATH.
    let base_dir = exe_dir;

    let mut command_path = base_dir.join(command);

    #[cfg(windows)]
    {
        let already_exe = command_path.extension().is_some_and(|ext| ext == "exe");
        if !already_exe {
            command_path.as_mut_os_string().push(".exe");
        }
    }

    #[cfg(not(windows))]
    {
        if command_path.extension().is_some_and(|ext| ext == "exe") {
            command_path.set_extension("");
        }
    }

    Ok(command_path)
}

// External Help-menu destinations, opened in the default browser via the OS
// opener (see [`spawn_open`]). The repo is the canonical source for docs,
// release notes (the same tag the updater reads), and issue intake.
#[cfg(not(test))]
const HELP_DOCS_URL: &str = "https://github.com/SuperSwinkAI/Swink-AgentShore#readme";
#[cfg(not(test))]
const HELP_RELEASES_URL: &str = "https://github.com/SuperSwinkAI/Swink-AgentShore/releases";
#[cfg(not(test))]
const HELP_ISSUES_URL: &str = "https://github.com/SuperSwinkAI/Swink-AgentShore/issues/new";

/// Build the app menu and wire menu events to React via Tauri events.
///
/// Custom items emit a `menu:<id>` Tauri event that a React listener picks up
/// (e.g. File > Stop Session → `menu:stop_session` → `session.stop` drain).
/// Items stay enabled — React decides what to do based on current state
/// (no-op rather than show a dialog: cheaper than keeping Rust enabled-state
/// synced over IPC).
///
/// Follows the macOS HIG application-menu convention: the leading "AgentShore"
/// app menu holds About, Preferences (Cmd+,), Services, the Hide / Hide Others /
/// Show All group, and Quit; Adjust Budget / Stop Session / Close Window live in
/// File. Windows/Linux have no app menu, so Preferences lives in File. Check for
/// Updates is hidden everywhere until the updater is provisioned.
#[cfg(not(test))]
fn build_app_menu<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<tauri::menu::Menu<R>> {
    let stop_session = MenuItemBuilder::with_id("stop_session", "Stop Session")
        .accelerator("CmdOrCtrl+Shift+.")
        .build(app)?;

    let adjust_budget = MenuItemBuilder::with_id("adjust_budget", "Adjust Budget…")
        .accelerator("CmdOrCtrl+B")
        .build(app)?;

    // Preferences lives in the macOS App menu and the non-macOS File menu.
    let preferences = MenuItemBuilder::with_id("preferences", "Preferences…")
        .accelerator("CmdOrCtrl+,")
        .build(app)?;
    // Check for Updates is hidden until the updater is provisioned (a real
    // signing keypair + a published `latest.json` release manifest). The React
    // update machinery and the `menu:check_updates` handler stay in place, so
    // re-adding this item to the menu is all that's needed to re-enable it.

    let edit = SubmenuBuilder::new(app, "Edit")
        .item(&PredefinedMenuItem::undo(app, None)?)
        .item(&PredefinedMenuItem::redo(app, None)?)
        .separator()
        .item(&PredefinedMenuItem::cut(app, None)?)
        .item(&PredefinedMenuItem::copy(app, None)?)
        .item(&PredefinedMenuItem::paste(app, None)?)
        .item(&PredefinedMenuItem::select_all(app, None)?)
        .build()?;

    let window = SubmenuBuilder::new(app, "Window")
        .item(&PredefinedMenuItem::minimize(app, None)?)
        .item(&PredefinedMenuItem::maximize(app, None)?)
        .build()?;

    let welcome_tour = MenuItemBuilder::with_id("help_welcome_tour", "Welcome Tour").build(app)?;
    let documentation =
        MenuItemBuilder::with_id("help_documentation", "Documentation").build(app)?;
    let release_notes =
        MenuItemBuilder::with_id("help_release_notes", "Release Notes").build(app)?;
    let report_issue =
        MenuItemBuilder::with_id("help_report_issue", "Report an Issue").build(app)?;
    let keyboard_shortcuts =
        MenuItemBuilder::with_id("help_keyboard_shortcuts", "Keyboard Shortcuts").build(app)?;
    let open_logs = MenuItemBuilder::with_id("help_open_logs", "Open Log Folder").build(app)?;
    let copy_diagnostics =
        MenuItemBuilder::with_id("help_copy_diagnostics", "Copy Diagnostics").build(app)?;

    let mut menu = MenuBuilder::new(app);

    // macOS: the leading application menu (HIG convention). Building it
    // explicitly replaces the menu macOS would auto-supply, so we re-add the
    // standard About / Services / Hide / Show All / Quit items in their
    // conventional positions. Preferences (Cmd+,) belongs here too.
    #[cfg(target_os = "macos")]
    {
        let app_menu = SubmenuBuilder::new(app, "AgentShore")
            .item(&PredefinedMenuItem::about(app, None, None)?)
            .separator()
            .item(&preferences)
            .separator()
            .item(&PredefinedMenuItem::services(app, None)?)
            .separator()
            .item(&PredefinedMenuItem::hide(app, None)?)
            .item(&PredefinedMenuItem::hide_others(app, None)?)
            .item(&PredefinedMenuItem::show_all(app, None)?)
            .separator()
            .item(&PredefinedMenuItem::quit(app, None)?)
            .build()?;
        menu = menu.item(&app_menu);
    }

    // File holds the session/window commands. Preferences lives here only on
    // Windows/Linux (no app menu there); on macOS it's in the app menu above.
    #[allow(unused_mut)]
    let mut file_builder = SubmenuBuilder::new(app, "File")
        .item(&adjust_budget)
        .item(&stop_session);
    #[cfg(not(target_os = "macos"))]
    {
        file_builder = file_builder.separator().item(&preferences);
    }
    let file = file_builder
        .separator()
        .item(&PredefinedMenuItem::close_window(
            app,
            Some("Close Window"),
        )?)
        .build()?;

    let reload_ui_item = MenuItemBuilder::with_id("reload_ui", "Reload UI")
        .accelerator("CmdOrCtrl+R")
        .build(app)?;

    let view = SubmenuBuilder::new(app, "View")
        .item(&reload_ui_item)
        .separator()
        .item(&PredefinedMenuItem::fullscreen(app, None)?)
        .build()?;

    // Help: documentation / support links plus the diagnostics helpers. Check
    // for Updates is intentionally omitted until the updater is provisioned.
    let help = SubmenuBuilder::new(app, "Help")
        .item(&welcome_tour)
        .separator()
        .item(&documentation)
        .item(&release_notes)
        .item(&report_issue)
        .separator()
        .item(&keyboard_shortcuts)
        .separator()
        .item(&open_logs)
        .item(&copy_diagnostics)
        .build()?;

    menu = menu
        .item(&file)
        .item(&edit)
        .item(&view)
        .item(&window)
        .item(&help);
    menu.build()
}

#[cfg(not(test))]
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default()
        // DESIGN §1.3 — single-instance enforcement. A second launch
        // attempt (e.g. user runs `open AgentShore.app` again) hits this
        // handler instead of spawning a parallel sidecar; we focus the
        // existing main window so the user lands back in the running app.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_store::Builder::new().build())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(UiStateHolder::default())
        .manage::<SidecarHolderState>(Mutex::new(None))
        .manage(FatalShellState::default())
        .manage(activity::ActivityHolder::new())
        .manage(QuitConfirmed::default())
        .manage(SessionInfoHolder::default())
        .manage(WebviewHeartbeat::default())
        .manage(WedgeDialogActive::default());

    // macOS WKWebView renderer-death hook (#274, Phase 1+2). Registering a
    // handler REPLACES wry's default auto-reload, so we must reload ourselves.
    // The terminate hook fires for true renderer process death; the
    // heartbeat watchdog (setup below) covers the JS-alive paint-wedge case.
    // Broken out of the builder chain so the #[cfg] can be a statement attribute.
    #[cfg(target_os = "macos")]
    let builder = builder.on_web_content_process_terminate(|webview| {
        eprintln!(
            "[agentshore-desktop] WKWebView content process terminated; reloading main webview"
        );
        let _ = webview.reload();
        // Disarm heartbeat watchdog; it re-arms on the next ui_heartbeat call.
        webview
            .app_handle()
            .state::<WebviewHeartbeat>()
            .enabled
            .store(false, Ordering::SeqCst);
    });

    builder
        .on_menu_event(|app, event| {
            // Custom items fan out to React via `menu:<id>` events (the
            // session-scoped ones are handled by SessionDashboardScreen, the
            // app-global ones by the AppMenu controller). The three Help
            // URL items and Copy Diagnostics are handled inline in Rust —
            // no React round-trip needed.
            match event.id().as_ref() {
                // React's SessionDashboardScreen drives these against the
                // running session; emitted unconditionally (no-op outside one).
                "stop_session" => {
                    let _ = app.emit("menu:stop_session", ());
                }
                "adjust_budget" => {
                    let _ = app.emit("menu:adjust_budget", ());
                }
                "preferences" => {
                    let _ = app.emit("menu:preferences", ());
                }
                "check_updates" => {
                    let _ = app.emit("menu:check_updates", ());
                }
                "help_welcome_tour" => {
                    let _ = app.emit("menu:welcome_tour", ());
                }
                "help_keyboard_shortcuts" => {
                    let _ = app.emit("menu:keyboard_shortcuts", ());
                }
                // React resolves the active project path then invokes
                // open_log_folder (it knows the selected project; Rust here
                // does not).
                "help_open_logs" => {
                    let _ = app.emit("menu:open_logs", ());
                }
                "help_copy_diagnostics" => {
                    let diag = collect_diagnostics(&app.package_info().version.to_string());
                    let _ = app.emit("menu:copy_diagnostics", diag);
                }
                "help_documentation" => {
                    let _ = spawn_open(HELP_DOCS_URL);
                }
                "help_release_notes" => {
                    let _ = spawn_open(HELP_RELEASES_URL);
                }
                "help_report_issue" => {
                    let _ = spawn_open(HELP_ISSUES_URL);
                }
                // Handled inline in Rust — must work while the WebView is white.
                "reload_ui" => {
                    let _ = reload_main_webview(app);
                }
                _ => {}
            }
        })
        .setup(|app| {
            let app_handle = app.handle().clone();
            let state = read_ui_state(&app_handle);
            let _ = with_ui_state(&app_handle, |current| {
                *current = state;
            });

            // Install the app menu (File > Stop Session etc.). macOS auto-
            // supplies the leading "App" menu with About / Hide / Quit
            // when set_menu doesn't pre-populate it.
            let menu = build_app_menu(&app_handle)?;
            app.set_menu(menu)?;

            let bd_sidecar_path = resolve_bundled_sidecar_path(Path::new("agentshore-bd"))
                .ok()
                .filter(|path| path.is_file());
            // Show the shell before sidecar startup. On Windows, process
            // launch or handshake failures otherwise leave an invisible
            // but still-running GUI process because the window starts hidden.
            apply_restored_window_state(&app_handle);
            attach_window_persistence(&app_handle);

            // DESIGN §2.6 — survive a supervisor-startup failure: store
            // the structured error in FatalShellState, emit an "app:fatal_error"
            // Tauri event for the React shell to pick up immediately, and
            // leave SidecarHolderState as ``None`` so the WebView can route
            // to /fatal-error. Quit / Open log buttons on that screen are
            // the only allowed actions.
            match sidecar::SidecarSupervisor::start_classified(
                &app_handle,
                bd_sidecar_path.as_deref(),
            ) {
                Ok(supervisor) => {
                    let holder_state = app_handle.state::<SidecarHolderState>();
                    let mut guard = holder_state
                        .lock()
                        .map_err(|e| std::io::Error::other(e.to_string()))?;
                    *guard = Some(SidecarHolder {
                        supervisor: Arc::new(supervisor),
                    });
                }
                Err(err) => {
                    let fatal = app_handle.state::<FatalShellState>();
                    if let Ok(mut guard) = fatal.info.lock() {
                        *guard = Some(err.clone());
                    }
                    let _ = app_handle.emit("app:fatal_error", err);
                }
            }

            // Phase 2 — heartbeat watchdog (#274). A single long-lived thread
            // checks for missed rAF beats. The watchdog only trips when ALL of:
            // enabled (first beat arrived), a session is active, and now minus
            // last_beat > WEDGE_THRESHOLD_MS. On trip it shows the native
            // fallback dialog and disarms; the next ui_heartbeat re-arms it.
            let watchdog_app = app_handle.clone();
            std::thread::spawn(move || {
                const WEDGE_THRESHOLD_MS: i64 = 10_000; // ~10s / 5 missed 2s beats

                // Require the stale condition to hold for this many
                // consecutive 1s polls before declaring a wedge — absorbs
                // transient stalls (GC pause, heavy re-render under a large
                // fleet) that self-heal within a couple more seconds, at the
                // cost of ~3s slower detection of a genuine wedge.
                const WEDGE_CONFIRM_POLLS: u32 = 3;
                let mut wedge_declared = false;
                let mut consecutive_stale_polls: u32 = 0;
                loop {
                    std::thread::sleep(std::time::Duration::from_millis(1_000));
                    let beat = watchdog_app.state::<WebviewHeartbeat>();
                    if !beat.enabled.load(Ordering::SeqCst) {
                        // Not armed or disarmed after a reload; reset wedge flag.
                        wedge_declared = false;
                        consecutive_stale_polls = 0;
                        continue;
                    }
                    let active = watchdog_app.state::<activity::ActivityHolder>().is_active();
                    if !active {
                        // Session ended; disarm until the next session starts.
                        beat.enabled.store(false, Ordering::SeqCst);
                        wedge_declared = false;
                        consecutive_stale_polls = 0;
                        continue;
                    }
                    let now_ms = std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_millis() as i64;
                    let last = beat.last_beat_ms.load(Ordering::SeqCst);
                    let shutdown_in_progress = beat.esr_ready.load(Ordering::SeqCst)
                        || beat.draining.load(Ordering::SeqCst);
                    let stale = should_declare_wedge(
                        true,
                        active,
                        shutdown_in_progress,
                        last,
                        now_ms,
                        WEDGE_THRESHOLD_MS,
                    );
                    if stale && !wedge_declared {
                        consecutive_stale_polls += 1;
                        if wedge_confirmed(consecutive_stale_polls, WEDGE_CONFIRM_POLLS) {
                            wedge_declared = true;
                            declare_webview_wedged(&watchdog_app, WebviewWedgeMode::Heartbeat);
                        }
                    } else {
                        consecutive_stale_polls = 0;
                    }
                }
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            load_ui_state,
            set_ui_theme,
            set_onboarding_completed,
            set_last_selected_tab,
            read_text_file,
            jsonrpc_call,
            open_path_in_default_app,
            open_log_folder,
            restart_sidecar,
            reload_ui,
            current_session,
            ui_heartbeat,
            quit_app,
            tracked_agent_pids,
            kill_all_agents,
            get_fatal_shell_state
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            match event {
                // macOS dock-icon click on a running app. Tauri 2 fires
                // Reopen but does not bring the window to front by
                // default — without this the user sees nothing happen
                // and has to minimize every other app to find the
                // AgentShore window underneath. Mirror what
                // tauri_plugin_single_instance does for relaunch.
                #[cfg(target_os = "macos")]
                tauri::RunEvent::Reopen {
                    has_visible_windows,
                    ..
                } => {
                    if let Some(window) = app_handle.get_webview_window("main") {
                        if !has_visible_windows {
                            let _ = window.show();
                        }
                        let _ = window.unminimize();
                        let _ = window.set_focus();
                    }
                }
                // ⌘Q / app-menu Quit / NSApp terminate. macOS short-circuits
                // to exit() and can skip our SidecarSupervisor::drop, so we tear
                // the sidecar down explicitly. But first: if a session is still
                // running, confirm — quitting force-kills it with no drain and
                // no end-of-session report.
                tauri::RunEvent::ExitRequested { api, .. } => {
                    let session_active = app_handle.state::<activity::ActivityHolder>().is_active();
                    let already_confirmed = app_handle.state::<QuitConfirmed>().get();
                    if quit_requires_confirmation(session_active, already_confirmed) {
                        api.prevent_exit();
                        let app = app_handle.clone();
                        prompt_quit_confirmation(app_handle, move |quit| {
                            if quit {
                                app.state::<QuitConfirmed>().set();
                                app.exit(0);
                            }
                        });
                        return;
                    }
                    // Quit is going ahead: bound the teardown. If anything
                    // below (or in the Exit handler) stalls, the watchdog
                    // hard-exits instead of leaving a headless orchestrator.
                    arm_teardown_watchdog();
                    shutdown_sidecar_and_agents(app_handle);
                }
                tauri::RunEvent::Exit => {
                    arm_teardown_watchdog();
                    shutdown_sidecar_and_agents(app_handle);
                }
                _ => {}
            }
        });
}

#[cfg(test)]
pub fn run() {}

#[cfg(test)]
mod tests {
    use super::{
        collect_diagnostics, default_window_rect, read_text_file_impl,
        resolve_bundled_sidecar_path, resolve_log_folder, UiState,
    };
    use std::io::Write;
    use std::path::{Path, PathBuf};

    #[test]
    fn default_window_rect_is_90_percent_centered_on_origin_monitor() {
        let (x, y, w, h) = default_window_rect((2560.0, 1440.0), (0.0, 0.0));
        assert!((w - 2304.0).abs() < 0.5);
        assert!((h - 1296.0).abs() < 0.5);
        assert!((x - 128.0).abs() < 0.5);
        assert!((y - 72.0).abs() < 0.5);
    }

    #[test]
    fn default_window_rect_centers_on_offset_monitor() {
        let (x, y, w, h) = default_window_rect((1920.0, 1080.0), (1920.0, 0.0));
        assert!((w - 1728.0).abs() < 0.5);
        assert!((h - 972.0).abs() < 0.5);
        assert!((x - 2016.0).abs() < 0.5);
        assert!((y - 54.0).abs() < 0.5);
    }

    #[test]
    fn default_window_rect_uses_logical_units_not_physical() {
        // On a 2560x1440 logical screen (physical 5120x2880 @ 2x), 90%
        // must be ~2304x1296 in logical units — never the 4608x2592 the
        // pre-fix Physical path produced (which appeared as a window
        // ~1.8× the logical screen width).
        let (_x, _y, w, _h) = default_window_rect((2560.0, 1440.0), (0.0, 0.0));
        assert!(
            w < 2560.0,
            "window must not be wider than the logical screen"
        );
    }

    #[test]
    fn quit_requires_confirmation_only_when_session_active_and_unconfirmed() {
        use super::quit_requires_confirmation;
        // Live session, not yet approved → prompt.
        assert!(quit_requires_confirmation(true, false));
        // Live session but already approved (re-entrant close / explicit Quit).
        assert!(!quit_requires_confirmation(true, true));
        // No session → never prompt, regardless of the latch.
        assert!(!quit_requires_confirmation(false, false));
        assert!(!quit_requires_confirmation(false, true));
    }

    #[test]
    fn should_declare_wedge_trips_when_stale_and_not_esr_ready() {
        use super::should_declare_wedge;
        // enabled, active, not esr_ready, beat stamped at t=0, now=11s, 10s
        // threshold → 11s elapsed > 10s → trip.
        assert!(should_declare_wedge(true, true, false, 1, 11_000, 10_000));
    }

    #[test]
    fn should_declare_wedge_suppressed_once_esr_ready() {
        use super::should_declare_wedge;
        // Same staleness as above, but esr_ready=true suppresses the trip —
        // this is the fix: the trailing session.completed gap (timelapse
        // finalization etc.) must not be mistaken for a paint wedge.
        assert!(!should_declare_wedge(true, true, true, 1, 11_000, 10_000));
    }

    #[test]
    fn should_declare_wedge_suppressed_when_draining() {
        use super::should_declare_wedge;
        // Same staleness as the esr_ready test, but the shutdown_in_progress
        // param is driven by `draining` (session.draining, fired at drain
        // start) rather than esr_ready — must suppress the trip identically,
        // since this is the earlier of the two signals in the real call site.
        assert!(!should_declare_wedge(true, true, true, 1, 11_000, 10_000));
    }

    #[test]
    fn wedge_confirmed_false_below_threshold() {
        use super::wedge_confirmed;
        assert!(!wedge_confirmed(0, 3));
        assert!(!wedge_confirmed(1, 3));
        assert!(!wedge_confirmed(2, 3));
    }

    #[test]
    fn wedge_confirmed_true_at_and_above_threshold() {
        use super::wedge_confirmed;
        assert!(wedge_confirmed(3, 3));
        assert!(wedge_confirmed(4, 3));
    }

    #[test]
    fn should_declare_wedge_false_when_disabled() {
        use super::should_declare_wedge;
        assert!(!should_declare_wedge(false, true, false, 1, 11_000, 10_000));
    }

    #[test]
    fn should_declare_wedge_false_when_inactive() {
        use super::should_declare_wedge;
        assert!(!should_declare_wedge(true, false, false, 1, 11_000, 10_000));
    }

    #[test]
    fn should_declare_wedge_false_when_no_beat_yet() {
        use super::should_declare_wedge;
        // last_beat_ms == 0 means "enabled but no beat stamped yet" — never trip.
        assert!(!should_declare_wedge(true, true, false, 0, 11_000, 10_000));
        assert!(!should_declare_wedge(true, true, false, 0, 0, 10_000));
    }

    #[test]
    fn should_declare_wedge_false_within_threshold() {
        use super::should_declare_wedge;
        // 5s elapsed against a 10s threshold, beat stamped at t=5_000.
        assert!(!should_declare_wedge(
            true, true, false, 5_000, 10_000, 10_000
        ));
    }

    #[test]
    fn quit_confirmed_latch_sets_once() {
        use super::QuitConfirmed;
        let guard = QuitConfirmed::default();
        assert!(!guard.get());
        guard.set();
        assert!(guard.get());
        // Idempotent.
        guard.set();
        assert!(guard.get());
    }

    #[test]
    fn ui_state_defaults_match_shell_expectations() {
        let state = UiState::default();
        assert_eq!(state.theme, "system");
        assert_eq!(state.last_selected_tab, "home");
        assert!(state.window.is_none());
        assert!(!state.onboarding_completed);
    }

    #[test]
    fn ui_state_deserialize_invalid_payload_falls_back_to_default() {
        let parsed = serde_json::from_str::<UiState>("{\"theme\":123}");
        assert!(parsed.is_err());
    }

    #[test]
    fn ui_state_legacy_blob_without_onboarding_field_preserves_other_settings() {
        // A `ui-state.json` written before `onboarding_completed` existed must
        // still deserialize (via `#[serde(default)]`) rather than wiping the
        // user's theme / tab / window through the `unwrap_or_default()` path.
        let legacy = "{\"theme\":\"dark\",\"lastSelectedTab\":\"stats\",\"window\":null}";
        let parsed = serde_json::from_str::<UiState>(legacy).expect("legacy blob deserializes");
        assert_eq!(parsed.theme, "dark");
        assert_eq!(parsed.last_selected_tab, "stats");
        assert!(!parsed.onboarding_completed);
    }

    #[test]
    fn ui_state_round_trips_onboarding_completed() {
        let state = UiState {
            onboarding_completed: true,
            ..UiState::default()
        };
        let json = serde_json::to_string(&state).expect("serialize");
        let parsed = serde_json::from_str::<UiState>(&json).expect("deserialize");
        assert!(parsed.onboarding_completed);
    }

    #[test]
    fn read_text_file_round_trips_a_tmp_file() {
        let dir = std::env::temp_dir();
        let path = dir.join("agentshore-desktop-read-text-file-test.txt");
        {
            let mut file = std::fs::File::create(&path).expect("create tmp file");
            file.write_all(b"hello-from-tauri").expect("write tmp file");
        }
        let result = read_text_file_impl(path.clone()).expect("read tmp file");
        assert_eq!(result, "hello-from-tauri");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn read_text_file_missing_path_returns_err() {
        let missing = std::env::temp_dir().join("agentshore-desktop-no-such-file-xyz");
        let _ = std::fs::remove_file(&missing);
        let result = read_text_file_impl(missing);
        assert!(result.is_err());
    }

    #[test]
    fn resolve_log_folder_prefers_project_logs_dir() {
        let folder = resolve_log_folder(Some("/tmp/proj"), Some(Path::new("/home/u")))
            .expect("project path resolves a folder");
        assert_eq!(folder, PathBuf::from("/tmp/proj/.agentshore/logs"));
    }

    #[test]
    fn resolve_log_folder_falls_back_to_global_home_when_no_project() {
        let folder = resolve_log_folder(None, Some(Path::new("/home/u")))
            .expect("home resolves the global folder");
        assert_eq!(folder, PathBuf::from("/home/u/.config/swink/agentshore"));
    }

    #[test]
    fn resolve_log_folder_treats_blank_project_as_unset() {
        let folder = resolve_log_folder(Some("   "), Some(Path::new("/home/u")))
            .expect("blank project falls through to home");
        assert_eq!(folder, PathBuf::from("/home/u/.config/swink/agentshore"));
    }

    #[test]
    fn resolve_log_folder_returns_none_without_project_or_home() {
        assert!(resolve_log_folder(None, None).is_none());
    }

    #[test]
    fn collect_diagnostics_captures_version_and_build_target() {
        let diag = collect_diagnostics("1.2.3");
        assert_eq!(diag.app, "AgentShore");
        assert_eq!(diag.version, "1.2.3");
        assert_eq!(diag.os, std::env::consts::OS);
        assert_eq!(diag.arch, std::env::consts::ARCH);
    }

    #[test]
    fn resolve_bundled_sidecar_path_keeps_binary_stem() {
        let path = resolve_bundled_sidecar_path(std::path::Path::new("agentshore-bd"))
            .expect("resolve sidecar path");
        assert_eq!(
            path.file_stem().and_then(|stem| stem.to_str()),
            Some("agentshore-bd")
        );
    }
}
