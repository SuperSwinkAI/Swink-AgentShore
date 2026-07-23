//! Sidecar supervisor: spawns and supervises the Python sidecar subprocess,
//! split into four sibling concerns:
//! - [`transport`] — process spawning (env, PATH overlay), the stderr
//!   collector thread, and process-tree teardown.
//! - [`rpc`] — JSON-RPC request/response plumbing (the pending-response map,
//!   per-method timeout classification, and the stdout dispatcher that
//!   routes each line to a response or a notification).
//! - [`agents`] — the tracked-agent-subprocess PID map and "kill all agents"
//!   recovery surface (DESIGN §1.4).
//! - [`crash_watch`] — the background thread that detects an unexpected
//!   sidecar exit and emits `sidecar:crashed`.
//!
//! This module owns the [`SidecarSupervisor`] struct itself (its fields span
//! all four concerns), construction (`start`/`start_classified`, which wires
//! the concerns together), and teardown (`Drop`). Each submodule contributes
//! its own `impl SidecarSupervisor` block for the methods it owns.

mod agents;
mod crash_watch;
mod rpc;
mod transport;

pub use agents::TrackedAgent;
pub use crash_watch::SidecarCrashPayload;
pub use rpc::{SidecarNotificationPayload, SESSION_COMPLETED_EVENT, SIDECAR_NOTIFICATION_EVENT};

use crate::jsonrpc_stdio::{handshake_request, JsonRpcResponse};
use crate::sidecar_pid::write_sidecar_pid_file;
use serde::Serialize;
use serde_json::Value;
use std::collections::{HashMap, VecDeque};
use std::process::{Child, ChildStdin};
use std::sync::atomic::{AtomicBool, AtomicI64};
use std::sync::{mpsc, Arc, Mutex};
use std::time::Duration;
use tauri::AppHandle;

const CLIENT_NAME: &str = "agentshore-desktop";

/// Env override for the handshake budget (seconds). Lets support widen it on a
/// pathologically slow box without a rebuild.
const HANDSHAKE_TIMEOUT_ENV: &str = "AGENTSHORE_HANDSHAKE_TIMEOUT_SECS";

/// Environment variable that lets a packaging script inject the
/// runtime build_id (hash of git SHA + build timestamp per DESIGN
/// §2.6). When unset (the dev case), the shell falls back to ``"dev"``
/// so it matches the Python sidecar's unfrozen ``build_id.py``
/// fallback. Tauri's bundler can set this via its ``env`` config or
/// the desktop CI workflow can set it at build time.
const BUILD_ID_ENV: &str = "AGENTSHORE_DESKTOP_BUILD_ID";
const DEV_BUILD_ID: &str = "dev";

/// Resolve the build_id the desktop shell announces during
/// ``app.handshake``. Reads ``AGENTSHORE_DESKTOP_BUILD_ID`` first so a
/// packaged build can override it; otherwise falls back to ``"dev"``
/// so unfrozen development handshakes against the unfrozen Python
/// sidecar (which also resolves to ``"dev"`` without ``_MEIPASS``)
/// succeed.
pub fn resolve_build_id() -> String {
    match std::env::var(BUILD_ID_ENV) {
        Ok(value) if !value.trim().is_empty() => value.trim().to_string(),
        _ => DEV_BUILD_ID.to_string(),
    }
}

/// Budget for the initial ``app.handshake`` round-trip.
///
/// A Windows cold start under antivirus (Defender/Avast scanning ``python.exe``
/// and the first ``import agentshore.sidecar``) routinely exceeds a 30s wall and
/// would otherwise trip the supervisor into the fatal-error screen even though
/// the sidecar is alive and about to answer. Give Windows a wider budget; other
/// platforms keep the original 30s. Overridable via
/// ``AGENTSHORE_HANDSHAKE_TIMEOUT_SECS`` for field diagnosis.
fn handshake_response_timeout() -> Duration {
    if let Ok(raw) = std::env::var(HANDSHAKE_TIMEOUT_ENV) {
        if let Ok(secs) = raw.trim().parse::<u64>() {
            if secs > 0 {
                return Duration::from_secs(secs);
            }
        }
    }
    if cfg!(target_os = "windows") {
        Duration::from_secs(90)
    } else {
        Duration::from_secs(30)
    }
}

/// Structured supervisor-startup failure (DESIGN §2.6 fatal-error
/// surface). The shell uses these variants to route the user to the
/// fatal-error screen with the right diagnostic copy. ``BuildIdMismatch``
/// is the named case from gh-337; any other handshake / spawn failure
/// falls into ``Other``.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SupervisorStartError {
    BuildIdMismatch { expected: String, received: String },
    Other { reason: String },
}

impl SupervisorStartError {
    pub fn message(&self) -> String {
        match self {
            SupervisorStartError::BuildIdMismatch { expected, received } => {
                format!("sidecar build mismatch: expected {expected}, got {received}")
            }
            SupervisorStartError::Other { reason } => reason.clone(),
        }
    }
}

