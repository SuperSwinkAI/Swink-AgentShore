use std::process::Command;

use crate::sidecar_runtime::machine_managed_bin_path;

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x08000000;

#[cfg(target_os = "windows")]
pub(crate) fn apply_no_window_creation_flags(cmd: &mut Command) {
    cmd.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(target_os = "windows"))]
pub(crate) fn apply_no_window_creation_flags(_cmd: &mut Command) {}

/// Prepend common user-install locations so the sidecar sees tools installed
/// through terminals, Homebrew, npm, uv, winget, and the Windows installer.
pub(crate) fn apply_user_path_overlay(cmd: &mut Command) {
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
    if let Some(userprofile) = std::env::var_os("USERPROFILE") {
        candidates.push(std::path::PathBuf::from(&userprofile).join(".local/bin"));
        candidates.push(std::path::PathBuf::from(&userprofile).join(".cargo/bin"));
        #[cfg(target_os = "windows")]
        {
            ensure_env_from_userprofile(cmd, "APPDATA", &userprofile, &["AppData", "Roaming"]);
            ensure_env_from_userprofile(cmd, "LOCALAPPDATA", &userprofile, &["AppData", "Local"]);
        }
    }
    if let Some(appdata) = std::env::var_os("APPDATA") {
        candidates.push(std::path::PathBuf::from(appdata).join("npm"));
    }
    if let Some(local_appdata) = std::env::var_os("LOCALAPPDATA") {
        candidates.push(
            std::path::PathBuf::from(&local_appdata)
                .join("Programs")
                .join("bd"),
        );
        candidates.push(
            std::path::PathBuf::from(&local_appdata)
                .join("Microsoft")
                .join("WinGet")
                .join("Links"),
        );
    }
    if let Some(programdata) = std::env::var_os("PROGRAMDATA") {
        candidates.push(machine_managed_bin_path(std::path::Path::new(&programdata)));
    }
    if let Some(program_files) = std::env::var_os("ProgramFiles") {
        candidates.push(
            std::path::PathBuf::from(&program_files)
                .join("Git")
                .join("cmd"),
        );
        candidates.push(std::path::PathBuf::from(&program_files).join("GitHub CLI"));
        candidates.push(std::path::PathBuf::from(program_files).join("nodejs"));
    }
    #[cfg(target_os = "windows")]
    if let Some(program_files_x86) = std::env::var_os("ProgramFiles(x86)") {
        candidates.push(std::path::PathBuf::from(program_files_x86).join("GitHub CLI"));
    }

    #[cfg(target_os = "windows")]
    apply_windows_github_cli_env(cmd);

    for dir in candidates.into_iter().rev() {
        if !entries.iter().any(|e| e == &dir) {
            entries.insert(0, dir);
        }
    }

    if let Ok(joined) = std::env::join_paths(entries) {
        cmd.env("PATH", joined);
    }
}

#[cfg(target_os = "windows")]
fn ensure_env_from_userprofile(
    cmd: &mut Command,
    key: &str,
    userprofile: &std::ffi::OsStr,
    suffix: &[&str],
) {
    if std::env::var_os(key).is_some() {
        return;
    }
    let mut path = std::path::PathBuf::from(userprofile);
    for part in suffix {
        path.push(part);
    }
    cmd.env(key, path);
}

#[cfg(target_os = "windows")]
fn apply_windows_github_cli_env(cmd: &mut Command) {
    if std::env::var_os("GH_CONFIG_DIR").is_some() {
        return;
    }
    let appdata = std::env::var_os("APPDATA")
        .map(std::path::PathBuf::from)
        .or_else(|| {
            std::env::var_os("USERPROFILE").map(|profile| {
                std::path::PathBuf::from(profile)
                    .join("AppData")
                    .join("Roaming")
            })
        });
    if let Some(appdata) = appdata {
        cmd.env("GH_CONFIG_DIR", appdata.join("GitHub CLI"));
    }
}
