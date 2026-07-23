//! Tracked agent-subprocess PID map and the "kill all agents" recovery
//! surface (DESIGN §1.4 — Crash recovery "Kill all" surface).

use serde::Serialize;
use serde_json::Value;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use super::SidecarSupervisor;

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

impl SidecarSupervisor {
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
}

pub(super) fn handle_agent_subprocess_notification(
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

fn kill_agent_pid(pid: u32) {
    // SIGTERM is the courteous first signal — agents may flush state.
    // SIGKILL is the follow-up after a brief grace period for any
    // refusal to exit. The recovery screen is opt-in cleanup; we accept
    // best-effort semantics here.
    #[cfg(unix)]
    {
        send_unix_signal(pid, libc::SIGTERM);
        std::thread::sleep(std::time::Duration::from_millis(250));
        send_unix_signal(pid, libc::SIGKILL);
    }
    #[cfg(windows)]
    {
        // ``taskkill /F /T /PID`` is the standard Windows equivalent. ``/T``
        // kills the agent's whole process tree — CLI agents spawn their own
        // children (node, git, MCP servers) which Windows does not reap on
        // parent death, so a direct-PID kill leaks them (#155).
        let mut cmd = std::process::Command::new("taskkill");
        cmd.args(["/F", "/T", "/PID", &pid.to_string()]);
        crate::sidecar_env::apply_no_window_creation_flags(&mut cmd);
        let _ = cmd.output();
    }
}

#[cfg(unix)]
fn send_unix_signal(pid: u32, signal: libc::c_int) {
    // SAFETY: ``libc::kill`` is the Unix process-signal boundary. The
    // PID comes from the Python sidecar's tracked subprocess
    // notifications, and this recovery path treats ESRCH/already-dead
    // processes as successful cleanup, so the return value is deliberately
    // ignored.
    unsafe {
        let _ = libc::kill(pid as libc::pid_t, signal);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
