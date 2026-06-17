use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::Path;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
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
}

impl Default for UiState {
    fn default() -> Self {
        Self {
            theme: "system".to_string(),
            last_selected_tab: "home".to_string(),
            window: None,
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
    // Clone the Arc out under a short-lived lock, then run `f` OUTSIDE the
    // lock. `f` is typically a blocking JSON-RPC call that can run for
    // minutes (session.start) to hours (session.stop drain); holding the
    // holder lock for that duration blockaded shutdown_sidecar_and_agents'
    // lock acquisition on window close, so the Tauri run loop never returned
    // and the app lingered headless with the orchestrator still running (#155).
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
    // Run the (blocking) supervisor call OFF the main thread. `jsonrpc_call`
    // used to be a synchronous command, and Tauri runs sync commands on the
    // main UI thread — so a long RPC (session.start can take many seconds to
    // minutes for the PPO/beads/bridge bringup) froze the window ("not
    // responding") and stalled $/progress rendering. spawn_blocking keeps the
    // event loop pumping so the UI stays live and the startup checklist updates.
    let app_for_call = app.clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        with_supervisor(&app_for_call, |sup| sup.call(method, params))
    })
    .await
    .map_err(|e| format!("sidecar call task failed: {e}"))??;
    // desktop-bzr2: hold an NSProcessInfo activity assertion while a
    // AgentShore session is alive so App Nap can't throttle the Tauri UI's
    // event loop while the window is backgrounded. Acquire on
    // successful session.start, release on session.stop. session.completed
    // (natural exit) lands as a notification and is handled in
    // sidecar::handle_sidecar_notification.
    if result.is_ok() {
        let holder = app.state::<activity::ActivityHolder>();
        match method_for_hook.as_str() {
            "session.start" => {
                holder.acquire("AgentShore session active");
            }
            "session.stop" => {
                holder.release();
            }
            _ => {}
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
    // Full restart of the entire app — the simplest implementation that
    // reliably re-spawns the sidecar and re-runs the handshake. A future
    // refinement could in-place respawn just the supervisor's child
    // process without tearing down the WebView; that's out of scope here.
    app.restart()
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

/// Tear down agent subprocesses and the Python sidecar before the shell exits.
/// Order matters: kill the AGENT subprocesses (Claude / Codex / Gemini CLIs and
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
    // desktop-bzr2: release the App Nap activity assertion if a session was
    // still running at quit time. Without this the assertion can linger in
    // pmset for a fraction of a second past app exit; belt-and-suspenders for
    // the session.completed path.
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

    // Tauri's bundler places `externalBin` entries alongside the main
    // executable on every platform — on macOS that's Contents/MacOS/,
    // not Contents/Resources/. Earlier macOS arm of this lookup pointed
    // at ../Resources/agentshore-bd which doesn't exist, so the Python
    // sidecar logged `agentshore_bd_bin_invalid` and bd subcommands silently
    // fell through to whatever bd was on PATH (or nothing).
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
/// synced over IPC). Standard parity items added here:
///   - Preferences… (Cmd+,): App menu on macOS, File menu elsewhere.
///   - Check for Updates…: App menu on macOS, Help menu elsewhere.
///   - Help: Documentation / Release Notes / Report an Issue (URLs),
///     Keyboard Shortcuts, Open Log Folder, Copy Diagnostics.
#[cfg(not(test))]
fn build_app_menu<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<tauri::menu::Menu<R>> {
    let stop_session = MenuItemBuilder::with_id("stop_session", "Stop Session")
        .accelerator("CmdOrCtrl+Shift+.")
        .build(app)?;

    let adjust_budget = MenuItemBuilder::with_id("adjust_budget", "Adjust Budget…")
        .accelerator("CmdOrCtrl+B")
        .build(app)?;

    // Shared between the macOS App menu and the non-macOS File/Help menus —
    // each platform branch references both, so neither goes unused.
    let preferences = MenuItemBuilder::with_id("preferences", "Preferences…")
        .accelerator("CmdOrCtrl+,")
        .build(app)?;
    let check_updates =
        MenuItemBuilder::with_id("check_updates", "Check for Updates…").build(app)?;

    let mut menu = MenuBuilder::new(app);

    // macOS: build the leading App menu explicitly so Preferences and Check
    // for Updates land in their conventional home. Doing so replaces the menu
    // macOS would otherwise auto-supply, so we re-add the standard items.
    #[cfg(target_os = "macos")]
    {
        let app_menu = SubmenuBuilder::new(app, "AgentShore")
            .item(&PredefinedMenuItem::about(app, None, None)?)
            .item(&check_updates)
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

    // `mut` is used only on the non-macOS branch (Preferences in File); on
    // macOS Preferences lives in the App menu, so the builder isn't rebound.
    #[allow(unused_mut)]
    let mut file_builder = SubmenuBuilder::new(app, "File")
        .item(&adjust_budget)
        .item(&stop_session);
    // On Windows/Linux there is no App menu, so Preferences lives in File.
    #[cfg(not(target_os = "macos"))]
    {
        file_builder = file_builder.separator().item(&preferences);
    }
    let file = file_builder
        .separator()
        .item(&PredefinedMenuItem::close_window(app, Some("Close Window"))?)
        .build()?;

    let edit = SubmenuBuilder::new(app, "Edit")
        .item(&PredefinedMenuItem::undo(app, None)?)
        .item(&PredefinedMenuItem::redo(app, None)?)
        .separator()
        .item(&PredefinedMenuItem::cut(app, None)?)
        .item(&PredefinedMenuItem::copy(app, None)?)
        .item(&PredefinedMenuItem::paste(app, None)?)
        .item(&PredefinedMenuItem::select_all(app, None)?)
        .build()?;

    let view = SubmenuBuilder::new(app, "View")
        .item(&PredefinedMenuItem::fullscreen(app, None)?)
        .build()?;

    let window = SubmenuBuilder::new(app, "Window")
        .item(&PredefinedMenuItem::minimize(app, None)?)
        .item(&PredefinedMenuItem::maximize(app, None)?)
        .build()?;

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

    // `mut` is used only on the non-macOS branch (Check for Updates in Help).
    #[allow(unused_mut)]
    let mut help_builder = SubmenuBuilder::new(app, "Help")
        .item(&documentation)
        .item(&release_notes)
        .item(&report_issue)
        .separator()
        .item(&keyboard_shortcuts)
        .separator()
        .item(&open_logs)
        .item(&copy_diagnostics);
    // Check for Updates is in the App menu on macOS; everywhere else its
    // conventional home is the Help menu.
    #[cfg(not(target_os = "macos"))]
    {
        help_builder = help_builder.separator().item(&check_updates);
    }
    let help = help_builder.build()?;

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
    tauri::Builder::default()
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
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            load_ui_state,
            set_ui_theme,
            set_last_selected_tab,
            read_text_file,
            jsonrpc_call,
            open_path_in_default_app,
            open_log_folder,
            restart_sidecar,
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
        collect_diagnostics, default_window_rect, read_text_file_impl, resolve_bundled_sidecar_path,
        resolve_log_folder, UiState,
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
    }

    #[test]
    fn ui_state_deserialize_invalid_payload_falls_back_to_default() {
        let parsed = serde_json::from_str::<UiState>("{\"theme\":123}");
        assert!(parsed.is_err());
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
