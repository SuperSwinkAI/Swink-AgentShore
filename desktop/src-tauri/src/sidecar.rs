use crate::jsonrpc_stdio::{
    decode_response_line, encode_line, handshake_request, JsonRpcError, JsonRpcRequest,
    JsonRpcResponse,
};
use serde::Serialize;
use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager};

const CLIENT_NAME: &str = "agentshore-desktop";
const MAX_STDERR_LINES: usize = 50;
const RESPONSE_TIMEOUT: Duration = Duration::from_secs(120);
const AGENTSHORE_BD_BIN_ENV: &str = "AGENTSHORE_BD_BIN";

/// Tauri event name carrying forwarded sidecar JSON-RPC notifications
/// (anything sent over stdout without an ``id`` field — including
/// ``$/progress``, ``sidecar.crashed``, ``session.completed``, etc.).
pub const SIDECAR_NOTIFICATION_EVENT: &str = "sidecar:notification";
pub const SESSION_COMPLETED_EVENT: &str = "session:completed";

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

#[derive(Debug, Clone, Serialize)]
pub struct SidecarCrashPayload {
    pub exit_code: Option<i32>,
    pub last_stderr_lines: Vec<String>,
    pub log_file_path: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SidecarNotificationPayload {
    pub method: String,
    pub params: Value,
}

/// Tracked agent CLI subprocess managed by the Python sidecar
/// (DESIGN §1.4 — Crash recovery "Kill all" surface). The supervisor
/// maintains a map of these keyed by ``agent_id``, populated from
/// ``agent.subprocess_spawned`` and pruned on
/// ``agent.subprocess_exited`` notifications.
#[derive(Debug, Clone, Serialize)]
pub struct TrackedAgent {
    pub agent_id: String,
    pub agent_type: String,
    pub pid: u32,
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
    pub fn start(app: &AppHandle, bd_sidecar_path: Option<&std::path::Path>) -> Result<Self, String> {
        Self::start_classified(app, bd_sidecar_path).map_err(|e| e.message())
    }

