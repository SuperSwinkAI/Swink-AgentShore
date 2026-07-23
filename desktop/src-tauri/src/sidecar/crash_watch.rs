//! Background thread that detects an unexpected sidecar exit and emits
//! `sidecar:crashed` with the last stderr lines for diagnosis.

use serde::Serialize;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use tauri::{AppHandle, Emitter};

use super::transport::snapshot_stderr;
use super::SidecarSupervisor;
use crate::sidecar_pid::remove_sidecar_pid_file;

#[derive(Debug, Clone, Serialize)]
pub struct SidecarCrashPayload {
    pub exit_code: Option<i32>,
    pub last_stderr_lines: Vec<String>,
    pub log_file_path: Option<String>,
}

impl SidecarSupervisor {
    pub(super) fn start_crash_watcher(&self, app: AppHandle) {
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
                remove_sidecar_pid_file();
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
    use std::sync::Mutex;

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
}
