use std::env;
use std::ffi::{OsStr, OsString};
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(windows)]
use windows_sys::Win32::Foundation::CloseHandle;
#[cfg(windows)]
use windows_sys::Win32::System::Threading::{
    OpenProcess, QueryFullProcessImageNameW, TerminateProcess, WaitForSingleObject,
    PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_TERMINATE,
};

// Shared install-layout constants (single source of truth for all path literals).
#[path = "../../install_layout.rs"]
mod install_layout;

#[cfg(windows)]
const SYNCHRONIZE_ACCESS: u32 = 0x00100000;

const USERS_SID: &str = "*S-1-5-32-545";
pub(crate) const SUCCESS: i32 = 0;
pub(crate) const INVALID_ARGS: i32 = 10;
pub(crate) const MISSING_PAYLOAD: i32 = 20;
pub(crate) const PROCESS_OR_SWAP_FAILURE: i32 = 30;
pub(crate) const UV_VENV_FAILURE: i32 = 40;
pub(crate) const WHEEL_INSTALL_FAILURE: i32 = 50;
pub(crate) const SIDECAR_IMPORT_FAILURE: i32 = 60;
pub(crate) const BD_PROVISION_FAILURE: i32 = 70;
pub(crate) const TIMELAPSE_FAILURE: i32 = 80;
pub(crate) const CLI_FAILURE: i32 = 90;

pub(crate) const UV_VENV_TIMEOUT: Duration = Duration::from_secs(10 * 60);
pub(crate) const WHEEL_INSTALL_TIMEOUT: Duration = Duration::from_secs(45 * 60);
pub(crate) const SIDECAR_IMPORT_TIMEOUT: Duration = Duration::from_secs(2 * 60);
pub(crate) const BD_PROVISION_TIMEOUT: Duration = Duration::from_secs(10 * 60);
pub(crate) const TIMELAPSE_TIMEOUT: Duration = Duration::from_secs(45 * 60);
pub(crate) const CLI_TIMEOUT: Duration = Duration::from_secs(20 * 60);
const ICACLS_TIMEOUT: Duration = Duration::from_secs(60);

#[derive(Debug)]
pub(crate) struct ProvisionError {
    pub(crate) code: i32,
    pub(crate) message: String,
}

impl ProvisionError {
    pub(crate) fn new(code: i32, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
}

pub(crate) type ProvisionResult<T> = Result<T, ProvisionError>;

#[derive(Clone)]
pub(crate) struct Logger {
    file: Arc<Mutex<File>>,
}

impl Logger {
    pub(crate) fn open(step_name: &str) -> ProvisionResult<Self> {
        let log_dir = agentshore_programdata_root().join("install-logs");
        fs::create_dir_all(&log_dir).map_err(|err| {
            ProvisionError::new(
                PROCESS_OR_SWAP_FAILURE,
                format!("create log directory {}: {err}", log_dir.display()),
            )
        })?;
        let path = log_dir.join(format!("{}.log", safe_log_name(step_name)));
        let file = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(&path)
            .map_err(|err| {
                ProvisionError::new(
                    PROCESS_OR_SWAP_FAILURE,
                    format!("open log file {}: {err}", path.display()),
                )
            })?;
        let logger = Self {
            file: Arc::new(Mutex::new(file)),
        };
        logger.line(format!("log: {}", path.display()));
        Ok(logger)
    }

    pub(crate) fn line(&self, message: impl AsRef<str>) {
        if let Ok(mut file) = self.file.lock() {
            let _ = writeln!(file, "{}", message.as_ref());
            let _ = file.flush();
        }
    }
}

pub(crate) struct ParsedArgs {
    pairs: Vec<(String, OsString)>,
}

impl ParsedArgs {
    pub(crate) fn parse(args: Vec<OsString>) -> ProvisionResult<Self> {
        let mut pairs = Vec::new();
        let mut iter = args.into_iter();
        while let Some(flag) = iter.next() {
            let flag_text = flag.to_string_lossy().to_string();
            if !flag_text.starts_with("--") {
                return Err(ProvisionError::new(INVALID_ARGS, "invalid argument"));
            }
            let Some(value) = iter.next() else {
                return Err(ProvisionError::new(INVALID_ARGS, "missing argument value"));
            };
            pairs.push((flag_text, value));
        }
        Ok(Self { pairs })
    }

