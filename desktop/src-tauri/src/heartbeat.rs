//! Phase 2 (#274) rAF-gated heartbeat watchdog: detects a JS-alive paint
//! wedge (compositor stall while the session is still running) and falls
//! back to a native "dashboard not responding" dialog. Distinct from the
//! macOS WKWebView content-process-terminate hook in `lib.rs`, which covers
//! true renderer-process death rather than a JS-alive stall.

#[cfg(not(test))]
use crate::window::reload_main_webview;
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
#[cfg(not(test))]
use tauri::Emitter;
use tauri::{AppHandle, Manager};

/// rAF-gated heartbeat state. `enabled` is set true by the first
/// `ui_heartbeat` call and cleared when the watchdog fires or the session ends.
/// `last_beat_ms` is a UNIX-epoch millisecond timestamp stamped each beat.
/// `esr_ready` is set true once the engine emits `$/esr_ready` (or, defensively,
/// `session.completed`) — from that point there is no more live-dashboard work
/// for the watchdog to protect, even though `ActivityHolder` can stay active for
/// up to another ~60s while backend bookkeeping (e.g. timelapse render
/// finalization) finishes. `draining` is set true once the engine emits
/// `session.draining`, fired at drain start — well before `esr_ready`, which
/// only arrives after (unbounded, O(plays)) ESR HTML generation completes.
/// `unfocused` is set true on `WindowEvent::Focused(false)` (window minimized
/// or simply not the key window) and false on `Focused(true)` — the OS/webview
/// throttles rAF whenever the window isn't visible/key, which stops the
/// renderer's heartbeat exactly as a genuine paint wedge would, so a missed
/// beat while unfocused must not be mistaken for one (#314). `esr_ready` and
/// `draining` reset false on the next `session.start`; `unfocused` tracks live
/// window state and is intentionally NOT reset there.
pub struct WebviewHeartbeat {
    pub last_beat_ms: AtomicI64,
    pub enabled: AtomicBool,
    pub esr_ready: AtomicBool,
    pub draining: AtomicBool,
    pub unfocused: AtomicBool,
}

impl Default for WebviewHeartbeat {
    fn default() -> Self {
        Self {
            last_beat_ms: AtomicI64::new(0),
            enabled: AtomicBool::new(false),
            esr_ready: AtomicBool::new(false),
            draining: AtomicBool::new(false),
            unfocused: AtomicBool::new(false),
        }
    }
}

/// Re-entrancy guard: prevents the heartbeat watchdog and the
/// content-process-terminate hook from surfacing the wedge dialog
/// simultaneously.
#[cfg_attr(test, allow(dead_code))]
#[derive(Default)]
pub struct WedgeDialogActive(pub AtomicBool);

/// Caller context for `declare_webview_wedged` — informs the log message.
#[allow(dead_code)]
pub enum WebviewWedgeMode {
    Heartbeat,
    ProcessTerminate,
}

/// Whether the heartbeat watchdog should declare a paint wedge right now. Pure
/// so the trip logic is unit-testable without a running watchdog thread.
///
/// `suppressed` covers every known reason a missed rAF beat does NOT mean a
/// real paint wedge, combined by `watchdog_suppressed` below:
///   - shutdown in progress (`session.draining` or `$/esr_ready`/
///     `session.completed`) — the whole window from drain-start onward is
///     backend bookkeeping (ESR HTML generation, timelapse render
///     finalization, etc.) with no live dashboard left to protect, and a
///     real, busy shutdown can legitimately pause the render loop for many
///     seconds during it.
///   - the window is minimized/unfocused (#314) — rAF is throttled by the
///     OS/webview whenever the window isn't visible/key, which is the same
///     JS-observable signal as a genuine paint wedge but has nothing to do
///     with one.
pub fn should_declare_wedge(
    enabled: bool,
    active: bool,
    suppressed: bool,
    last_beat_ms: i64,
    now_ms: i64,
    threshold_ms: i64,
) -> bool {
    enabled
        && active
        && !suppressed
        && last_beat_ms != 0
        && now_ms.saturating_sub(last_beat_ms) > threshold_ms
}

/// Combines every reason the watchdog should stand down regardless of
/// staleness. Pure so the combination itself is unit-testable — see
/// `should_declare_wedge` for what each reason means.
pub fn watchdog_suppressed(esr_ready: bool, draining: bool, unfocused: bool) -> bool {
    esr_ready || draining || unfocused
}

/// Whether the debounced watchdog trip is confirmed: the stale condition has
/// held for at least `confirm_threshold` consecutive polls. Pure so the
/// debounce boundary is unit-testable without the watchdog thread.
pub fn wedge_confirmed(consecutive_stale_polls: u32, confirm_threshold: u32) -> bool {
    consecutive_stale_polls >= confirm_threshold
}

