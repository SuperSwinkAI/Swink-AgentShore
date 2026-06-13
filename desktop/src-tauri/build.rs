use std::ffi::OsString;
use std::path::{Path, PathBuf};

const SIDECAR_NAME: &str = "agentshore-bd";
const SOURCE_BD_NAME: &str = "bd";

fn main() {
    let target = std::env::var("TARGET").unwrap_or_default();
    if target.contains("windows") {
        // Windows: bd is provisioned at install time via the managed sidecar venv,
        // not bundled as a Tauri externalBin. packaging/desktop/windows/tauri.windows-installer.conf.json
        // sets externalBin:[] for the Tauri build.
        println!("cargo:rerun-if-env-changed=TARGET");
    } else if std::env::var_os("AGENTSHORE_SKIP_BD_SIDECAR").is_none() {
        ensure_bd_sidecar();
    } else {
        println!("cargo:rerun-if-env-changed=AGENTSHORE_SKIP_BD_SIDECAR");
    }
    tauri_build::build()
}

fn ensure_bd_sidecar() {
    println!("cargo:rerun-if-env-changed=PATH");

    let manifest_dir = PathBuf::from(
        std::env::var_os("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR must be set by Cargo"),
    );
    let target = std::env::var("TARGET").unwrap_or_else(|_| std::env::consts::ARCH.to_string());
    let extension = if target.contains("windows") {
        ".exe"
    } else {
        ""
    };
    let sidecar_dir = manifest_dir.join("binaries").join(SIDECAR_NAME);
    let sidecar_path = sidecar_dir.join(format!("{SIDECAR_NAME}{extension}"));
    let tauri_sidecar_path = sidecar_dir.join(format!("{SIDECAR_NAME}-{target}{extension}"));

    println!("cargo:rerun-if-changed={}", sidecar_path.display());
    println!("cargo:rerun-if-changed={}", tauri_sidecar_path.display());

    if is_real_bd_binary(&sidecar_path) && is_real_bd_binary(&tauri_sidecar_path) {
        return;
    }

    let source = find_existing_sidecar_source(&sidecar_path, &tauri_sidecar_path)
        .or_else(find_real_bd_binary);
    let Some(source) = source else {
        panic!(
            "unable to stage Tauri bd sidecar: no real `bd` binary was found. \
Install `bd` on PATH, then run `npm --prefix desktop run build:tauri-sidecars`; \
expected sidecar output at {}",
            sidecar_path.display()
        );
    };

    if let Err(err) = stage_bd_sidecar(&source, &sidecar_path, &tauri_sidecar_path) {
        panic!(
            "unable to stage Tauri bd sidecar from {}: {err}",
            source.display()
        );
    }
}

fn find_existing_sidecar_source(sidecar_path: &Path, tauri_sidecar_path: &Path) -> Option<PathBuf> {
    for candidate in [sidecar_path, tauri_sidecar_path] {
        if is_real_bd_binary(candidate) {
            return Some(candidate.to_path_buf());
        }
    }
    None
}

fn stage_bd_sidecar(
    source: &Path,
    sidecar_path: &Path,
    tauri_sidecar_path: &Path,
) -> std::io::Result<()> {
    let sidecar_dir = sidecar_path
        .parent()
        .expect("sidecar path should always have a parent directory");
    std::fs::create_dir_all(sidecar_dir)?;
    copy_bd_binary(source, sidecar_path)?;
    copy_bd_binary(source, tauri_sidecar_path)?;
    Ok(())
}

fn copy_bd_binary(source: &Path, target: &Path) -> std::io::Result<()> {
    if source == target {
        return Ok(());
    }
    std::fs::copy(source, target)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        let mut permissions = std::fs::metadata(target)?.permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(target, permissions)?;
    }
    Ok(())
}

fn find_real_bd_binary() -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        let candidate = dir.join(executable_name(SOURCE_BD_NAME));
        if is_real_bd_binary(&candidate) {
            return Some(candidate);
        }
    }
    None
}

fn executable_name(stem: &str) -> OsString {
    let mut name = OsString::from(stem);
    if cfg!(windows) {
        name.push(".exe");
    }
    name
}

fn is_real_bd_binary(path: &Path) -> bool {
    if !is_executable_file(path) {
        return false;
    }
    let Ok(output) = std::process::Command::new(path).arg("--version").output() else {
        return false;
    };
    if !output.status.success() {
        return false;
    }
    let mut text = String::from_utf8_lossy(&output.stdout).to_string();
    text.push_str(&String::from_utf8_lossy(&output.stderr));
    text.contains("bd version")
}

fn is_executable_file(path: &Path) -> bool {
    if !path.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        std::fs::metadata(path)
            .map(|meta| meta.permissions().mode() & 0o111 != 0)
            .unwrap_or(false)
    }
    #[cfg(not(unix))]
    {
        true
    }
}