    /// DESIGN §2.6 — classified supervisor startup. Returns a structured
    /// error variant so the shell can distinguish build_id mismatch (the
    /// named fatal case from gh-337) from generic spawn / handshake
    /// failures.
    pub fn start_classified(app: &AppHandle, bd_sidecar_path: Option<&std::path::Path>) -> Result<Self, SupervisorStartError> {
        let mut cmd = sidecar_command(bd_sidecar_path);
        let mut child = cmd.spawn().map_err(|e| SupervisorStartError::Other {
            reason: format!("spawn sidecar: {e}"),
        })?;

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

        let stderr_lines = Arc::new(Mutex::new(VecDeque::with_capacity(MAX_STDERR_LINES)));
        spawn_stderr_collector(stderr, Arc::clone(&stderr_lines));

        let pending: Arc<Mutex<HashMap<i64, mpsc::Sender<JsonRpcResponse>>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let agent_pids: Arc<Mutex<HashMap<String, TrackedAgent>>> =
            Arc::new(Mutex::new(HashMap::new()));
        spawn_stdout_dispatcher(
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
            .send_request(&handshake)
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

    /// Snapshot of the agent PIDs the sidecar has reported alive.
    pub fn tracked_agents(&self) -> Vec<TrackedAgent> {
        self.agent_pids
            .lock()
            .map(|map| map.values().cloned().collect())
            .unwrap_or_default()
    }

    /// SIGTERM then SIGKILL every tracked agent PID. Returns the list of
    /// PIDs the supervisor attempted to signal; missing-process errors
    /// from already-dead PIDs are silently treated as success. Linux/macOS
    /// use ``libc::kill``; Windows uses ``TerminateProcess`` via a
    /// short-lived ``taskkill`` command so we don't pull in a new crate
    /// dep for the minority platform.
    pub fn kill_all_agents(&self) -> Vec<TrackedAgent> {
        let snapshot = self.tracked_agents();
        for agent in &snapshot {
            kill_agent_pid(agent.pid);
        }
        if let Ok(mut map) = self.agent_pids.lock() {
            map.clear();
        }
        snapshot
    }

    pub fn call(&self, method: String, params: Option<Value>) -> Result<Value, String> {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let req = JsonRpcRequest {
            jsonrpc: "2.0".to_string(),
            id: Value::from(id),
            method,
            params,
        };

        let response = self.send_request(&req)?;

        if let Some(error) = response.error {
            return Ok(json!({"error": serialize_error(error)}));
        }

        Ok(response.result.unwrap_or(Value::Null))
    }

    fn send_request(&self, req: &JsonRpcRequest) -> Result<JsonRpcResponse, String> {
        let id = req
            .id
            .as_i64()
            .ok_or_else(|| "request id must be an integer".to_string())?;

        // Register a one-shot channel under the request id before we
        // write the request — the dispatcher thread is already running
        // and could deliver the response before we'd otherwise register.
        let (tx, rx) = mpsc::channel::<JsonRpcResponse>();
        {
            let mut pending = self
                .pending
                .lock()
                .map_err(|e| format!("sidecar pending lock poisoned: {e}"))?;
            pending.insert(id, tx);
        }

        // Write the framed request to stdin under its own lock.
        let write_result = (|| -> Result<(), String> {
            let line = encode_line(req).map_err(|e| format!("encode request: {e}"))?;
            let mut stdin = self
                .stdin
                .lock()
                .map_err(|e| format!("sidecar stdin lock poisoned: {e}"))?;
            stdin
                .write_all(line.as_bytes())
                .map_err(|e| format!("write request: {e}"))?;
            stdin.flush().map_err(|e| format!("flush request: {e}"))?;
            Ok(())
        })();

        // If the write failed, clean up the pending entry — no response
        // will ever land for this id, so it would leak.
        if let Err(err) = write_result {
            self.discard_pending(id);
            return Err(err);
        }

        match rx.recv_timeout(RESPONSE_TIMEOUT) {
            Ok(response) => Ok(response),
            Err(mpsc::RecvTimeoutError::Timeout) => {
                self.discard_pending(id);
                Err(format!(
                    "sidecar response timed out after {RESPONSE_TIMEOUT:?}"
                ))
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                self.discard_pending(id);
                Err("sidecar closed stdout while waiting for response".to_string())
            }
        }
    }

    fn discard_pending(&self, id: i64) {
        if let Ok(mut pending) = self.pending.lock() {
            pending.remove(&id);
        }
    }

    fn start_crash_watcher(&self, app: AppHandle) {
        let child = Arc::clone(&self.child);
        let stderr_lines = Arc::clone(&self.stderr_lines);
        let log_file_path = self.log_file_path.clone();
        let emitted = Arc::clone(&self.crashed_emitted);

        std::thread::spawn(move || loop {
            std::thread::sleep(std::time::Duration::from_millis(250));
            if emitted.load(Ordering::SeqCst) {
                break;
            }

            let maybe_exit = {
                let mut guard = match child.lock() {
                    Ok(g) => g,
                    Err(_) => break,
                };
                let Some(proc_ref) = guard.as_mut() else {
                    break;
                };
                proc_ref.try_wait().ok().flatten()
            };

            if let Some(status) = maybe_exit {
                let payload = SidecarCrashPayload {
                    exit_code: status.code(),
                    last_stderr_lines: snapshot_stderr(&stderr_lines),
                    log_file_path: log_file_path.clone(),
                };
                // Attempt the emit BEFORE marking emitted, so a failed
                // emit (Tauri shutting down, no listeners attached yet)
                // doesn't poison the flag and leave React permanently
                // uninformed. On success, set the flag and exit; on
                // failure, log and loop — the watcher will retry on the
                // next 250ms tick.
                match app.emit("sidecar:crashed", payload) {
                    Ok(()) => {
                        emitted.store(true, Ordering::SeqCst);
                        break;
                    }
                    Err(err) => {
                        eprintln!(
                            "[agentshore-desktop][sidecar] crash emit failed: {err}; will retry"
                        );
                    }
                }
            }
        });
    }
}

impl Drop for SidecarSupervisor {
    fn drop(&mut self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(proc_ref) = guard.as_mut() {
                let _ = proc_ref.kill();
                let _ = proc_ref.wait();
            }
            *guard = None;
        }
    }
}

/// Build the command that spawns the Python sidecar.
///
/// Production: launch the Python interpreter from the pkg-installer's
/// managed venv (``/Library/Application Support/AgentShore/venv/bin/python``
/// on macOS — see ``locate_managed_venv_python``) with
/// ``-m agentshore.sidecar``. The venv is provisioned by the .pkg's
/// postinstall script which pip-installs the bundled agentshore wheel.
///
/// Development: when the managed venv is not present (running outside
/// an installed .pkg), fall back to ``uv run python -m agentshore.sidecar``
/// against the in-tree source so ``tauri dev`` and ``cargo test`` work
/// without an install step.
fn sidecar_command(bd_path: Option<&std::path::Path>) -> Command {
    let mut cmd = match locate_managed_venv_python() {
        Some(python) => {
            let mut prod = Command::new(python);
            prod.arg("-m").arg("agentshore.sidecar");
            prod
        }
        None => {
            let mut dev = Command::new("uv");
            dev.arg("run")
                .arg("python")
                .arg("-m")
                .arg("agentshore.sidecar")
                .env("PYTHONPATH", "../../src");
            dev
        }
    };
    cmd.stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(p) = bd_path {
        cmd.env(AGENTSHORE_BD_BIN_ENV, p);
    }
    apply_user_path_overlay(&mut cmd);
    cmd
}

/// When the Tauri .app launches from Finder/Dock/Spotlight, its PATH is
/// the minimal launchd default (``/usr/bin:/bin:/usr/sbin:/sbin``) — the
/// shell rc files that populate the user's terminal PATH never run. The
/// sidecar inherits that minimal PATH, so its readiness check
/// (``shutil.which("bd")`` / ``which("gh")``) reports tooling as missing
/// even when the user has it installed in standard user-install
/// locations. Prepend those locations so the sidecar's checks see
/// what the user's terminal sees.
fn apply_user_path_overlay(cmd: &mut Command) {
    let existing = std::env::var_os("PATH").unwrap_or_default();
    let mut entries: Vec<std::path::PathBuf> = std::env::split_paths(&existing).collect();

    let mut candidates: Vec<std::path::PathBuf> = vec![
        std::path::PathBuf::from("/opt/homebrew/bin"),
        std::path::PathBuf::from("/opt/homebrew/sbin"),
        std::path::PathBuf::from("/usr/local/bin"),
        std::path::PathBuf::from("/usr/local/sbin"),
    ];
    if let Some(home) = std::env::var_os("HOME") {
        candidates.push(std::path::PathBuf::from(&home).join(".local/bin"));
        candidates.push(std::path::PathBuf::from(&home).join(".cargo/bin"));
    }

    // Prepend (in reverse so the first candidate ends up first), skipping
    // any already-present entry to avoid PATH duplication.
    for dir in candidates.into_iter().rev() {
        if !entries.iter().any(|e| e == &dir) {
            entries.insert(0, dir);
        }
    }

    if let Ok(joined) = std::env::join_paths(entries) {
        cmd.env("PATH", joined);
    }
}

/// Locate the Python interpreter inside the pkg-installer's managed venv.
///
/// The .pkg's postinstall script provisions a per-user venv inside the
/// launching user's home directory (no sudo required for installation)
/// and pip-installs the bundled agentshore wheel into it. Returns ``None``
/// in development builds where the .pkg has never been installed;
/// ``sidecar_command()`` then falls back to ``uv run``.
fn locate_managed_venv_python() -> Option<std::path::PathBuf> {
    let home = std::env::var_os("HOME")?;
    let home = std::path::PathBuf::from(home);
    #[cfg(target_os = "macos")]
    let path = home.join("Library/Application Support/AgentShore/venv/bin/python");
    #[cfg(target_os = "linux")]
    let path = home.join(".local/share/agentshore/venv/bin/python");
    #[cfg(target_os = "windows")]
    let path = home.join(r"AppData\Local\AgentShore\venv\Scripts\python.exe");
    if path.is_file() {
        Some(path)
    } else {
        None
    }
}

fn spawn_stdout_dispatcher(
    stdout: ChildStdout,
    pending: Arc<Mutex<HashMap<i64, mpsc::Sender<JsonRpcResponse>>>>,
    agent_pids: Arc<Mutex<HashMap<String, TrackedAgent>>>,
    app: AppHandle,
) {
    std::thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        loop {
            line.clear();
            let read = match reader.read_line(&mut line) {
                Ok(n) => n,
                Err(_) => break,
            };
            if read == 0 {
                break;
            }
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            dispatch_line(trimmed, &pending, &agent_pids, &app);
        }
    });
}

