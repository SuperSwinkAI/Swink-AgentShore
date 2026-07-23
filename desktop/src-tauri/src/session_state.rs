//! Centralizes the session-lifecycle side effects that `jsonrpc_call`'s
//! session.start/session.stop handling and the sidecar's session.completed
//! notification handling both need: the App Nap activity assertion,
//! `SessionInfoHolder` caching, and the heartbeat watchdog's esr_ready/
//! draining gates. The three call sites had drifted slightly before this
//! existed; `SessionState` preserves those asymmetries rather than
//! unifying them, since they may be deliberate (see the per-method docs
//! below).

use serde_json::Value;
use std::sync::atomic::Ordering;
use tauri::{AppHandle, Manager};

use crate::activity::ActivityHolder;
use crate::heartbeat::WebviewHeartbeat;
use crate::{SessionInfo, SessionInfoHolder};

/// Namespaces the session-lifecycle transition methods. Not itself
/// Tauri-managed state — each method reaches into the existing
/// `ActivityHolder` / `SessionInfoHolder` / `WebviewHeartbeat` managed
/// states via `app.state()`.
pub struct SessionState;

impl SessionState {
    /// Called from `jsonrpc_call` on a successful `session.start` response.
    /// Acquires the App Nap activity assertion, re-arms the heartbeat
    /// watchdog's esr_ready/draining gates for the new session (#274
    /// follow-up — a prior session's disarm must not suppress wedge
    /// detection for this one), and caches dashboard_url/session_id for
    /// `current_session()` / reattach (#274).
    pub fn on_session_started(app: &AppHandle, rpc_value: &Value) {
        app.state::<ActivityHolder>()
            .acquire("AgentShore session active");

        let heartbeat = app.state::<WebviewHeartbeat>();
        heartbeat.esr_ready.store(false, Ordering::SeqCst);
        heartbeat.draining.store(false, Ordering::SeqCst);

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

    /// Called from `jsonrpc_call` on a successful `session.stop` response.
    /// Releases the activity assertion and clears the cached session info.
    /// Deliberately does NOT touch the heartbeat esr_ready/draining gates —
    /// unlike `on_session_completed`, the caller here already knows the
    /// session ended via the RPC return, so there's no trailing
    /// "session.completed never fires" gap to guard against.
    pub fn on_session_stopped(app: &AppHandle) {
        app.state::<ActivityHolder>().release();
        if let Ok(mut guard) = app.state::<SessionInfoHolder>().0.lock() {
            *guard = None;
        }
    }

    /// Called from the sidecar notification handler on `session.completed` —
    /// the fallback for exits where `session.stop` never fires
    /// (drain_complete, max_plays, timeout, shutting_down). Releases the
    /// activity assertion, clears cached session info (#274 reattach), and
    /// disarms the heartbeat watchdog. Unlike `on_session_started`'s reset,
    /// this defensively force-sets `esr_ready = true` (rather than false) —
    /// covers any shutdown path that reaches session.completed without a
    /// preceding `$/esr_ready` (e.g. non-embedded mode) so the watchdog can't
    /// mistake trailing shutdown bookkeeping for a paint wedge.
    pub fn on_session_completed(app: &AppHandle) {
        app.state::<ActivityHolder>().release();
        if let Ok(mut guard) = app.state::<SessionInfoHolder>().0.lock() {
            *guard = None;
        }
        let heartbeat = app.state::<WebviewHeartbeat>();
        heartbeat.enabled.store(false, Ordering::Relaxed);
        heartbeat.esr_ready.store(true, Ordering::Relaxed);
    }
}