    pub(crate) fn required_path(&self, flag: &str) -> ProvisionResult<PathBuf> {
        self.pairs
            .iter()
            .find(|(candidate, _)| candidate == flag)
            .map(|(_, value)| PathBuf::from(value))
            .ok_or_else(|| ProvisionError::new(INVALID_ARGS, format!("missing required {flag}")))
    }
}

#[derive(Debug)]
pub(crate) struct CommandResult {
    pub(crate) code: i32,
    pub(crate) timed_out: bool,
}

pub(crate) fn run_required(
    label: &str,
    failure_code: i32,
    program: &Path,
    args: &[OsString],
    timeout: Duration,
    logger: &Logger,
) -> ProvisionResult<()> {
    let result = run_logged(label, program, args, timeout, logger)?;
    if result.timed_out {
        return Err(ProvisionError::new(
            failure_code,
            format!("{label} timed out after {}s", timeout.as_secs()),
        ));
    }
    if result.code != 0 {
        return Err(ProvisionError::new(
            failure_code,
            format!("{label} failed with exit {}", result.code),
        ));
    }
    Ok(())
}

pub(crate) fn run_logged(
    label: &str,
    program: &Path,
    args: &[OsString],
    timeout: Duration,
    logger: &Logger,
) -> ProvisionResult<CommandResult> {
    logger.line(format!(
        "$ {} {}",
        program.display(),
        format_args_for_log(args)
    ));
    let mut command = Command::new(program);
    command
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // Force UTF-8 mode for any Python child (sidecar import, bd provisioning,
    // timelapse). Without it the managed venv's stdout defaults to the legacy
    // code page (cp1252 on most en-US installs), and structlog's print()
    // crashes with UnicodeEncodeError the moment a log line carries a
    // non-cp1252 character (e.g. winget progress glyphs folded into an error
    // message). The desktop sidecar already sets this in sidecar_env.rs; the
    // installer's provisioner must match so install-time logging is just as
    // robust.
    command.env("PYTHONUTF8", "1");
    apply_no_window_creation_flags(&mut command);
    let mut child = command.spawn().map_err(|err| {
        ProvisionError::new(
            MISSING_PAYLOAD,
            format!("launch {label} via {}: {err}", program.display()),
        )
    })?;

    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    let mut readers = Vec::new();
    if let Some(stdout) = stdout {
        readers.push(spawn_pipe_logger(
            label.to_string(),
            "stdout",
            stdout,
            logger.clone(),
        ));
    }
    if let Some(stderr) = stderr {
        readers.push(spawn_pipe_logger(
            label.to_string(),
            "stderr",
            stderr,
            logger.clone(),
        ));
    }

    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                join_readers(readers);
                return Ok(CommandResult {
                    code: status.code().unwrap_or(1),
                    timed_out: false,
                });
            }
            Ok(None) => {
                if started.elapsed() >= timeout {
                    logger.line(format!("{label}: timed out; killing child"));
                    let _ = child.kill();
                    let _ = child.wait();
                    join_readers(readers);
                    return Ok(CommandResult {
                        code: 1,
                        timed_out: true,
                    });
                }
                thread::sleep(Duration::from_millis(100));
            }
            Err(err) => {
                let _ = child.kill();
                let _ = child.wait();
                join_readers(readers);
                return Err(ProvisionError::new(
                    PROCESS_OR_SWAP_FAILURE,
                    format!("wait for {label}: {err}"),
                ));
            }
        }
    }
}

