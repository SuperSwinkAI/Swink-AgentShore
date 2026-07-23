//! Help-menu diagnostics: the OS-opener helper shared by "Open Log Folder" /
//! "Open in default app" / the Help-menu URL items, log-folder resolution,
//! and the Copy Diagnostics payload.

use serde::Serialize;
use std::path::{Path, PathBuf};

/// Hand a path or URL to the OS default handler — the browser for an https
/// URL, Finder/Explorer for a folder. Shared by the
/// `open_path_in_default_app` / `open_log_folder` commands and the Help-menu
/// URL items (Documentation / Release Notes / Report an Issue). Spawns the
/// platform opener detached; rejects an empty target.
pub fn spawn_open(target: &str) -> Result<(), String> {
    if target.trim().is_empty() {
        return Err("target must not be empty".to_string());
    }
    #[cfg(target_os = "macos")]
    let mut cmd = std::process::Command::new("open");
    #[cfg(target_os = "linux")]
    let mut cmd = std::process::Command::new("xdg-open");
    #[cfg(target_os = "windows")]
    let mut cmd = {
        let mut c = std::process::Command::new("cmd");
        c.args(["/C", "start", ""]);
        c
    };
    cmd.arg(target);
    cmd.spawn().map(|_| ()).map_err(|e| e.to_string())
}

// DESIGN §1.4 — Recovery Screen actions. These commands trust the caller-
// supplied path (the recovery screen plumbs it from the SidecarCrashedPayload
// the supervisor emitted, never from a user-typed input).
#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
pub fn open_path_in_default_app(path: String) -> Result<(), String> {
    spawn_open(&path)
}

/// Resolve the folder the Help > Open Log Folder item reveals. With a project
/// path, AgentShore writes per-session NDJSON to ``<project>/.agentshore/logs``
/// (the ``log_dir`` config default); without one — no project selected yet —
/// fall back to the global AgentShore home ``~/.config/swink/agentshore``.
/// Pure so the path logic is unit-testable; the caller creates and opens it.
pub fn resolve_log_folder(project_path: Option<&str>, home: Option<&Path>) -> Option<PathBuf> {
    if let Some(project) = project_path {
        let trimmed = project.trim();
        if !trimmed.is_empty() {
            return Some(PathBuf::from(trimmed).join(".agentshore").join("logs"));
        }
    }
    home.map(|h| h.join(".config").join("swink").join("agentshore"))
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
pub fn open_log_folder(project_path: Option<String>) -> Result<(), String> {
    // HOME is the norm on macOS/Linux; USERPROFILE is the Windows fallback.
    let home = std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from);
    let folder = resolve_log_folder(project_path.as_deref(), home.as_deref())
        .ok_or_else(|| "could not resolve a log folder".to_string())?;
    // Create it so the opener doesn't fail on a project that hasn't logged yet.
    let _ = std::fs::create_dir_all(&folder);
    spawn_open(&folder.to_string_lossy())
}

/// Diagnostics payload for the Help > Copy Diagnostics item. Assembled in Rust
/// (which owns the bundle version + build target) and emitted to the React
/// shell, which renders it in a copyable dialog.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Diagnostics {
    pub app: String,
    pub version: String,
    pub os: String,
    pub arch: String,
}

pub fn collect_diagnostics(version: &str) -> Diagnostics {
    Diagnostics {
        app: "AgentShore".to_string(),
        version: version.to_string(),
        os: std::env::consts::OS.to_string(),
        arch: std::env::consts::ARCH.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::{collect_diagnostics, resolve_log_folder};
    use std::path::{Path, PathBuf};

    #[test]
    fn resolve_log_folder_prefers_project_logs_dir() {
        let folder = resolve_log_folder(Some("/tmp/proj"), Some(Path::new("/home/u")))
            .expect("project path resolves a folder");
        assert_eq!(folder, PathBuf::from("/tmp/proj/.agentshore/logs"));
    }

    #[test]
    fn resolve_log_folder_falls_back_to_global_home_when_no_project() {
        let folder = resolve_log_folder(None, Some(Path::new("/home/u")))
            .expect("home resolves the global folder");
        assert_eq!(folder, PathBuf::from("/home/u/.config/swink/agentshore"));
    }

    #[test]
    fn resolve_log_folder_treats_blank_project_as_unset() {
        let folder = resolve_log_folder(Some("   "), Some(Path::new("/home/u")))
            .expect("blank project falls through to home");
        assert_eq!(folder, PathBuf::from("/home/u/.config/swink/agentshore"));
    }

    #[test]
    fn resolve_log_folder_returns_none_without_project_or_home() {
        assert!(resolve_log_folder(None, None).is_none());
    }

    #[test]
    fn collect_diagnostics_captures_version_and_build_target() {
        let diag = collect_diagnostics("1.2.3");
        assert_eq!(diag.app, "AgentShore");
        assert_eq!(diag.version, "1.2.3");
        assert_eq!(diag.os, std::env::consts::OS);
        assert_eq!(diag.arch, std::env::consts::ARCH);
    }
}