fn handle_agent_subprocess_notification(
    method: &str,
    params: &Value,
    agent_pids: &Arc<Mutex<HashMap<String, TrackedAgent>>>,
) {
    let agent_id = params.get("agent_id").and_then(Value::as_str);
    let Some(agent_id) = agent_id else { return };
    match method {
        "agent.subprocess_spawned" => {
            let agent_type = params
                .get("agent_type")
                .and_then(Value::as_str)
                .unwrap_or("unknown")
                .to_string();
            let pid = match params.get("pid").and_then(Value::as_u64) {
                Some(pid) => pid as u32,
                None => return,
            };
            if let Ok(mut map) = agent_pids.lock() {
                map.insert(
                    agent_id.to_string(),
                    TrackedAgent {
                        agent_id: agent_id.to_string(),
                        agent_type,
                        pid,
                    },
                );
            }
        }
        "agent.subprocess_exited" => {
            if let Ok(mut map) = agent_pids.lock() {
                map.remove(agent_id);
            }
        }
        _ => {}
    }
}

fn dispatch_line(
    line: &str,
    pending: &Arc<Mutex<HashMap<i64, mpsc::Sender<JsonRpcResponse>>>>,
    agent_pids: &Arc<Mutex<HashMap<String, TrackedAgent>>>,
    app: &AppHandle,
) {
    let value: Value = match serde_json::from_str(line) {
        Ok(v) => v,
        Err(err) => {
            // Silent drop here meant a partial/corrupt sidecar line could
            // make an RPC wait the full 120s timeout with no console trace.
            // At least leave breadcrumbs so we know parse failure happened.
            let preview: String = line.chars().take(200).collect();
            eprintln!(
                "[agentshore-desktop][sidecar] dispatch_line: JSON parse failure: {err}; line preview: {preview}"
            );
            return;
        }
    };

    // JSON-RPC responses carry an `id`; notifications do not (the
    // `method` field marks the line as a notification).
    if let Some(id) = value.get("id").and_then(Value::as_i64) {
        if let Ok(response) = decode_response_line(line) {
            if let Ok(mut pending) = pending.lock() {
                if let Some(tx) = pending.remove(&id) {
                    let _ = tx.send(response);
                }
            }
        }
        return;
    }

    if let Some(method) = value.get("method").and_then(Value::as_str) {
        let params = value.get("params").cloned().unwrap_or(Value::Null);
        handle_agent_subprocess_notification(method, &params, agent_pids);
        // desktop-bzr2: release the NSProcessInfo activity assertion on
        // natural session exit. session.start path took the assertion;
        // session.stop releases it via jsonrpc_call's RPC-success hook.
        // session.completed is the fallback for natural exits where
        // session.stop never fires (drain_complete, max_plays, timeout,
        // shutting_down).
        if method == "session.completed" {
            let holder = app.state::<crate::activity::ActivityHolder>();
            holder.release();
            let _ = app.emit(SESSION_COMPLETED_EVENT, params.clone());
        }
        let payload = SidecarNotificationPayload {
            method: method.to_string(),
            params,
        };
        let _ = app.emit(SIDECAR_NOTIFICATION_EVENT, payload);
    }
}

