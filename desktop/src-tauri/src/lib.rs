//! Tauri app entry point: state registration, the JSON-RPC bridge to the
//! Python sidecar (`jsonrpc_call`), session-facing commands, and `run()`
//! wiring. Window geometry, UI-state persistence, quit confirmation, the
//! heartbeat watchdog, the app menu, and Help-menu diagnostics each live in
//! their own module — see `window`, `ui_state`, `quit`, `heartbeat`, `menu`,
//! `diagnostics`.

use serde::Serialize;
use serde_json::Value;
use std::path::Path;
use std::path::PathBuf;
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex};
#[cfg(not(test))]
use tauri::Emitter;
use tauri::{AppHandle, Manager};

pub mod activity;
mod diagnostics;
mod heartbeat;
pub mod install_layout;
pub mod jsonrpc_stdio;
mod menu;
pub mod methods;
mod quit;
pub mod readiness;
pub mod sidecar;
mod sidecar_env;
mod sidecar_pid;
mod sidecar_runtime;
mod ui_state;
mod window;

use heartbeat::WebviewHeartbeat;
#[cfg(not(test))]
use quit::QuitConfirmed;
#[cfg(not(test))]
use ui_state::{read_ui_state, with_ui_state, UiStateHolder};

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
                methods::SESSION_START => {
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
                methods::SESSION_STOP => {
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
        .manage(heartbeat::WedgeDialogActive::default());

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
        .on_menu_event(menu::handle_menu_event)
        .setup(|app| {
            let app_handle = app.handle().clone();
            let state = read_ui_state(&app_handle);
            let _ = with_ui_state(&app_handle, |current| {
                *current = state;
            });

            // Install the app menu (File > Stop Session etc.). macOS auto-
            // supplies the leading "App" menu with About / Hide / Quit
            // when set_menu doesn't pre-populate it.
            let menu = menu::build_app_menu(&app_handle)?;
            app.set_menu(menu)?;

            let bd_sidecar_path = resolve_bundled_sidecar_path(Path::new("agentshore-bd"))
                .ok()
                .filter(|path| path.is_file());
            // Show the shell before sidecar startup. On Windows, process
            // launch or handshake failures otherwise leave an invisible
            // but still-running GUI process because the window starts hidden.
            window::apply_restored_window_state(&app_handle);
            window::attach_window_persistence(&app_handle);

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

            // Phase 2 — heartbeat watchdog (#274). See `heartbeat::spawn_watchdog_thread`.
            heartbeat::spawn_watchdog_thread(app_handle.clone());

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            ui_state::load_ui_state,
            ui_state::set_ui_theme,
            ui_state::set_onboarding_completed,
            ui_state::set_last_selected_tab,
            read_text_file,
            jsonrpc_call,
            diagnostics::open_path_in_default_app,
            diagnostics::open_log_folder,
            quit::restart_sidecar,
            window::reload_ui,
            current_session,
            heartbeat::ui_heartbeat,
            quit::quit_app,
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
                    if quit::quit_requires_confirmation(session_active, already_confirmed) {
                        api.prevent_exit();
                        let app = app_handle.clone();
                        quit::prompt_quit_confirmation(app_handle, move |quit| {
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
                    quit::arm_teardown_watchdog();
                    quit::shutdown_sidecar_and_agents(app_handle);
                }
                tauri::RunEvent::Exit => {
                    quit::arm_teardown_watchdog();
                    quit::shutdown_sidecar_and_agents(app_handle);
                }
                _ => {}
            }
        });
}

#[cfg(test)]
pub fn run() {}

#[cfg(test)]
mod tests {
    use super::{read_text_file_impl, resolve_bundled_sidecar_path};

    #[test]
    fn read_text_file_round_trips_a_tmp_file() {
        let dir = std::env::temp_dir();
        let path = dir.join("agentshore-desktop-read-text-file-test.txt");
        {
            use std::io::Write;
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
