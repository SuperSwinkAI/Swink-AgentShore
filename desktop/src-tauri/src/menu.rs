//! App menu construction and the menu-event dispatch that fans custom items
//! out to React (via `menu:<id>` Tauri events) or handles them inline in Rust
//! (Help URL items, Copy Diagnostics, Reload UI).

#[cfg(not(test))]
use crate::diagnostics::{collect_diagnostics, spawn_open};
#[cfg(not(test))]
use crate::window::reload_main_webview;
#[cfg(not(test))]
use tauri::menu::{MenuBuilder, MenuItemBuilder, PredefinedMenuItem, SubmenuBuilder};
#[cfg(not(test))]
use tauri::{AppHandle, Emitter, Runtime};

// External Help-menu destinations, opened in the default browser via the OS
// opener (see [`crate::diagnostics::spawn_open`]). The repo is the canonical
// source for docs, release notes (the same tag the updater reads), and issue
// intake.
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
pub fn build_app_menu<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<tauri::menu::Menu<R>> {
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

/// Dispatch a menu-item click. Session-scoped and app-global custom items fan
/// out to React via `menu:<id>` events; the three Help URL items, Copy
/// Diagnostics, and Reload UI are handled inline here since they need no
/// React round-trip (Reload UI must also work while the WebView is white).
#[cfg(not(test))]
pub fn handle_menu_event(app: &AppHandle, event: tauri::menu::MenuEvent) {
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
}
