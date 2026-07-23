//! Canonical Windows install-layout paths and process flags for AgentShore.
//!
//! Single source of truth for `ProgramData\AgentShore\{venv,bin,runtime}`,
//! `sidecar.pid`, and `CREATE_NO_WINDOW`. Shared by the Tauri library crate
//! and the provisioner binary via `#[path]` include. Do NOT duplicate these
//! literals in other files.
//!
//! On non-Windows builds path functions compile to no-ops; callers gate
//! Windows-specific logic on `#[cfg(target_os = "windows")]`.

#[cfg(target_os = "windows")]
use std::path::PathBuf;

/// `CREATE_NO_WINDOW` suppresses the console-window flash and avoids AV
/// window-hooking latency on every subprocess spawn.
#[cfg(target_os = "windows")]
pub const CREATE_NO_WINDOW: u32 = 0x08000000;

/// Apply `CREATE_NO_WINDOW` to *command* on Windows; no-op elsewhere.
#[cfg(target_os = "windows")]
pub fn apply_no_window_creation_flags(command: &mut std::process::Command) {
    use std::os::windows::process::CommandExt;
    command.creation_flags(CREATE_NO_WINDOW);
}

/// Apply `CREATE_NO_WINDOW` to *command* on Windows; no-op elsewhere.
#[cfg(not(target_os = "windows"))]
pub fn apply_no_window_creation_flags(_command: &mut std::process::Command) {}

/// Root data directory: `%ProgramData%\AgentShore` (Windows only).
///
/// Reads `ProgramData` from the environment; falls back to `C:\ProgramData` if
/// the variable is absent (should never happen on a real Windows install).
#[cfg(target_os = "windows")]
pub fn agentshore_data_root() -> PathBuf {
    let programdata = std::env::var_os("ProgramData")
        .or_else(|| std::env::var_os("PROGRAMDATA"))
        .unwrap_or_else(|| std::ffi::OsString::from(r"C:\ProgramData"));
    PathBuf::from(programdata).join("AgentShore")
}

/// Managed Python venv: `%ProgramData%\AgentShore\venv` (Windows only).
#[cfg(target_os = "windows")]
pub fn managed_venv_path() -> PathBuf {
    agentshore_data_root().join("venv")
}

/// Managed tool bin dir: `%ProgramData%\AgentShore\bin` (Windows only).
///
/// This is where the provisioner drops `bd.exe` and other machine-managed
/// tool binaries. Also known as `machine_managed_bin_path` in the sidecar
/// runtime.
#[cfg(target_os = "windows")]
pub fn managed_bin_path() -> PathBuf {
    agentshore_data_root().join("bin")
}

/// Runtime state dir: `%ProgramData%\AgentShore\runtime` (Windows only).
#[cfg(target_os = "windows")]
pub fn runtime_dir() -> PathBuf {
    agentshore_data_root().join("runtime")
}

/// Sidecar pid file: `%ProgramData%\AgentShore\runtime\sidecar.pid` (Windows only).
///
/// Used by the lib crate's `sidecar_pid` module. When `install_layout` is
/// included via `#[path]` into the provisioner binary, only a subset of
/// callers is present, so suppress the dead-code lint here rather than at
/// every hypothetical future call site.
#[cfg(target_os = "windows")]
#[allow(dead_code)]
pub fn sidecar_pid_path() -> PathBuf {
    runtime_dir().join("sidecar.pid")
}

/// Managed Python interpreter: `%ProgramData%\AgentShore\venv\Scripts\python.exe` (Windows only).
#[cfg(target_os = "windows")]
pub fn managed_venv_python_path() -> PathBuf {
    managed_venv_path().join("Scripts").join("python.exe")
}
