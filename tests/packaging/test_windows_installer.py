from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROVISIONER = REPO_ROOT / "desktop/src-tauri/src/bin/agentshore-provisioner.rs"
GITHUB_HELPER = REPO_ROOT / "desktop/src-tauri/src/bin/agentshore-github-helper.rs"


def test_windows_inno_template_matches_pkg_component_defaults() -> None:
    template = (REPO_ROOT / "packaging/desktop/windows/AgentShore.iss.in").read_text()

    assert "PrivilegesRequired=admin" in template
    assert r"DefaultDirName={autopf}\AgentShore" in template
    assert (
        'Name: "desktop"; Description: "AgentShore Desktop"; Types: custom; Flags: fixed'
        in template
    )
    assert (
        'Name: "timelapse"; Description: "Timelapse Capture (optional)"; Types: custom' in template
    )
    assert 'Name: "cli"; Description: "AgentShore CLI"; Types: custom' in template
    assert "WizardSelectComponents('desktop,cli')" in template


def test_windows_inno_template_calls_compiled_provisioner() -> None:
    template = (REPO_ROOT / "packaging/desktop/windows/AgentShore.iss.in").read_text()

    assert "ProvisionerFileName must be supplied" in template
    assert "GitHubHelperFileName must be supplied" in template
    assert r"{app}\installer\{#ProvisionerFileName}" in template
    assert "RunProvisionerStep('Provisioning AgentShore Desktop sidecar', 'sidecar'" in template
    assert "RunProvisionerStep('Provisioning Timelapse Capture', 'timelapse'" in template
    assert "RunProvisionerStep('Installing AgentShore CLI', 'cli'" in template
    assert "ExecAsOriginalUser(ProvisionerPath" in template
    assert "run-windows-installer-step.ps1" not in template
    assert "powershell.exe" not in template


def test_windows_inno_template_keeps_optional_failures_non_blocking() -> None:
    template = (REPO_ROOT / "packaging/desktop/windows/AgentShore.iss.in").read_text()

    assert "if Optional then" in template
    assert "if not WizardSilent then" in template
    assert "%ProgramData%\\AgentShore\\install-logs" in template


def test_windows_inno_template_matches_pkg_install_order() -> None:
    template = (REPO_ROOT / "packaging/desktop/windows/AgentShore.iss.in").read_text()

    desktop = template.index("Provisioning AgentShore Desktop sidecar")
    timelapse = template.index("Provisioning Timelapse Capture")
    cli = template.index("Installing AgentShore CLI")

    assert desktop < timelapse < cli


def test_windows_inno_template_avoids_per_user_cleanup_warning() -> None:
    template = (REPO_ROOT / "packaging/desktop/windows/AgentShore.iss.in").read_text()

    assert "[InstallDelete]" not in template
    assert "{localappdata}" not in template.lower()
    assert r'Type: filesandordirs; Name: "{commonappdata}\AgentShore\venv"' in template
    assert r'Type: filesandordirs; Name: "{commonappdata}\AgentShore\bin"' in template
    assert r'Type: filesandordirs; Name: "{commonappdata}\AgentShore\runtime"' in template


def test_windows_build_script_stages_provisioner_uv_and_wheel() -> None:
    script = (REPO_ROOT / "scripts/build-windows.ps1").read_text()

    assert "AGENTSHORE_SKIP_BD_SIDECAR" in script
    assert "CARGO_HTTP_CHECK_REVOKE" in script
    assert "tauri.windows-installer.conf.json" in script
    assert '"cargo" "build" "--release" "--bin" "agentshore-provisioner" "--locked"' in script
    assert '"cargo" "build" "--release" "--bin" "agentshore-github-helper" "--locked"' in script
    assert "Copy-Item -LiteralPath $ProvisionerExe" in script
    assert 'Copy-Item -LiteralPath $GitHubHelperExe -Destination (Join-Path $AppStageDir "agentshore-github-helper.exe")' in script
    assert 'Copy-Item -LiteralPath $GitHubHelperExe -Destination (Join-Path $InstallerStageDir "agentshore-github-helper.exe")' not in script
    assert "Copy-Item -LiteralPath $UvPath" in script
    assert '"/DProvisionerFileName=agentshore-provisioner.exe"' in script
    assert '"/DGitHubHelperFileName=agentshore-github-helper.exe"' in script
    assert '"/DUvFileName=uv.exe"' in script
    assert "install-agentshore-venv.ps1" not in script
    assert "install-agentshore-cli.ps1" not in script
    assert "install-timelapse.ps1" not in script
    assert "run-windows-installer-step.ps1" not in script
    assert "Split-Path -Leaf $WheelPath" in script
    assert "AgentShoreSetup-$Version-x64.exe" in script