pub struct SidecarSupervisor {
    /// stdin is the only handle the supervisor uses to talk *to* the
    /// sidecar after start; the dispatcher thread owns stdout.
    stdin: Arc<Mutex<ChildStdin>>,
    child: Arc<Mutex<Option<Child>>>,
    next_id: AtomicI64,
    /// Pending request channels, keyed by JSON-RPC id. The stdout
    /// dispatcher delivers the parsed response on the channel whose id
    /// matches; ``send_request`` registers and removes its own entry.
    pending: Arc<Mutex<HashMap<i64, mpsc::Sender<JsonRpcResponse>>>>,
    stderr_lines: Arc<Mutex<VecDeque<String>>>,
    log_file_path: Option<String>,
    crashed_emitted: Arc<AtomicBool>,
    /// Tracked agent subprocess PIDs reported by the sidecar via
    /// ``agent.subprocess_spawned`` / ``agent.subprocess_exited``
    /// notifications. Read by the recovery screen "Kill all" button.
    agent_pids: Arc<Mutex<HashMap<String, TrackedAgent>>>,
}

impl SidecarSupervisor {
    /// Wraps :meth:`start_classified` and flattens the error to a string
    /// for existing callers / cargo-tests that don't need the variant
    /// discrimination.
    pub fn start(
        app: &AppHandle,
        bd_sidecar_path: Option<&std::path::Path>,
    ) -> Result<Self, String> {
        Self::start_classified(app, bd_sidecar_path).map_err(|e| e.message())
    }

    /// DESIGN §2.6 — classified supervisor startup. Returns a structured
    /// error variant so the shell can distinguish build_id mismatch (the
    /// named fatal case from gh-337) from generic spawn / handshake
    /// failures.
    pub fn start_classified(
        app: &AppHandle,
        bd_sidecar_path: Option<&std::path::Path>,
    ) -> Result<Self, SupervisorStartError> {
        let mut cmd = transport::sidecar_command(bd_sidecar_path);
        let mut child = cmd.spawn().map_err(|e| SupervisorStartError::Other {
            reason: format!("spawn sidecar: {e}"),
        })?;
        write_sidecar_pid_file(child.id());

        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| SupervisorStartError::Other {
                reason: "sidecar stdin unavailable".to_string(),
            })?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| SupervisorStartError::Other {
                reason: "sidecar stdout unavailable".to_string(),
            })?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| SupervisorStartError::Other {
                reason: "sidecar stderr unavailable".to_string(),
            })?;

        let stderr_lines = Arc::new(Mutex::new(VecDeque::with_capacity(
            transport::MAX_STDERR_LINES,
        )));
        transport::spawn_stderr_collector(stderr, Arc::clone(&stderr_lines));

        let pending: Arc<Mutex<HashMap<i64, mpsc::Sender<JsonRpcResponse>>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let agent_pids: Arc<Mutex<HashMap<String, TrackedAgent>>> =
            Arc::new(Mutex::new(HashMap::new()));
        rpc::spawn_stdout_dispatcher(
            stdout,
            Arc::clone(&pending),
            Arc::clone(&agent_pids),
            app.clone(),
        );

        let supervisor = Self {
            stdin: Arc::new(Mutex::new(stdin)),
            child: Arc::new(Mutex::new(Some(child))),
            next_id: AtomicI64::new(2),
            pending,
            stderr_lines,
            log_file_path: None,
            crashed_emitted: Arc::new(AtomicBool::new(false)),
            agent_pids,
        };

        let build_id = resolve_build_id();
        let handshake = handshake_request(1, CLIENT_NAME, &build_id);
        let response = supervisor
            .send_request(&handshake, handshake_response_timeout())
            .map_err(|reason| SupervisorStartError::Other { reason })?;

        if let Some(err) = response.error {
            return Err(SupervisorStartError::Other {
                reason: format!("handshake failed: {} ({})", err.message, err.code),
            });
        }

        let result = response.result.ok_or_else(|| SupervisorStartError::Other {
            reason: "handshake missing result".to_string(),
        })?;
        let sidecar_build_id = result
            .get("sidecar_build_id")
            .and_then(Value::as_str)
            .ok_or_else(|| SupervisorStartError::Other {
                reason: "handshake missing sidecar_build_id".to_string(),
            })?;
        if sidecar_build_id != build_id {
            return Err(SupervisorStartError::BuildIdMismatch {
                expected: build_id,
                received: sidecar_build_id.to_string(),
            });
        }

        supervisor.start_crash_watcher(app.clone());
        Ok(supervisor)
    }
}

