#[cfg(target_os = "windows")]
fn sidecar_pid_file_path() -> Option<std::path::PathBuf> {
    let programdata = std::env::var_os("PROGRAMDATA")?;
    Some(
        std::path::PathBuf::from(programdata)
            .join("AgentShore")
            .join("runtime")
            .join("sidecar.pid"),
    )
}

#[cfg(not(target_os = "windows"))]
fn sidecar_pid_file_path() -> Option<std::path::PathBuf> {
    None
}

pub(crate) fn write_sidecar_pid_file(pid: u32) {
    let Some(path) = sidecar_pid_file_path() else {
        return;
    };
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

pub(crate) fn remove_sidecar_pid_file() {
    let Some(path) = sidecar_pid_file_path() else {
        return;
    };
    let _ = std::fs::remove_file(path);
}
