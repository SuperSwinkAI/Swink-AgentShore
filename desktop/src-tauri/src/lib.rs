use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::Path;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
#[cfg(not(test))]
use tauri::menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem, SubmenuBuilder};
use tauri::{AppHandle, Manager, WindowEvent};
#[cfg(not(test))]
use tauri::{Emitter, Runtime};
use tauri_plugin_store::StoreExt;

pub mod activity;
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
    supervisor: sidecar::SidecarSupervisor,
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
    let state = app.state::<SidecarHolderState>();
    let guard = state.lock().map_err(|e| e.to_string())?;
    match guard.as_ref() {
        Some(holder) => Ok(f(&holder.supervisor)),
        None => Err("sidecar unavailable (shell is in fatal-error state)".to_string()),
    }
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
fn jsonrpc_call(app: AppHandle, method: String, params: Option<Value>) -> Result<Value, String> {
    let method_for_hook = method.clone();
    let result = with_supervisor(&app, |sup| sup.call(method, params))?;
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

// DESIGN §1.4 — Recovery Screen actions. These commands trust the caller-
// supplied path (the recovery screen plumbs it from the SidecarCrashedPayload
// the supervisor emitted, never from a user-typed input).
#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
fn open_path_in_default_app(path: String) -> Result<(), String> {
    if path.trim().is_empty() {
        return Err("path must not be empty".to_string());
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
    cmd.arg(&path);
    cmd.spawn().map(|_| ()).map_err(|e| e.to_string())
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
#[cfg_attr(test, allow(dead_code))]
fn shutdown_sidecar_and_agents(app_handle: &AppHandle) {
    {
        let sidecar_state: tauri::State<'_, SidecarHolderState> = app_handle.state();
        let lock_result = sidecar_state.lock();
        if let Ok(guard) = lock_result {
            if let Some(holder) = guard.as_ref() {
                let _ = holder.supervisor.kill_all_agents();
            }
        }
    }
    // Now drop the supervisor so its Drop impl SIGKILLs the Python sidecar.
    let sidecar_state: tauri::State<'_, SidecarHolderState> = app_handle.state();
    let lock_result = sidecar_state.lock();
    if let Ok(mut guard) = lock_result {
        *guard = None;
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

/// Build the app menu and wire menu events to React via Tauri events.
///
/// File > Stop Session emits `menu:stop_session`; React's session-dashboard
/// listener calls the `session.stop` JSON-RPC drain. The item is always
/// enabled — React decides what to do if a session isn't running (no-op
/// rather than show a dialog: cheaper to ignore than to gate from Rust,
/// which would need IPC to keep enabled-state synced with session state).
#[cfg(not(test))]
fn build_app_menu<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<tauri::menu::Menu<R>> {
    let stop_session = MenuItemBuilder::with_id("stop_session", "Stop Session")
        .accelerator("CmdOrCtrl+Shift+.")
        .build(app)?;

    let adjust_budget = MenuItemBuilder::with_id("adjust_budget", "Adjust Budget…")
        .accelerator("CmdOrCtrl+B")
        .build(app)?;

    let file = SubmenuBuilder::new(app, "File")
        .item(&adjust_budget)
        .item(&stop_session)
        .separator()
        .item(&PredefinedMenuItem::close_window(
            app,
            Some("Close Window"),
        )?)
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

    let mut menu = MenuBuilder::new(app);
    // The leading App menu (About / Hide / Quit) is auto-supplied by
    // macOS as long as we don't pre-populate it. Tauri 2 wires the
    // standard items into the app submenu when set_menu is called.
    menu = menu.item(&file).item(&edit).item(&view).item(&window);
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
            if event.id().as_ref() == "stop_session" {
                // React's SessionDashboardScreen listens for this event
                // and dispatches the session.stop drain. Emitting here
                // unconditionally — React decides what to do based on
                // current session state. No-op outside a session.
                let _ = app.emit("menu:stop_session", ());
            } else if event.id().as_ref() == "adjust_budget" {
                // React's SessionDashboardScreen listens for this event
                // and opens the live Adjust Budget dialog (session.get_budget
                // / session.set_budget). Emitting unconditionally — React
                // decides what to do based on current session state.
                let _ = app.emit("menu:adjust_budget", ());
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
                    *guard = Some(SidecarHolder { supervisor });
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
                    shutdown_sidecar_and_agents(app_handle);
                }
                tauri::RunEvent::Exit => {
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
    use super::{default_window_rect, read_text_file_impl, resolve_bundled_sidecar_path, UiState};
    use std::io::Write;

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
    fn resolve_bundled_sidecar_path_keeps_binary_stem() {
        let path = resolve_bundled_sidecar_path(std::path::Path::new("agentshore-bd"))
            .expect("resolve sidecar path");
        assert_eq!(
            path.file_stem().and_then(|stem| stem.to_str()),
            Some("agentshore-bd")
        );
    }
}
