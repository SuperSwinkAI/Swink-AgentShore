//! Tauri app entry point: state registration, the JSON-RPC bridge to the
//! Python sidecar (`jsonrpc_call`), session-facing commands, and `run()`
//! wiring. Window geometry, UI-state persistence, quit confirmation, the
//! heartbeat watchdog, the app menu, Help-menu diagnostics, and the
//! session-lifecycle side effects `jsonrpc_call` and the sidecar's
//! notification handler share each live in their own module — see `window`,
//! `ui_state`, `quit`, `heartbeat`, `menu`, `diagnostics`, `session_state`.

use serde::Serialize;
use serde_json::Value;
use std::path::PathBuf;
#[cfg(not(test))]
use std::sync::atomic::Ordering;
use std::sync::{Arc, Condvar, Mutex};
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
mod session_state;
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

fn read_text_file_impl(path: PathBuf) -> Result<String, String> {
    std::fs::read_to_string(path).map_err(|e| e.to_string())
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
fn read_text_file(path: String) -> Result<String, String> {
    read_text_file_impl(PathBuf::from(path))
}

struct SidecarHolder {
    supervisor: Arc<sidecar::SidecarSupervisor>,
}

/// Optional supervisor handle. When the supervisor failed to start
/// (handshake mismatch, spawn error, …) ``Option<SidecarHolder>`` is
/// ``None`` and the shell routes to the fatal-error screen instead of
/// dispatching JSON-RPC calls.
type SidecarHolderState = Mutex<Option<SidecarHolder>>;

/// Set once the background sidecar start (see `setup`) has finished, success
/// or failure. RPCs wait on it so the first calls after the page loads don't
/// race the handshake and report the shell as fatal.
#[derive(Default)]
struct SidecarStartup {
    done: Mutex<bool>,
    cv: Condvar,
}

impl SidecarStartup {
    fn finish(&self) {
        if let Ok(mut done) = self.done.lock() {
            *done = true;
        }
        self.cv.notify_all();
    }

    fn wait(&self) {
        if let Ok(done) = self.done.lock() {
            drop(self.cv.wait_while(done, |done| !*done));
        }
    }
}

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
    let app_for_call = app.clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        app_for_call.state::<SidecarStartup>().wait();
        with_supervisor(&app_for_call, |sup| sup.call(method, params))
    })
    .await
    .map_err(|e| format!("sidecar call task failed: {e}"))??;
    if let Ok(ref rpc_value) = result {
        if rpc_value.get("error").is_none() {
            match method_for_hook.as_str() {
                methods::SESSION_START => {
                    session_state::SessionState::on_session_started(&app, rpc_value)
                }
                methods::SESSION_STOP => session_state::SessionState::on_session_stopped(&app),
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

#[cfg(not(test))]
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default()
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
        .manage(SidecarStartup::default())
        .manage(FatalShellState::default())
        .manage(activity::ActivityHolder::new())
        .manage(QuitConfirmed::default())
        .manage(SessionInfoHolder::default())
        .manage(WebviewHeartbeat::default())
        .manage(heartbeat::WedgeDialogActive::default());

    #[cfg(target_os = "macos")]
    let builder = builder.on_web_content_process_terminate(|webview| {
        eprintln!(
            "[agentshore-desktop] WKWebView content process terminated; reloading main webview"
        );
        let _ = webview.reload();
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

            let menu = menu::build_app_menu(&app_handle)?;
            app.set_menu(menu)?;

            window::apply_restored_window_state(&app_handle);
            #[cfg(windows)]
            window::apply_taskbar_icon(&app_handle);
            window::attach_window_persistence(&app_handle);

            let startup_app = app_handle.clone();
            std::thread::spawn(move || {
                match sidecar::SidecarSupervisor::start_classified(&startup_app) {
                    Ok(supervisor) => {
                        let holder_state = startup_app.state::<SidecarHolderState>();
                        if let Ok(mut guard) = holder_state.lock() {
                            *guard = Some(SidecarHolder {
                                supervisor: Arc::new(supervisor),
                            });
                        };
                    }
                    Err(err) => {
                        let fatal = startup_app.state::<FatalShellState>();
                        if let Ok(mut guard) = fatal.info.lock() {
                            *guard = Some(err.clone());
                        }
                        let _ = startup_app.emit("app:fatal_error", err);
                    }
                }
                startup_app.state::<SidecarStartup>().finish();
            });

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
    use super::read_text_file_impl;

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
}
