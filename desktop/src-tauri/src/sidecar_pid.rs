#[cfg(target_os = "windows")]
use crate::install_layout;

pub(crate) fn write_sidecar_pid_file(pid: u32) {
    #[cfg(target_os = "windows")]
    {
        let path = install_layout::sidecar_pid_path();
        if let Some(parent) = path.parent() {
            if let Err(err) = std::fs::create_dir_all(parent) {
                eprintln!(
                    "[agentshore-desktop][sidecar] could not create runtime dir {}: {err}",
                    parent.display()
                );
                return;
            }
        }
        if let Err(err) = std::fs::write(&path, pid.to_string()) {
            eprintln!(
                "[agentshore-desktop][sidecar] could not write sidecar pid file {}: {err}",
                path.display()
            );
        }
    }
    #[cfg(not(target_os = "windows"))]
    {
        let _ = pid;
    }
}

pub(crate) fn remove_sidecar_pid_file() {
    #[cfg(target_os = "windows")]
    {
        let path = install_layout::sidecar_pid_path();
        let _ = std::fs::remove_file(path);
    }
}