def test_windows_build_script_pins_uv_for_installer_payload() -> None:
    script = (REPO_ROOT / "scripts/build-windows.ps1").read_text()

    assert '$PinnedUvVersion = "uv 0.8.11"' in script
    assert "function Resolve-Uv" in script
    assert "function Assert-UvVersion" in script
    assert ".StartsWith($PinnedUvVersion)" in script


def test_windows_build_script_signs_provisioner_when_cert_exists() -> None:
    script = (REPO_ROOT / "scripts/build-windows.ps1").read_text()

    assert "Invoke-AuthenticodeSign -FilePath $AppExe" in script
    assert "Invoke-AuthenticodeSign -FilePath $ProvisionerExe" in script
    assert "Invoke-AuthenticodeSign -FilePath $GitHubHelperExe" in script
    assert "Invoke-AuthenticodeSign -FilePath $SetupOut" in script


def test_windows_build_script_removes_stale_setup_artifacts_before_tauri_build() -> None:
    script = (REPO_ROOT / "scripts/build-windows.ps1").read_text()

    assert "function Clear-StaleSetupArtifacts" in script
    assert '"AgentShoreSetup-*.exe"' in script
    assert "Clear-StaleSetupArtifacts" in script
    assert "Close any open installer windows or security scanner handles" in script


def test_windows_tauri_config_disables_build_time_bd_sidecar() -> None:
    config = (REPO_ROOT / "packaging/desktop/windows/tauri.windows-installer.conf.json").read_text()

    assert '"beforeBuildCommand": "npm run build:tauri-frontend"' in config
    assert '"externalBin": []' in config


def test_windows_tauri_entrypoint_uses_gui_subsystem_for_release_builds() -> None:
    main_rs = (REPO_ROOT / "desktop/src-tauri/src/main.rs").read_text()

    assert '#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]' in main_rs


def test_windows_sidecar_runtime_uses_programdata_venv_and_bd_bin() -> None:
    sidecar_rs = (REPO_ROOT / "desktop/src-tauri/src/sidecar.rs").read_text()

    assert 'std::env::var_os("PROGRAMDATA")' in sidecar_rs
    assert "managed_venv_python_path_in_programdata" in sidecar_rs
    assert "machine_managed_bin_path" in sidecar_rs
    assert "locate_machine_managed_bd" in sidecar_rs
    assert "locate_github_helper" in sidecar_rs
    assert "AGENTSHORE_BD_BIN_ENV" in sidecar_rs
    assert "AGENTSHORE_GITHUB_HELPER_ENV" in sidecar_rs
    assert r"AgentShore\venv\Scripts\python.exe" in sidecar_rs


def test_windows_github_helper_uses_stdin_json_and_octocrab() -> None:
    source = GITHUB_HELPER.read_text()
    cargo = (REPO_ROOT / "desktop/src-tauri/Cargo.toml").read_text()

    assert "read_to_string(&mut input)" in source
    assert "serde_json::from_str::<HelperRequest>" in source
    assert "std::env::args" not in source
    assert 'octocrab = "=0.53.0"' in cargo
    assert 'windows-sys = { version = "=0.61.2"' in cargo
    assert "Win32_Security_Credentials" in cargo