impl Drop for SidecarSupervisor {
    fn drop(&mut self) {
        self.kill_sidecar();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::jsonrpc_stdio::{decode_response_line, encode_line, JsonRpcRequest};
    use crate::sidecar_env::apply_no_window_creation_flags;
    use crate::sidecar_runtime::development_sidecar_command;
    use std::io::{BufRead, BufReader, Write};
    use std::process::Stdio;

    #[test]
    fn sidecar_round_trip_handshake_and_recents_list() {
        let mut cmd = development_sidecar_command();
        cmd.stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        cmd.env("PYTHONDONTWRITEBYTECODE", "1");
        apply_no_window_creation_flags(&mut cmd);
        let mut child = cmd.spawn().expect("spawn sidecar");
        let mut stdin = child.stdin.take().expect("stdin");
        let stdout = child.stdout.take().expect("stdout");
        let mut stdout = BufReader::new(stdout);

        let handshake = handshake_request(1, "agentshore-desktop", "dev");
        let line = encode_line(&handshake).expect("encode handshake");
        stdin.write_all(line.as_bytes()).expect("send handshake");
        stdin.flush().expect("flush handshake");

        let mut response = String::new();
        stdout
            .read_line(&mut response)
            .expect("read handshake response");
        let parsed = decode_response_line(&response).expect("decode handshake");
        assert!(parsed.error.is_none());

        let req = JsonRpcRequest {
            jsonrpc: "2.0".to_string(),
            id: Value::from(2),
            method: "recents.list".to_string(),
            params: None,
        };
        let line = encode_line(&req).expect("encode list request");
        stdin.write_all(line.as_bytes()).expect("send list request");
        stdin.flush().expect("flush list request");

        response.clear();
        stdout.read_line(&mut response).expect("read list response");
        let parsed = decode_response_line(&response).expect("decode list");
        assert!(parsed.error.is_none());
        let result = parsed.result.expect("list result");
        assert!(result.is_array(), "recents.list should return an array");

        let _ = child.kill();
        let _ = child.wait();
    }

    /// All three resolve_build_id paths run in one test because cargo
    /// runs ``#[test]``s in parallel by default and the env var is
    /// process-global; three separate tests would race on the same
    /// ``BUILD_ID_ENV`` value.
    #[test]
    fn resolve_build_id_covers_all_paths() {
        std::env::remove_var(BUILD_ID_ENV);
        assert_eq!(resolve_build_id(), DEV_BUILD_ID, "unset env → dev fallback");

        std::env::set_var(BUILD_ID_ENV, "1.2.3-abcd1234");
        assert_eq!(
            resolve_build_id(),
            "1.2.3-abcd1234",
            "non-empty env override applied verbatim"
        );

        std::env::set_var(BUILD_ID_ENV, "   ");
        assert_eq!(
            resolve_build_id(),
            DEV_BUILD_ID,
            "whitespace-only env → dev fallback"
        );

        std::env::remove_var(BUILD_ID_ENV);
    }

    #[test]
    fn handshake_response_timeout_covers_env_and_default() {
        // Env override wins.
        std::env::set_var(HANDSHAKE_TIMEOUT_ENV, "7");
        assert_eq!(handshake_response_timeout(), Duration::from_secs(7));
        // Non-positive / non-numeric override falls back to the platform default.
        std::env::set_var(HANDSHAKE_TIMEOUT_ENV, "  0 ");
        let default_secs = if cfg!(target_os = "windows") { 90 } else { 30 };
        assert_eq!(
            handshake_response_timeout(),
            Duration::from_secs(default_secs)
        );
        std::env::remove_var(HANDSHAKE_TIMEOUT_ENV);
        assert_eq!(
            handshake_response_timeout(),
            Duration::from_secs(default_secs)
        );
    }

    #[test]
    fn supervisor_start_error_message_describes_build_id_mismatch() {
        let err = SupervisorStartError::BuildIdMismatch {
            expected: "abc123".to_string(),
            received: "def456".to_string(),
        };
        let msg = err.message();
        assert!(
            msg.contains("abc123"),
            "expected build_id in message: {msg}"
        );
        assert!(
            msg.contains("def456"),
            "received build_id in message: {msg}"
        );
        assert!(msg.contains("mismatch"), "category in message: {msg}");
    }

    #[test]
    fn supervisor_start_error_message_passes_through_other_reason() {
        let err = SupervisorStartError::Other {
            reason: "spawn sidecar: file not found".to_string(),
        };
        assert_eq!(err.message(), "spawn sidecar: file not found");
    }

    #[test]
    fn supervisor_start_error_serializes_with_tag_kind() {
        let err = SupervisorStartError::BuildIdMismatch {
            expected: "abc".to_string(),
            received: "def".to_string(),
        };
        let json = serde_json::to_value(&err).expect("serialize");
        assert_eq!(json["kind"], "build_id_mismatch");
        assert_eq!(json["expected"], "abc");
        assert_eq!(json["received"], "def");
    }
}