/// Stamp the heartbeat timestamp and arm the watchdog (Phase 2). Called by the
/// React app on mount and every 2s via a rAF-gated interval. A missed rAF beat
/// (compositor stall / paint wedge) is what stops the call and arms the watchdog.
#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
pub fn ui_heartbeat(app: AppHandle) {
    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64;
    let beat = app.state::<WebviewHeartbeat>();
    beat.last_beat_ms.store(now_ms, Ordering::SeqCst);
    beat.enabled.store(true, Ordering::SeqCst);
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
pub fn declare_webview_wedged(app: &AppHandle, _mode: WebviewWedgeMode) {
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

/// Spawn the single long-lived watchdog thread that checks for missed rAF
/// beats (Phase 2, #274). The watchdog only trips when ALL of: enabled
/// (first beat arrived), a session is active, and now minus last_beat >
/// WEDGE_THRESHOLD_MS. On trip it shows the native fallback dialog and
/// disarms; the next `ui_heartbeat` re-arms it.
#[cfg(not(test))]
pub fn spawn_watchdog_thread(app_handle: AppHandle) {
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
            let beat = app_handle.state::<WebviewHeartbeat>();
            if !beat.enabled.load(Ordering::SeqCst) {
                // Not armed or disarmed after a reload; reset wedge flag.
                wedge_declared = false;
                consecutive_stale_polls = 0;
                continue;
            }
            let active = app_handle.state::<crate::activity::ActivityHolder>().is_active();
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
            let suppressed = watchdog_suppressed(
                beat.esr_ready.load(Ordering::SeqCst),
                beat.draining.load(Ordering::SeqCst),
                beat.unfocused.load(Ordering::SeqCst),
            );
            let stale = should_declare_wedge(true, active, suppressed, last, now_ms, WEDGE_THRESHOLD_MS);
            if stale && !wedge_declared {
                consecutive_stale_polls += 1;
                if wedge_confirmed(consecutive_stale_polls, WEDGE_CONFIRM_POLLS) {
                    wedge_declared = true;
                    declare_webview_wedged(&app_handle, WebviewWedgeMode::Heartbeat);
                }
            } else {
                consecutive_stale_polls = 0;
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::{should_declare_wedge, watchdog_suppressed, wedge_confirmed};

    #[test]
    fn should_declare_wedge_trips_when_stale_and_not_esr_ready() {
        // enabled, active, not esr_ready, beat stamped at t=0, now=11s, 10s
        // threshold → 11s elapsed > 10s → trip.
        assert!(should_declare_wedge(true, true, false, 1, 11_000, 10_000));
    }

    #[test]
    fn should_declare_wedge_suppressed_once_esr_ready() {
        // Same staleness as above, but esr_ready=true suppresses the trip —
        // this is the fix: the trailing session.completed gap (timelapse
        // finalization etc.) must not be mistaken for a paint wedge.
        assert!(!should_declare_wedge(true, true, true, 1, 11_000, 10_000));
    }

    #[test]
    fn should_declare_wedge_suppressed_when_draining() {
        // Same staleness as the esr_ready test, but the suppressed
        // param is driven by `draining` (session.draining, fired at drain
        // start) rather than esr_ready — must suppress the trip identically,
        // since this is the earlier of the two signals in the real call site.
        assert!(!should_declare_wedge(true, true, true, 1, 11_000, 10_000));
    }

    #[test]
    fn should_declare_wedge_suppressed_when_unfocused() {
        // Same staleness as the esr_ready/draining tests, but the suppressed
        // param is driven by the window being unfocused/minimized (#314) —
        // must suppress the trip identically, since a missed rAF beat while
        // the OS/webview has throttled it is not a paint wedge.
        assert!(!should_declare_wedge(true, true, true, 1, 11_000, 10_000));
    }

    #[test]
    fn watchdog_suppressed_true_when_any_reason_present() {
        assert!(watchdog_suppressed(true, false, false)); // esr_ready
        assert!(watchdog_suppressed(false, true, false)); // draining
        assert!(watchdog_suppressed(false, false, true)); // unfocused (#314)
        assert!(watchdog_suppressed(true, true, true)); // all three
    }

    #[test]
    fn watchdog_suppressed_false_when_no_reason_present() {
        assert!(!watchdog_suppressed(false, false, false));
    }

    #[test]
    fn wedge_confirmed_false_below_threshold() {
        assert!(!wedge_confirmed(0, 3));
        assert!(!wedge_confirmed(1, 3));
        assert!(!wedge_confirmed(2, 3));
    }

    #[test]
    fn wedge_confirmed_true_at_and_above_threshold() {
        assert!(wedge_confirmed(3, 3));
        assert!(wedge_confirmed(4, 3));
    }

    #[test]
    fn should_declare_wedge_false_when_disabled() {
        assert!(!should_declare_wedge(false, true, false, 1, 11_000, 10_000));
    }

    #[test]
    fn should_declare_wedge_false_when_inactive() {
        assert!(!should_declare_wedge(true, false, false, 1, 11_000, 10_000));
    }

    #[test]
    fn should_declare_wedge_false_when_no_beat_yet() {
        // last_beat_ms == 0 means "enabled but no beat stamped yet" — never trip.
        assert!(!should_declare_wedge(true, true, false, 0, 11_000, 10_000));
        assert!(!should_declare_wedge(true, true, false, 0, 0, 10_000));
    }

    #[test]
    fn should_declare_wedge_false_within_threshold() {
        // 5s elapsed against a 10s threshold, beat stamped at t=5_000.
        assert!(!should_declare_wedge(
            true, true, false, 5_000, 10_000, 10_000
        ));
    }
}
