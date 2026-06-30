use std::path::{Path, PathBuf};
use std::process::Command;

use crate::install_layout;

/// Development fallback command for the Python sidecar.
pub(crate) fn development_sidecar_command() -> Command {
    if let Some(root) = find_repo_root_for_dev_sidecar() {
        let mut dev = match dev_venv_python_path(&root) {
            Some(python) => Command::new(python),
            None => {
                let mut uv = Command::new("uv");
                uv.arg("run").arg("python");
                uv
            }
        };
        dev.current_dir(&root)
            .env("PYTHONPATH", root.join("src"))
            .arg("-m")
            .arg("agentshore.sidecar");
        dev
    } else {
        let mut dev = Command::new("uv");
        dev.arg("run")
            .arg("python")
            .arg("-m")
            .arg("agentshore.sidecar");
        dev.env("PYTHONPATH", "../../src");
        dev
    }
}

pub(crate) fn find_repo_root_for_dev_sidecar() -> Option<PathBuf> {
    let mut starts = Vec::new();
    if let Ok(current_dir) = std::env::current_dir() {
        starts.push(current_dir);
    }
    if let Ok(current_exe) = std::env::current_exe() {
        if let Some(parent) = current_exe.parent() {
            starts.push(parent.to_path_buf());
        }
    }

    for start in starts {
        for candidate in start.ancestors() {
            if candidate.join("pyproject.toml").is_file()
                && candidate.join("src").join("agentshore").is_dir()
            {
                return Some(candidate.to_path_buf());
            }
        }
    }
    None
}

fn dev_venv_python_path(repo_root: &Path) -> Option<PathBuf> {
    let candidates = [
        repo_root.join(".venv").join("Scripts").join("python.exe"),
        repo_root.join(".venv").join("bin").join("python"),
    ];
    candidates.into_iter().find(|path| path.is_file())
}

#[cfg(target_os = "windows")]
pub(crate) fn locate_machine_managed_bd() -> Option<PathBuf> {
    let path = install_layout::managed_bin_path().join("bd.exe");
    if path.is_file() {
        Some(path)
    } else {
        None
    }
}

#[cfg(not(target_os = "windows"))]
pub(crate) fn locate_machine_managed_bd() -> Option<PathBuf> {
    None
}

/// Locate the Python interpreter inside the platform installer's managed venv.
pub(crate) fn locate_managed_venv_python() -> Option<PathBuf> {
    #[cfg(target_os = "windows")]
    {
        let path = install_layout::managed_venv_python_path();
        if path.is_file() {
            return Some(path);
        }
    }

    let home = std::env::var_os("HOME")?;
    locate_managed_venv_python_in_home(Path::new(&home))
}

pub(crate) fn locate_managed_venv_python_in_home(home: &Path) -> Option<PathBuf> {
    let path = managed_venv_python_path(home);
    if path.is_file() {
        Some(path)
    } else {
        None
    }
}

pub(crate) fn managed_venv_python_path(home: &Path) -> PathBuf {
    #[cfg(target_os = "macos")]
    {
        home.join("Library/Application Support/AgentShore/venv/bin/python")
    }
    #[cfg(target_os = "linux")]
    {
        home.join(".local/share/agentshore/venv/bin/python")
    }
    #[cfg(target_os = "windows")]
    {
        // Per-user fallback (AppData\Local) — the machine-managed path under
        // ProgramData is tried first in locate_managed_venv_python.
        home.join(r"AppData\Local\AgentShore\venv\Scripts\python.exe")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn locate_managed_venv_python_in_home_returns_none_when_absent() {
        let root = unique_temp_dir("absent-managed-venv");
        std::fs::create_dir_all(&root).expect("create temp home");
        let result = locate_managed_venv_python_in_home(&root);
        let _ = std::fs::remove_dir_all(&root);
        assert!(result.is_none());
    }

    #[test]
    fn locate_managed_venv_python_in_home_returns_python_when_present() {
        let root = unique_temp_dir("present-managed-venv");
        let python = managed_venv_python_path(&root);
        std::fs::create_dir_all(python.parent().expect("python parent")).expect("create venv bin");
        std::fs::write(&python, b"#!/bin/sh\n").expect("write fake python");

        let result = locate_managed_venv_python_in_home(&root);
        let _ = std::fs::remove_dir_all(&root);

        assert_eq!(result, Some(python));
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn managed_venv_python_path_in_programdata_matches_installer_layout() {
        let path = install_layout::managed_venv_python_path();
        // The managed venv python must be under ProgramData\AgentShore\venv\Scripts.
        let s = path.to_string_lossy().to_lowercase();
        assert!(
            s.contains("agentshore"),
            "path must contain AgentShore: {}",
            path.display()
        );
        assert!(
            s.contains("venv"),
            "path must contain venv: {}",
            path.display()
        );
        assert!(
            s.ends_with("python.exe"),
            "path must end with python.exe: {}",
            path.display()
        );
    }

    fn unique_temp_dir(label: &str) -> PathBuf {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("clock after epoch")
            .as_nanos();
        std::env::temp_dir().join(format!(
            "agentshore-desktop-{label}-{}-{nanos}",
            std::process::id()
        ))
    }
}