fn spawn_pipe_logger<R: Read + Send + 'static>(
    label: String,
    stream_name: &'static str,
    stream: R,
    logger: Logger,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let mut reader = BufReader::new(stream);
        let mut line = String::new();
        loop {
            line.clear();
            match reader.read_line(&mut line) {
                Ok(0) => break,
                Ok(_) => logger.line(format!("[{label} {stream_name}] {}", line.trim_end())),
                Err(err) => {
                    logger.line(format!("[{label} {stream_name}] read error: {err}"));
                    break;
                }
            }
        }
    })
}

fn join_readers(readers: Vec<thread::JoinHandle<()>>) {
    for reader in readers {
        let _ = reader.join();
    }
}

pub(crate) fn replace_venv_with_rollback<F>(
    venv: &Path,
    logger: &Logger,
    install: F,
) -> ProvisionResult<()>
where
    F: FnOnce() -> ProvisionResult<()>,
{
    let backup = venv.with_file_name("venv.previous");
    if backup.exists() {
        remove_dir_all(
            &backup,
            PROCESS_OR_SWAP_FAILURE,
            "remove stale venv.previous",
        )?;
    }
    let had_existing = venv.exists();
    if had_existing {
        logger.line(format!(
            "renaming existing venv {} -> {}",
            venv.display(),
            backup.display()
        ));
        fs::rename(venv, &backup).map_err(|err| {
            ProvisionError::new(
                PROCESS_OR_SWAP_FAILURE,
                format!(
                    "rename existing venv {} -> {}: {err}",
                    venv.display(),
                    backup.display()
                ),
            )
        })?;
    }

    match install() {
        Ok(()) => {
            if backup.exists() {
                if let Err(err) = fs::remove_dir_all(&backup) {
                    logger.line(format!("warning: could not remove backup venv: {err}"));
                }
            }
            Ok(())
        }
        Err(err) => {
            logger.line(format!("install failed; rolling back: {}", err.message));
            if venv.exists() {
                if let Err(remove_err) = fs::remove_dir_all(venv) {
                    logger.line(format!(
                        "warning: could not remove partial venv: {remove_err}"
                    ));
                }
            }
            if had_existing && backup.exists() {
                match fs::rename(&backup, venv) {
                    Ok(()) => logger.line("rollback restored previous venv"),
                    Err(restore_err) => {
                        logger.line(format!(
                            "rollback failed to restore previous venv: {restore_err}"
                        ));
                    }
                }
            }
            Err(err)
        }
    }
}

pub(crate) fn stop_recorded_runtime_processes(logger: &Logger) -> ProvisionResult<()> {
    let sidecar_pid = runtime_dir().join("sidecar.pid");
    stop_pid_file(&sidecar_pid, logger)
}

fn stop_pid_file(path: &Path, logger: &Logger) -> ProvisionResult<()> {
    if !path.exists() {
        logger.line(format!("pid file absent: {}", path.display()));
        return Ok(());
    }
    let raw = match fs::read_to_string(path) {
        Ok(value) => value,
        Err(err) => {
            logger.line(format!("could not read pid file {}: {err}", path.display()));
            return Ok(());
        }
    };
    let Ok(pid) = raw.trim().parse::<u32>() else {
        logger.line(format!("invalid pid file {}; removing", path.display()));
        let _ = fs::remove_file(path);
        return Ok(());
    };
    match terminate_pid(pid) {
        Ok(true) => logger.line(format!("terminated recorded process {pid}")),
        Ok(false) => logger.line(format!(
            "recorded process {pid} was not running or inaccessible"
        )),
        Err(err) => {
            return Err(ProvisionError::new(
                PROCESS_OR_SWAP_FAILURE,
                format!("terminate recorded pid {pid}: {err}"),
            ))
        }
    }
    let _ = fs::remove_file(path);
    Ok(())
}

