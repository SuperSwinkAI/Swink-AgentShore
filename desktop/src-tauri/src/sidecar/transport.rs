//! Sidecar process transport: building the spawn `Command` (managed venv in
//! production, dev-tree fallback otherwise, plus the Windows headless/PATH
//! env overlay), the stderr-collector thread, and process-tree teardown.

use crate::sidecar_env::{
    apply_no_window_creation_flags, apply_user_path_overlay, apply_windows_headless_env,
};
use crate::sidecar_pid::remove_sidecar_pid_file;
use crate::sidecar_runtime::{
    development_sidecar_command, locate_machine_managed_bd, locate_managed_venv_python,
};
use std::collections::VecDeque;
use std::io::{BufRead, BufReader};
use std::process::{Child, ChildStderr, Command, Stdio};
use std::sync::{Arc, Mutex};

use super::SidecarSupervisor;

pub(super) const MAX_STDERR_LINES: usize = 50;
const AGENTSHORE_BD_BIN_ENV: &str = "AGENTSHORE_BD_BIN";

/// Build the command that spawns the Python sidecar.
///
/// Production: launch the Python interpreter from the pkg-installer's
/// managed venv (``/Library/Application Support/AgentShore/venv/bin/python``
/// on macOS — see ``locate_managed_venv_python``) with
/// ``-m agentshore.sidecar``. The venv is provisioned by the .pkg's
/// postinstall script which pip-installs the bundled agentshore wheel.
///
/// Development: when the managed venv is not present (running outside
/// an installed .pkg), fall back to the repo's ``.venv`` Python against
/// the in-tree source so ``tauri dev`` and ``cargo test`` work without an
/// install step. If the repo venv does not exist, use ``uv run`` as a
/// last-resort development fallback.
pub(super) fn sidecar_command(bd_path: Option<&std::path::Path>) -> Command {
    let mut cmd = match locate_managed_venv_python() {
        Some(python) => {
            let mut prod = Command::new(python);
            prod.arg("-m").arg("agentshore.sidecar");
            prod
        }
        None => development_sidecar_command(),
    };
    cmd.stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    cmd.env("PYTHONDONTWRITEBYTECODE", "1");
    apply_no_window_creation_flags(&mut cmd);
    apply_windows_headless_env(&mut cmd);
    if let Some(p) = bd_path {
        cmd.env(AGENTSHORE_BD_BIN_ENV, p);
    } else if let Some(p) = locate_machine_managed_bd() {
        cmd.env(AGENTSHORE_BD_BIN_ENV, p);
    }
    apply_user_path_overlay(&mut cmd);
    cmd
}

pub(super) fn spawn_stderr_collector(stderr: ChildStderr, lines: Arc<Mutex<VecDeque<String>>>) {
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

pub(super) fn snapshot_stderr(lines: &Arc<Mutex<VecDeque<String>>>) -> Vec<String> {
    lines
        .lock()
        .map(|ring| ring.iter().cloned().collect())
        .unwrap_or_default()
}

impl SidecarSupervisor {
    /// Explicit sidecar teardown: kill the entire sidecar process tree and
    /// reap the direct child. Called from the window-close path (which holds
    /// only an ``Arc`` — in-flight RPC threads may keep the supervisor alive
    /// past the holder's ``take()``, so ``Drop`` is not guaranteed to run at
    /// quit time); ``Drop`` delegates here as the backstop.
    ///
    /// The tree-kill matters on Windows (#155): the sidecar may be spawned
    /// through a ``uv`` trampoline (development fallback), so killing only
    /// the direct child leaves the real python — and its ``bd`` daemon
    /// children — running headless. ``taskkill /T`` walks the whole tree;
    /// ``Child::kill`` + ``wait`` stays as the direct-child backstop and
    /// reaper. On Unix the process group dies with the direct kill as before.
    pub fn kill_sidecar(&self) {
        remove_sidecar_pid_file();
        if let Ok(mut guard) = self.child.lock() {
            if let Some(proc_ref) = guard.as_mut() {
                kill_process_tree(proc_ref);
            }
            *guard = None;
        }
    }
}

/// Kill *proc_ref* and (on Windows) its entire descendant tree, then reap it.
///
/// Windows does not kill children when a parent dies and the sidecar may be
/// spawned through a ``uv`` trampoline (development fallback), so a plain
/// ``Child::kill`` strands the real python — and its ``bd`` daemon children —
/// running headless (#155). ``taskkill /T`` walks the tree first;
/// ``kill`` + ``wait`` stays as the direct-child backstop and reaper. On Unix
/// the direct kill suffices (the sidecar is spawned directly, no trampoline).
fn kill_process_tree(proc_ref: &mut Child) {
    #[cfg(windows)]
    {
        let mut cmd = std::process::Command::new("taskkill");
        cmd.args(["/F", "/T", "/PID", &proc_ref.id().to_string()]);
        crate::sidecar_env::apply_no_window_creation_flags(&mut cmd);
        let _ = cmd.output();
    }
    let _ = proc_ref.kill();
    let _ = proc_ref.wait();
}

#[cfg(test)]
mod tests {
    use super::*;

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
    fn sidecar_command_without_bd_path_uses_machine_managed_bd_when_available() {
        let cmd = sidecar_command(None);
        let value = cmd
            .get_envs()
            .find(|(k, _)| *k == std::ffi::OsStr::new(AGENTSHORE_BD_BIN_ENV))
            .and_then(|(_, value)| value.map(std::ffi::OsString::from));
        if let Some(path) = value {
            assert!(
                std::path::Path::new(&path).is_file(),
                "machine-managed AGENTSHORE_BD_BIN should point at an existing bd executable"
            );
        }
    }

    /// #155 regression guard: the explicit teardown must terminate and reap
    /// a still-running child. (On Windows ``kill_process_tree`` additionally
    /// walks the descendant tree via ``taskkill /T`` — that part is
    /// taskkill's contract; this pins the direct kill + reap.)
    #[test]
    fn kill_process_tree_terminates_and_reaps_a_live_child() {
        #[cfg(windows)]
        let mut cmd = {
            let mut c = Command::new("cmd");
            c.args(["/C", "ping -n 60 127.0.0.1 > NUL"]);
            c
        };
        #[cfg(unix)]
        let mut cmd = {
            let mut c = Command::new("sleep");
            c.arg("60");
            c
        };
        let mut child = cmd
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn long-running child");
        assert!(
            child.try_wait().expect("try_wait").is_none(),
            "child should still be running before the kill"
        );

        kill_process_tree(&mut child);

        // kill_process_tree wait()ed, so the child must be reaped: try_wait
        // reports an exit status immediately.
        assert!(
            child.try_wait().expect("try_wait after kill").is_some(),
            "child must be terminated and reaped after kill_process_tree"
        );
    }
}