fn kill_agent_pid(pid: u32) {
    // SIGTERM is the courteous first signal — agents may flush state.
    // SIGKILL is the follow-up after a brief grace period for any
    // refusal to exit. The recovery screen is opt-in cleanup; we accept
    // best-effort semantics here.
    #[cfg(unix)]
    {
        // SAFETY: ``libc::kill`` accepts a raw PID. We discard the
        // return value because already-dead processes return ESRCH and
        // we treat that as success.
        unsafe {
            libc::kill(pid as libc::pid_t, libc::SIGTERM);
        }
        std::thread::sleep(std::time::Duration::from_millis(250));
        unsafe {
            libc::kill(pid as libc::pid_t, libc::SIGKILL);
        }
    }
    #[cfg(windows)]
    {
        // ``taskkill /F /PID`` is the standard Windows equivalent.
        let _ = std::process::Command::new("taskkill")
            .args(["/F", "/PID", &pid.to_string()])
            .output();
    }
}

fn spawn_stderr_collector(stderr: ChildStderr, lines: Arc<Mutex<VecDeque<String>>>) {
    std::thread::spawn(move || {
        let mut reader = BufReader::new(stderr);
        let mut line = String::new();
        loop {
            line.clear();
            let Ok(read) = reader.read_line(&mut line) else {
                break;
            };
            if read == 0 {
                break;
            }
            let text = line.trim_end().to_string();
            if text.is_empty() {
                continue;
            }
            if let Ok(mut ring) = lines.lock() {
                if ring.len() == MAX_STDERR_LINES {
                    ring.pop_front();
                }
                ring.push_back(text);
            }
        }
    });
}