/// Verify that `image_path` (wide string slice of length `len`) refers to a
/// `python.exe` binary under the AgentShore managed installation tree.
///
/// Accepted prefixes (case-insensitive on Windows):
/// - `%ProgramData%\AgentShore\` (machine-wide managed venv)
/// - `%LOCALAPPDATA%\AgentShore\` (per-user venv written by older installers)
///
/// The path must also end with `\python.exe`. Both checks together eliminate
/// the attack described in #115: a local user writes an arbitrary PID into the
/// Users-writable `sidecar.pid`; the next admin-run installer calls
/// `terminate_pid` on that PID. Without this guard, any system process could
/// be killed. With it, only processes that are both named `python.exe` *and*
/// live under the managed AgentShore tree are eligible.
#[cfg(windows)]
fn is_managed_python(image_path: &[u16], len: usize) -> bool {
    use std::ffi::OsString;
    use std::os::windows::ffi::OsStringExt;

    if len == 0 || len > image_path.len() {
        return false;
    }
    let path_str = OsString::from_wide(&image_path[..len])
        .to_string_lossy()
        .to_lowercase();

    // Must be named python.exe
    if !path_str.ends_with("\\python.exe") {
        return false;
    }

    // Must be under the machine-wide ProgramData path or the per-user
    // LOCALAPPDATA path that older installer revisions used.
    let programdata_prefix = std::env::var_os("ProgramData")
        .map(|v| {
            v.to_string_lossy()
                .to_lowercase()
                .trim_end_matches('\\')
                .to_string()
                + r"\agentshore\"
        })
        .unwrap_or_else(|| r"c:\programdata\agentshore\".to_string());

    let localappdata_prefix = std::env::var_os("LOCALAPPDATA")
        .map(|v| {
            v.to_string_lossy()
                .to_lowercase()
                .trim_end_matches('\\')
                .to_string()
                + r"\agentshore\"
        })
        .unwrap_or_default();

    path_str.starts_with(&programdata_prefix)
        || (!localappdata_prefix.is_empty() && path_str.starts_with(&localappdata_prefix))
}

/// Terminate the process with the given PID.
///
/// Security invariant (#115): before calling `TerminateProcess`, query the
/// target's full image path via `QueryFullProcessImageNameW` and verify it is
/// a `python.exe` binary under the AgentShore managed installation tree. If
/// the image check fails (wrong executable, wrong location, or
/// `QueryFullProcessImageNameW` itself fails because the process has already
/// exited), the kill is skipped and `Ok(false)` is returned. This prevents a
/// local user from writing an arbitrary PID into the Users-writable
/// `sidecar.pid` and having the elevated installer kill an unrelated process.
///
/// `PROCESS_QUERY_LIMITED_INFORMATION` is sufficient for
/// `QueryFullProcessImageNameW` and does not require `SeDebugPrivilege`.
#[cfg(windows)]
fn terminate_pid(pid: u32) -> Result<bool, String> {
    // Buffer sized per Windows docs: MAX_PATH is 260 but long-path names can
    // reach 32767 UTF-16 code units. Allocate the full ceiling so we never
    // truncate a valid path.
    const MAX_IMAGE_PATH: usize = 32768;

    unsafe {
        // Open with both TERMINATE and QUERY_LIMITED_INFORMATION so we can
        // inspect the image before committing to a kill.
        let handle = OpenProcess(
            PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE_ACCESS,
            0,
            pid,
        );
        if handle.is_null() {
            // Process not running or access denied; treat as already gone.
            return Ok(false);
        }

        // Query the full image path. If this fails (process exited between
        // OpenProcess and here, or any other error), do NOT kill — we can no
        // longer verify the target.
        let mut buf = [0u16; MAX_IMAGE_PATH];
        let mut buf_len = MAX_IMAGE_PATH as u32;
        // dwFlags = 0 means Win32 format (not native NT path).
        let query_ok = QueryFullProcessImageNameW(handle, 0, buf.as_mut_ptr(), &mut buf_len) != 0;
        if !query_ok {
            eprintln!(
                "[agentshore-provisioner] QueryFullProcessImageNameW failed for pid {pid}; \
                 skipping kill (process likely already exited)"
            );
            CloseHandle(handle);
            return Ok(false);
        }

        // Verify the target is our managed python.exe before committing to a kill.
        if !is_managed_python(&buf, buf_len as usize) {
            let path_display = String::from_utf16_lossy(&buf[..buf_len as usize]);
            eprintln!(
                "[agentshore-provisioner] pid {pid} image '{path_display}' is not a managed \
                 AgentShore python.exe; skipping kill"
            );
            CloseHandle(handle);
            return Ok(false);
        }

        let terminated = TerminateProcess(handle, 1) != 0;
        if terminated {
            WaitForSingleObject(handle, 5_000);
        }
        CloseHandle(handle);
        Ok(terminated)
    }
}

#[cfg(not(windows))]
fn terminate_pid(_pid: u32) -> Result<bool, String> {
    Ok(false)
}

pub(crate) fn grant_read_execute(path: &Path, logger: &Logger) -> ProvisionResult<()> {
    run_icacls(
        path,
        &format!("{USERS_SID}:(OI)(CI)RX"),
        "grant read/execute",
        logger,
    )
}

pub(crate) fn grant_modify(path: &Path, logger: &Logger) -> ProvisionResult<()> {
    run_icacls(
        path,
        &format!("{USERS_SID}:(OI)(CI)M"),
        "grant modify",
        logger,
    )
}

fn run_icacls(path: &Path, grant: &str, label: &str, logger: &Logger) -> ProvisionResult<()> {
    #[cfg(windows)]
    {
        let system_root =
            env::var_os("SystemRoot").unwrap_or_else(|| OsString::from(r"C:\Windows"));
        let icacls = PathBuf::from(system_root)
            .join("System32")
            .join("icacls.exe");
        validate_file(&icacls, "icacls.exe")?;
        run_required(
            label,
            PROCESS_OR_SWAP_FAILURE,
            &icacls,
            &[
                path.as_os_str().to_os_string(),
                os("/grant"),
                os(grant),
                os("/T"),
                os("/C"),
            ],
            ICACLS_TIMEOUT,
            logger,
        )
    }
    #[cfg(not(windows))]
    {
        let _ = (path, grant, label, logger);
        Ok(())
    }
}

pub(crate) fn cleanup_legacy_layout(logger: &Logger) {
    let Some(local) = env::var_os("LOCALAPPDATA").map(PathBuf::from) else {
        return;
    };
    for path in [
        local.join("Programs").join("AgentShore"),
        local.join("AgentShore").join("venv"),
    ] {
        if path.exists() {
            match fs::remove_dir_all(&path) {
                Ok(()) => logger.line(format!("removed legacy path {}", path.display())),
                Err(err) => logger.line(format!(
                    "warning: could not remove legacy path {}: {err}",
                    path.display()
                )),
            }
        }
    }
}

pub(crate) fn validate_file(path: &Path, label: &str) -> ProvisionResult<()> {
    if path.is_file() {
        Ok(())
    } else {
        Err(ProvisionError::new(
            MISSING_PAYLOAD,
            format!("{label} not found at {}", path.display()),
        ))
    }
}

fn remove_dir_all(path: &Path, code: i32, label: &str) -> ProvisionResult<()> {
    fs::remove_dir_all(path).map_err(|err| {
        ProvisionError::new(code, format!("{label} {} failed: {err}", path.display()))
    })
}

pub(crate) fn agentshore_programdata_root() -> PathBuf {
    install_layout::agentshore_data_root()
}

pub(crate) fn managed_venv_path() -> PathBuf {
    install_layout::managed_venv_path()
}

pub(crate) fn managed_venv_python_path() -> PathBuf {
    install_layout::managed_venv_python_path()
}

pub(crate) fn machine_bin_path() -> PathBuf {
    install_layout::managed_bin_path()
}

pub(crate) fn runtime_dir() -> PathBuf {
    install_layout::runtime_dir()
}

pub(crate) fn safe_log_name(step_name: &str) -> String {
    let mut out = String::new();
    for ch in step_name.chars() {
        if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' || ch == '.' {
            out.push(ch);
        } else if !out.ends_with('-') {
            out.push('-');
        }
    }
    out.trim_matches('-').to_string()
}