def test_windows_sidecar_keeps_legacy_and_user_path_overlays() -> None:
    sidecar_rs = (REPO_ROOT / "desktop/src-tauri/src/sidecar.rs").read_text()

    assert 'std::env::var_os("APPDATA")' in sidecar_rs
    assert '.join("npm")' in sidecar_rs
    assert 'std::env::var_os("LOCALAPPDATA")' in sidecar_rs
    assert 'ensure_env_from_userprofile(cmd, "APPDATA"' in sidecar_rs
    assert 'ensure_env_from_userprofile(cmd, "LOCALAPPDATA"' in sidecar_rs
    assert '.join("Programs")' in sidecar_rs
    assert '.join("bd")' in sidecar_rs
    assert '.join("Microsoft")' in sidecar_rs
    assert '.join("WinGet")' in sidecar_rs
    assert '.join("GitHub CLI")' in sidecar_rs
    assert "apply_windows_github_cli_env(cmd)" in sidecar_rs
    assert 'cmd.env("GH_CONFIG_DIR"' in sidecar_rs


def test_windows_sidecar_records_pid_for_installer_cleanup() -> None:
    sidecar_rs = (REPO_ROOT / "desktop/src-tauri/src/sidecar.rs").read_text()

    assert "write_sidecar_pid_file(child.id())" in sidecar_rs
    assert "sidecar.pid" in sidecar_rs
    assert "remove_sidecar_pid_file()" in sidecar_rs


def test_windows_sidecar_launch_suppresses_console_window() -> None:
    sidecar_rs = (REPO_ROOT / "desktop/src-tauri/src/sidecar.rs").read_text()

    assert "std::os::windows::process::CommandExt" in sidecar_rs
    assert "CREATE_NO_WINDOW" in sidecar_rs
    assert "cmd.creation_flags(CREATE_NO_WINDOW)" in sidecar_rs


def test_windows_provisioner_has_stable_exit_codes_and_timeouts() -> None:
    source = PROVISIONER.read_text()

    for code in [
        "const INVALID_ARGS: i32 = 10",
        "const MISSING_PAYLOAD: i32 = 20",
        "const PROCESS_OR_SWAP_FAILURE: i32 = 30",
        "const UV_VENV_FAILURE: i32 = 40",
        "const WHEEL_INSTALL_FAILURE: i32 = 50",
        "const SIDECAR_IMPORT_FAILURE: i32 = 60",
        "const BD_PROVISION_FAILURE: i32 = 70",
        "const TIMELAPSE_FAILURE: i32 = 80",
        "const CLI_FAILURE: i32 = 90",
    ]:
        assert code in source

    assert "Duration::from_secs(10 * 60)" in source
    assert "Duration::from_secs(45 * 60)" in source
    assert "Duration::from_secs(2 * 60)" in source


def test_windows_provisioner_uses_programdata_layout_and_icacls() -> None:
    source = PROVISIONER.read_text()

    assert r'OsString::from(r"C:\ProgramData")' in source
    assert 'join("install-logs")' in source
    assert 'join("venv")' in source
    assert 'join("bin")' in source
    assert 'join("runtime")' in source
    assert "icacls.exe" in source
    assert "*S-1-5-32-545" in source


def test_windows_provisioner_installs_wheel_and_machine_bd() -> None:
    source = PROVISIONER.read_text()

    assert 'os("venv")' in source
    assert 'os("pip")' in source
    assert "import agentshore.sidecar" in source
    assert "provision_bd(assume_yes=True, dest_dir=Path({}))" in source


def test_windows_provisioner_rolls_back_failed_venv_replace() -> None:
    source = PROVISIONER.read_text()

    assert "replace_venv_with_rollback" in source
    assert 'venv.with_file_name("venv.previous")' in source
    assert "rollback restored previous venv" in source


def test_windows_provisioner_does_not_shell_processes() -> None:
    source = PROVISIONER.read_text()

    assert "Command::new(program)" in source
    assert ".args(args)" in source
    assert "OpenProcess" in source
    assert "TerminateProcess" in source
    assert "powershell" not in source.lower()
    assert "cmd /c" not in source.lower()
