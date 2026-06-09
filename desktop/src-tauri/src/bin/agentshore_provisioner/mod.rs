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
use std::os::windows::process::CommandExt;

#[cfg(windows)]
use windows_sys::Win32::Foundation::CloseHandle;
#[cfg(windows)]
use windows_sys::Win32::System::Threading::{
    OpenProcess, TerminateProcess, WaitForSingleObject, PROCESS_TERMINATE,
};

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;
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

#[cfg(windows)]
fn terminate_pid(pid: u32) -> Result<bool, String> {
    unsafe {
        let handle = OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE_ACCESS, 0, pid);
        if handle.is_null() {
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
    let programdata =
        env::var_os("ProgramData").unwrap_or_else(|| OsString::from(r"C:\ProgramData"));
    PathBuf::from(programdata).join("AgentShore")
}

pub(crate) fn managed_venv_path() -> PathBuf {
    agentshore_programdata_root().join("venv")
}

pub(crate) fn managed_venv_python_path() -> PathBuf {
    managed_venv_path().join("Scripts").join("python.exe")
}

pub(crate) fn machine_bin_path() -> PathBuf {
    agentshore_programdata_root().join("bin")
}

pub(crate) fn runtime_dir() -> PathBuf {
    agentshore_programdata_root().join("runtime")
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

pub(crate) fn wheel_uri(path: &Path) -> String {
    let mut text = path.to_string_lossy().replace('\\', "/");
    if !text.starts_with('/') {
        text = format!("/{text}");
    }
    format!("file://{text}").replace(' ', "%20")
}

pub(crate) fn os(value: impl AsRef<OsStr>) -> OsString {
    value.as_ref().to_os_string()
}

#[cfg(windows)]
fn apply_no_window_creation_flags(command: &mut Command) {
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn apply_no_window_creation_flags(_command: &mut Command) {}

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
