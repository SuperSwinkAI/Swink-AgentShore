//! Quit-confirmation gate, app-restart, and process teardown.
//!
//! A running session must never be force-killed silently: the
//! `QuitConfirmed` latch plus `quit_requires_confirmation` gate every quit
//! path (red close button, ⌘Q/app-menu Quit, in-app Quit buttons) behind one
//! confirmation dialog, and `shutdown_sidecar_and_agents` / the teardown
//! watchdog guarantee the process still exits even if that teardown wedges.

use crate::SidecarHolderState;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tauri::{AppHandle, Manager};

/// One-shot latch: set once the user has approved a quit while a session was
/// running, so the re-entrant close/exit that follows the confirmation (window
/// `destroy()` → `ExitRequested`, or `app.exit()`) proceeds without re-prompting.
/// Also set by the explicit in-app Quit buttons (recovery / fatal-error
/// screens), which are themselves a deliberate quit and shouldn't double-prompt.
#[derive(Default)]
pub struct QuitConfirmed(AtomicBool);

impl QuitConfirmed {
    pub fn get(&self) -> bool {
        self.0.load(Ordering::SeqCst)
    }

    pub fn set(&self) {
        self.0.store(true, Ordering::SeqCst);
    }
}

/// Whether quitting now needs the running-session confirmation prompt: a
/// session is live and the user hasn't already approved this quit. Pure so the
/// gate logic is unit-testable without a running Tauri app.
pub fn quit_requires_confirmation(session_active: bool, already_confirmed: bool) -> bool {
    session_active && !already_confirmed
}

/// Show the async "a session is still running" confirmation. Non-blocking:
/// the caller has already prevented the close/exit, and `on_choice(true)` is
/// invoked on the "Quit" button (false on "Cancel"). Async (not `blocking_show`)
/// because this runs on the main thread, where a blocking dialog would deadlock
/// the event loop the dialog itself needs to pump.
#[cfg_attr(test, allow(dead_code))]
pub fn prompt_quit_confirmation<F: FnOnce(bool) + Send + 'static>(app: &AppHandle, on_choice: F) {
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

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
pub fn quit_app(app: AppHandle) -> Result<(), String> {
    // Explicit in-app Quit (recovery / fatal-error screens): the user already
    // chose to quit, so latch confirmation to skip the native prompt.
    app.state::<QuitConfirmed>().set();
    app.exit(0);
    Ok(())
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
pub fn restart_sidecar(app: AppHandle) -> Result<(), String> {
    // Full app restart — the simplest way to reliably re-spawn the sidecar and
    // re-run the handshake (in-place child respawn is out of scope).
    app.restart()
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
pub fn arm_teardown_watchdog() {
    std::thread::spawn(|| {
        std::thread::sleep(TEARDOWN_WATCHDOG_DEADLINE);
        eprintln!(
            "[agentshore-desktop] teardown exceeded {TEARDOWN_WATCHDOG_DEADLINE:?}; hard-exiting"
        );
        std::process::exit(0);
    });
}

#[cfg_attr(test, allow(dead_code))]
pub fn shutdown_sidecar_and_agents(app_handle: &AppHandle) {
    // Take the supervisor OUT of the holder under a short-lived lock, then do
    // all the killing outside it. In-flight RPC threads may still hold Arc
    // clones of the supervisor, so its Drop impl is NOT guaranteed to run
    // here — kill_sidecar() is the explicit teardown and Drop is only the
    // backstop (#155).
    let supervisor: Option<Arc<crate::sidecar::SidecarSupervisor>> = {
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
    let activity_state: tauri::State<'_, crate::activity::ActivityHolder> = app_handle.state();
    activity_state.release();
}

#[cfg(test)]
mod tests {
    use super::{quit_requires_confirmation, QuitConfirmed};

    #[test]
    fn quit_requires_confirmation_only_when_session_active_and_unconfirmed() {
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
        let guard = QuitConfirmed::default();
        assert!(!guard.get());
        guard.set();
        assert!(guard.get());
        // Idempotent.
        guard.set();
        assert!(guard.get());
    }
}
