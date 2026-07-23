//! Main-window geometry: first-launch default sizing, persisted
//! position/size restore, and the window-event hooks that keep the
//! persisted state and the heartbeat/quit gates in sync.

use crate::quit::{prompt_quit_confirmation, quit_requires_confirmation, QuitConfirmed};
use crate::ui_state::{persist_ui_state, with_ui_state};
use serde::{Deserialize, Serialize};
use std::sync::atomic::Ordering;
use tauri::{AppHandle, Manager, WindowEvent};

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct WindowState {
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
}

#[cfg_attr(test, allow(dead_code))]
pub fn capture_window_state(app: &AppHandle) -> Option<WindowState> {
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
pub fn update_window_state(app: &AppHandle) {
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
pub fn reload_main_webview(app: &AppHandle) -> Result<(), String> {
    app.get_webview_window("main")
        .ok_or_else(|| "main webview window not found".to_string())?
        .reload()
        .map_err(|e| e.to_string())
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
pub fn reload_ui(app: AppHandle) -> Result<(), String> {
    reload_main_webview(&app)
}

/// First-launch window geometry: 90% of the monitor in both dimensions,
/// centered. Inputs and outputs are in **logical** units (CSS-pixel
/// equivalents). Tauri's set_size(Logical) is the only safe path on
/// HiDPI macOS; set_size(Physical) doesn't divide by scale_factor
/// before writing to NSWindow, so passing raw physical pixels produces
/// a window scale_factor× too big. Pure so it stays unit-testable.
pub fn default_window_rect(
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
pub fn apply_restored_window_state(app: &AppHandle) {
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

#[cfg_attr(test, allow(dead_code))]
pub fn attach_window_persistence(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let app_handle = app.clone();
        window.on_window_event(move |event| match event {
            WindowEvent::Moved(_) | WindowEvent::Resized(_) => {
                update_window_state(&app_handle);
            }
            // Minimizing (and simply switching to another app) fires
            // Focused(false) — Tauri has no dedicated minimize event, and this
            // is the same signal the OS/webview uses to throttle rAF, so it's
            // the correct proxy (#314). Suspend the wedge watchdog's trip
            // evaluation while unfocused; it re-arms on refocus, at which
            // point a genuinely wedged session will trip promptly since a real
            // hang means no heartbeat arrives once the user is looking again.
            WindowEvent::Focused(is_focused) => {
                app_handle
                    .state::<crate::heartbeat::WebviewHeartbeat>()
                    .unfocused
                    .store(!*is_focused, Ordering::SeqCst);
            }
            // Red close button / ⌘W. Confirm before force-killing a live
            // session; ⌘Q and the app-menu Quit are handled by ExitRequested.
            WindowEvent::CloseRequested { api, .. } => {
                let session_active = app_handle
                    .state::<crate::activity::ActivityHolder>()
                    .is_active();
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

#[cfg(test)]
mod tests {
    use super::default_window_rect;

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
}
