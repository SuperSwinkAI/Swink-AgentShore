use std::process::Command;

use crate::install_layout;

/// Apply `CREATE_NO_WINDOW` to *cmd* on Windows; no-op elsewhere.
///
/// Delegates to `install_layout::apply_no_window_creation_flags` — the single
/// definition shared with the provisioner binary.
pub(crate) fn apply_no_window_creation_flags(cmd: &mut Command) {
    install_layout::apply_no_window_creation_flags(cmd);
}

/// Windows-only headless/utf-8 environment for the sidecar process.
///
/// Belt-and-suspenders so even a git/gh spawn inside the sidecar that has not
/// yet been routed through ``agentshore.subprocess_env`` still runs
/// non-interactively (never blocks on a Git-Credential-Manager / askpass dialog
/// the headless ``CREATE_NO_WINDOW`` process can never answer) and emits utf-8.
/// No-op off Windows so macOS/Linux behavior is unchanged.
#[cfg(target_os = "windows")]
pub(crate) fn apply_windows_headless_env(cmd: &mut Command) {
    cmd.env("PYTHONUTF8", "1");
    cmd.env("GIT_TERMINAL_PROMPT", "0");
    cmd.env("GH_PROMPT_DISABLED", "1");
    cmd.env("GH_NO_UPDATE_NOTIFIER", "1");
}

#[cfg(not(target_os = "windows"))]
pub(crate) fn apply_windows_headless_env(_cmd: &mut Command) {}

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
    #[cfg(target_os = "windows")]
    {
        // Machine-managed bin dir (the provisioner drops bd.exe here).
        // Gated on cfg so an empty PathBuf::new() is never pushed on POSIX
        // (an empty PATH entry = cwd in POSIX PATH semantics — a security hole).
        candidates.push(install_layout::managed_bin_path());
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

    match std::env::join_paths(entries) {
        Ok(joined) => {
            cmd.env("PATH", joined);
        }
        Err(err) => {
            // Windows caps a single env var near 32KB; if the overlaid PATH
            // exceeds it, join_paths errs. Don't silently drop the overlay
            // (which would lose git/gh discovery) — keep the inherited PATH and
            // leave a breadcrumb so the failure is diagnosable.
            eprintln!(
                "[agentshore-desktop][sidecar] PATH overlay join failed ({err}); \
                 using inherited PATH"
            );
        }
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