pub(crate) fn format_args_for_log(args: &[OsString]) -> String {
    args.iter()
        .map(|arg| {
            let text = arg.to_string_lossy();
            if text.contains(' ') {
                format!("\"{text}\"")
            } else {
                text.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

pub(crate) fn python_string_literal(path: &Path) -> String {
    let text = path
        .to_string_lossy()
        .replace('\\', "\\\\")
        .replace('\'', "\\'");
    format!("'{text}'")
}

pub(crate) fn os(value: impl AsRef<OsStr>) -> OsString {
    value.as_ref().to_os_string()
}

fn apply_no_window_creation_flags(command: &mut Command) {
    install_layout::apply_no_window_creation_flags(command);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn safe_log_name_replaces_whitespace() {
        assert_eq!(safe_log_name("Path Quote Test"), "Path-Quote-Test");
    }

    #[test]
    fn python_literal_escapes_windows_path() {
        let literal = python_string_literal(Path::new(r"C:\ProgramData\AgentShore\bin"));
        assert_eq!(literal, r"'C:\\ProgramData\\AgentShore\\bin'");
    }

    #[test]
    fn rollback_restores_existing_venv_on_failure() {
        let root = unique_temp_dir("rollback");
        let venv = root.join("venv");
        fs::create_dir_all(&venv).expect("create venv");
        fs::write(venv.join("sentinel.txt"), "old").expect("write sentinel");
        let logger = test_logger(&root);

        let err = replace_venv_with_rollback(&venv, &logger, || {
            fs::create_dir_all(&venv).expect("create replacement");
            fs::write(venv.join("new.txt"), "new").expect("write replacement");
            Err(ProvisionError::new(WHEEL_INSTALL_FAILURE, "install failed"))
        })
        .expect_err("install should fail");

        assert_eq!(err.code, WHEEL_INSTALL_FAILURE);
        assert_eq!(
            fs::read_to_string(venv.join("sentinel.txt")).unwrap(),
            "old"
        );
        assert!(!venv.join("new.txt").exists());
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn stderr_with_zero_exit_is_success() {
        let root = unique_temp_dir("stderr-zero");
        fs::create_dir_all(&root).unwrap();
        let logger = test_logger(&root);
        let result = run_logged(
            "stderr-zero",
            Path::new("cmd.exe"),
            &[os("/C"), os("echo benign stderr 1>&2 & exit /b 0")],
            Duration::from_secs(5),
            &logger,
        )
        .expect("run command");
        assert_eq!(result.code, 0);
        assert!(!result.timed_out);
        let log = fs::read_to_string(root.join("test.log")).unwrap();
        assert!(log.contains("benign stderr"));
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn pythonutf8_is_set_for_spawned_children() {
        // Regression: the managed venv Python must run in UTF-8 mode so a
        // non-cp1252 log line (e.g. a winget progress glyph in a timelapse
        // error) cannot crash structlog's print() with UnicodeEncodeError.
        let root = unique_temp_dir("pythonutf8");
        fs::create_dir_all(&root).unwrap();
        let logger = test_logger(&root);
        let result = run_logged(
            "pythonutf8",
            Path::new("cmd.exe"),
            &[os("/C"), os("echo PYTHONUTF8=%PYTHONUTF8%")],
            Duration::from_secs(5),
            &logger,
        )
        .expect("run command");
        assert_eq!(result.code, 0);
        let log = fs::read_to_string(root.join("test.log")).unwrap();
        assert!(log.contains("PYTHONUTF8=1"), "log was: {log}");
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn nonzero_exit_is_reported() {
        let root = unique_temp_dir("nonzero");
        fs::create_dir_all(&root).unwrap();
        let logger = test_logger(&root);
        let result = run_logged(
            "nonzero",
            Path::new("cmd.exe"),
            &[os("/C"), os("exit /b 17")],
            Duration::from_secs(5),
            &logger,
        )
        .expect("run command");
        assert_eq!(result.code, 17);
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn timeout_kills_child() {
        let root = unique_temp_dir("timeout");
        fs::create_dir_all(&root).unwrap();
        let logger = test_logger(&root);
        let result = run_logged(
            "timeout",
            Path::new("cmd.exe"),
            &[os("/C"), os("ping 127.0.0.1 -n 6 >nul")],
            Duration::from_millis(250),
            &logger,
        )
        .expect("run command");
        assert!(result.timed_out);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn stale_pid_file_is_ignored() {
        let root = unique_temp_dir("pid");
        fs::create_dir_all(&root).unwrap();
        let pid_file = root.join("sidecar.pid");
        fs::write(&pid_file, "not-a-pid").unwrap();
        let logger = test_logger(&root);
        stop_pid_file(&pid_file, &logger).expect("ignore stale pid");
        assert!(!pid_file.exists());
        let _ = fs::remove_dir_all(root);
    }

    /// Unit-test the `is_managed_python` path-verification guard that prevents
    /// the #115 attack (arbitrary PID written to the Users-writable `sidecar.pid`
    /// by a local user, then killed by the next elevated installer run).
    ///
    /// `is_managed_python` only exists on Windows; the test is gated accordingly.
    #[cfg(windows)]
    #[test]
    fn is_managed_python_accepts_managed_paths_and_rejects_others() {
        use std::os::windows::ffi::OsStrExt;

        let encode = |s: &str| -> Vec<u16> {
            std::ffi::OsStr::new(s)
                .encode_wide()
                .collect::<Vec<u16>>()
        };

        // Use the real ProgramData prefix so the test works on any Windows install.
        let programdata = env::var_os("ProgramData")
            .map(|v| v.to_string_lossy().to_string())
            .unwrap_or_else(|| r"C:\ProgramData".to_string());

        // Canonical managed path — must be accepted.
        let managed_path = format!(r"{programdata}\AgentShore\venv\Scripts\python.exe");
        let buf = encode(&managed_path);
        assert!(
            is_managed_python(&buf, buf.len()),
            "managed venv python.exe should be accepted: {managed_path}"
        );

        // Case-insensitive variant — must be accepted.
        let managed_lower = managed_path.to_lowercase();
        let buf_lower = encode(&managed_lower);
        assert!(
            is_managed_python(&buf_lower, buf_lower.len()),
            "lower-case managed path should be accepted"
        );

        // System python — must be rejected.
        let system_python = r"C:\Windows\System32\python.exe";
        let buf_sys = encode(system_python);
        assert!(
            !is_managed_python(&buf_sys, buf_sys.len()),
            "system python.exe must be rejected"
        );

        // Arbitrary executable — must be rejected.
        let arbitrary = r"C:\Windows\System32\svchost.exe";
        let buf_arb = encode(arbitrary);
        assert!(
            !is_managed_python(&buf_arb, buf_arb.len()),
            "svchost.exe must be rejected"
        );

        // Path-traversal spoof: contains AgentShore prefix text but doesn't
        // start with the managed root — must be rejected.
        let spoof = format!(
            r"C:\Users\attacker\{programdata}\AgentShore\venv\Scripts\python.exe"
        );
        let buf_spoof = encode(&spoof);
        assert!(
            !is_managed_python(&buf_spoof, buf_spoof.len()),
            "path-traversal spoof must be rejected"
        );

        // Zero-length must not panic and must return false.
        assert!(
            !is_managed_python(&[], 0),
            "zero-length buffer must be rejected"
        );
    }

    fn test_logger(root: &Path) -> Logger {
        let file = File::create(root.join("test.log")).expect("create test log");
        Logger {
            file: Arc::new(Mutex::new(file)),
        }
    }

    fn unique_temp_dir(label: &str) -> PathBuf {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        env::temp_dir().join(format!(
            "agentshore-provisioner-{label}-{}-{nanos}",
            std::process::id()
        ))
    }
}