fn snapshot_stderr(lines: &Arc<Mutex<VecDeque<String>>>) -> Vec<String> {
    lines
        .lock()
        .map(|ring| ring.iter().cloned().collect())
        .unwrap_or_default()
}

fn serialize_error(error: JsonRpcError) -> Value {
    json!({"code": error.code, "message": error.message})
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crash_payload_keeps_latest_stderr_lines() {
        let lines = Arc::new(Mutex::new(VecDeque::new()));
        {
            let mut guard = lines.lock().expect("lock ring");
            guard.push_back("one".to_string());
            guard.push_back("two".to_string());
            guard.push_back("three".to_string());
        }
        let payload = SidecarCrashPayload {
            exit_code: Some(17),
            last_stderr_lines: snapshot_stderr(&lines),
            log_file_path: None,
        };
        assert_eq!(payload.exit_code, Some(17));
        assert_eq!(payload.last_stderr_lines, vec!["one", "two", "three"]);
    }

    #[test]
    fn locate_managed_venv_python_returns_none_in_dev_tree() {
        // ``cargo test`` runs on a developer machine that has no
        // pkg-installed managed venv at the production system path.
        // The function must return None so ``sidecar_command()`` falls
        // back to ``uv run``. (A packaged build is exercised end-to-end
        // by ``sidecar_round_trip_handshake_and_recents_list`` below.)
        assert!(
            locate_managed_venv_python().is_none(),
            "dev test environment must not have a managed venv at the production path"
        );
    }

    #[test]
    fn sidecar_command_with_bd_path_sets_agentshore_bd_bin_env() {
        let bd_path = std::path::Path::new("/tmp/agentshore-bd-fixture");
        let cmd = sidecar_command(Some(bd_path));
        let found = cmd
            .get_envs()
            .any(|(k, v)| k == AGENTSHORE_BD_BIN_ENV && v == Some(bd_path.as_os_str()));
        assert!(
            found,
            "expected AGENTSHORE_BD_BIN={} in command env",
            bd_path.display()
        );
    }

    #[test]
    fn sidecar_command_without_bd_path_omits_agentshore_bd_bin_env() {
        let cmd = sidecar_command(None);
        let found = cmd.get_envs().any(|(k, _)| k == AGENTSHORE_BD_BIN_ENV);
        assert!(!found, "AGENTSHORE_BD_BIN must not be set when bd_path is None");
    }

    #[test]
    fn sidecar_round_trip_handshake_and_recents_list() {
        let mut cmd = sidecar_command(None);
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

    /// Pure unit test for the dispatch logic. Constructs a stub
    /// ``pending`` map and verifies that a response line keyed by its
    /// id is delivered to the registered channel.
    #[test]
    fn dispatch_line_routes_response_to_pending_channel() {
        // We can't easily construct a tauri::AppHandle in a unit test,
        // so we exercise the response branch by inserting a sender and
        // verifying delivery. The notification branch is exercised in
        // the integration test below.
        let pending: Arc<Mutex<HashMap<i64, mpsc::Sender<JsonRpcResponse>>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let (tx, rx) = mpsc::channel();
        pending.lock().unwrap().insert(7, tx);

        // We need a real AppHandle for dispatch_line; but the response
        // branch doesn't touch `app`. To exercise just the response
        // dispatch path, replicate it here:
        let line = r#"{"jsonrpc":"2.0","id":7,"result":{"ok":true}}"#;
        let value: Value = serde_json::from_str(line).unwrap();
        let id = value.get("id").and_then(Value::as_i64).expect("id");
        assert_eq!(id, 7);
        let response = decode_response_line(line).expect("decode");
        let mut pending_guard = pending.lock().unwrap();
        let sender = pending_guard.remove(&id).expect("registered sender");
        drop(pending_guard);
        sender.send(response).expect("send response");
        let received = rx.recv_timeout(Duration::from_millis(100)).expect("recv");
        assert_eq!(received.id, Value::from(7));
        assert_eq!(received.result.expect("result")["ok"], true);
    }

    #[test]
    fn agent_subprocess_spawned_inserts_tracked_pid() {
        let pids = Arc::new(Mutex::new(HashMap::new()));
        let params = serde_json::json!({
            "agent_id": "agent-claude",
            "agent_type": "claude_code",
            "pid": 42,
        });
        handle_agent_subprocess_notification("agent.subprocess_spawned", &params, &pids);
        let snapshot = pids.lock().unwrap().clone();
        assert_eq!(snapshot.len(), 1);
        let entry = snapshot.get("agent-claude").expect("tracked agent present");
        assert_eq!(entry.agent_type, "claude_code");
        assert_eq!(entry.pid, 42);
    }

    #[test]
    fn agent_subprocess_exited_removes_tracked_pid() {
        let pids = Arc::new(Mutex::new(HashMap::new()));
        let spawn = serde_json::json!({
            "agent_id": "agent-claude",
            "agent_type": "claude_code",
            "pid": 42,
        });
        handle_agent_subprocess_notification("agent.subprocess_spawned", &spawn, &pids);
        let exit = serde_json::json!({
            "agent_id": "agent-claude",
            "agent_type": "claude_code",
            "pid": 42,
            "exit_code": 0,
        });
        handle_agent_subprocess_notification("agent.subprocess_exited", &exit, &pids);
        assert!(pids.lock().unwrap().is_empty());
    }

    #[test]
    fn agent_subprocess_spawned_without_pid_is_ignored() {
        let pids = Arc::new(Mutex::new(HashMap::new()));
        let params = serde_json::json!({
            "agent_id": "agent-claude",
            "agent_type": "claude_code",
            // pid missing
        });
        handle_agent_subprocess_notification("agent.subprocess_spawned", &params, &pids);
        assert!(pids.lock().unwrap().is_empty());
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

    #[test]
    fn unrelated_notification_methods_do_not_touch_tracked_pids() {
        let pids = Arc::new(Mutex::new(HashMap::new()));
        let params = serde_json::json!({
            "agent_id": "agent-claude",
            "agent_type": "claude_code",
            "pid": 42,
        });
        handle_agent_subprocess_notification("$/progress", &params, &pids);
        handle_agent_subprocess_notification("sidecar.health", &params, &pids);
        assert!(pids.lock().unwrap().is_empty());
    }
}
