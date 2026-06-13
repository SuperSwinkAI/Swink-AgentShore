from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROVISIONER = REPO_ROOT / "desktop/src-tauri/provisioner/src/main.rs"
PROVISIONER_SUPPORT = REPO_ROOT / "desktop/src-tauri/provisioner/src/agentshore_provisioner.rs"


def _provisioner_source() -> str:
    return PROVISIONER.read_text() + "\n" + PROVISIONER_SUPPORT.read_text()


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
    assert '"cargo" "build" "--release" "-p" "agentshore-provisioner" "--locked"' in script
    assert "Copy-Item -LiteralPath $ProvisionerExe" in script
    assert "Copy-Item -LiteralPath $UvPath" in script
    assert '"/DProvisionerFileName=agentshore-provisioner.exe"' in script
    assert '"/DUvFileName=uv.exe"' in script
    assert "agentshore-github-helper" not in script
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
    assert "Invoke-AuthenticodeSign -FilePath $SetupOut" in script


def test_windows_build_script_can_create_local_self_signed_cert() -> None:
    script = (REPO_ROOT / "scripts/build-windows.ps1").read_text()

    assert "[switch]$SelfSign" in script
    assert "[switch]$TrustSelfSignedCertificate" in script
    assert "[switch]$SetupSelfSignedCertificateOnly" in script
    assert "CN=AgentShore Local Dev Code Signing" in script
    assert "function New-AgentShoreSelfSignedCodeSigningCertificate" in script
    assert "New-SelfSignedCertificate" in script
    assert "-Type CodeSigningCert" in script
    assert "CurrentUser\\Root" in script
    assert "Use either -SelfSign or -CertificateThumbprint, not both." in script
    assert "-SetupSelfSignedCertificateOnly requires -SelfSign." in script
    assert "Self-signed certificate setup complete" in script


def test_windows_release_build_requires_signing_unless_explicitly_disabled() -> None:
    script = (REPO_ROOT / "scripts/build-windows.ps1").read_text()

    assert "Release Windows builds must be Authenticode-signed" in script
    assert "or intentionally pass -NoSign for local-only testing" in script
    assert "if (-not $DebugBuild)" in script


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
    runtime_rs = (REPO_ROOT / "desktop/src-tauri/src/sidecar_runtime.rs").read_text()
    layout_rs = (REPO_ROOT / "desktop/src-tauri/src/install_layout.rs").read_text()

    # PROGRAMDATA access and path helpers moved to install_layout (single source of truth).
    assert 'std::env::var_os("PROGRAMDATA")' in layout_rs
    assert "managed_venv_python_path" in layout_rs
    assert "managed_bin_path" in layout_rs
    assert "locate_machine_managed_bd" in sidecar_rs
    assert "AGENTSHORE_BD_BIN_ENV" in sidecar_rs
    assert "AGENTSHORE_GITHUB_HELPER" not in sidecar_rs
    assert r"AgentShore\venv\Scripts\python.exe" in runtime_rs


def test_windows_installer_does_not_ship_github_helper() -> None:
    cargo = (REPO_ROOT / "desktop/src-tauri/Cargo.toml").read_text()
    script = (REPO_ROOT / "scripts/build-windows.ps1").read_text()
    template = (REPO_ROOT / "packaging/desktop/windows/AgentShore.iss.in").read_text()

    assert "agentshore-github-helper" not in cargo
    assert "agentshore-github-helper" not in script
    assert "GitHubHelperFileName" not in template
    assert "octocrab" not in cargo
    assert "Win32_Security_Credentials" not in cargo


def test_windows_sidecar_keeps_legacy_and_user_path_overlays() -> None:
    env_rs = (REPO_ROOT / "desktop/src-tauri/src/sidecar_env.rs").read_text()

    assert 'std::env::var_os("APPDATA")' in env_rs
    assert '.join("npm")' in env_rs
    assert 'std::env::var_os("LOCALAPPDATA")' in env_rs
    assert 'ensure_env_from_userprofile(cmd, "APPDATA"' in env_rs
    assert 'ensure_env_from_userprofile(cmd, "LOCALAPPDATA"' in env_rs
    assert '.join("Programs")' in env_rs
    assert '.join("bd")' in env_rs
    assert '.join("Microsoft")' in env_rs
    assert '.join("WinGet")' in env_rs
    assert '.join("GitHub CLI")' in env_rs
    assert "apply_windows_github_cli_env(cmd)" in env_rs
    assert 'cmd.env("GH_CONFIG_DIR"' in env_rs


def test_windows_sidecar_records_pid_for_installer_cleanup() -> None:
    sidecar_rs = (REPO_ROOT / "desktop/src-tauri/src/sidecar.rs").read_text()
    layout_rs = (REPO_ROOT / "desktop/src-tauri/src/install_layout.rs").read_text()

    assert "write_sidecar_pid_file(child.id())" in sidecar_rs
    # sidecar.pid path definition moved to install_layout (single source of truth).
    assert "sidecar.pid" in layout_rs
    assert "remove_sidecar_pid_file()" in sidecar_rs


def test_windows_sidecar_launch_suppresses_console_window() -> None:
    env_rs = (REPO_ROOT / "desktop/src-tauri/src/sidecar_env.rs").read_text()
    layout_rs = (REPO_ROOT / "desktop/src-tauri/src/install_layout.rs").read_text()

    # CommandExt and CREATE_NO_WINDOW moved to install_layout (shared with provisioner).
    assert "std::os::windows::process::CommandExt" in layout_rs
    assert "CREATE_NO_WINDOW" in layout_rs
    assert "creation_flags(CREATE_NO_WINDOW)" in layout_rs
    # sidecar_env delegates to install_layout.
    assert "install_layout::apply_no_window_creation_flags" in env_rs


def test_windows_provisioner_has_stable_exit_codes_and_timeouts() -> None:
    source = _provisioner_source()

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
    source = _provisioner_source()
    layout_rs = (REPO_ROOT / "desktop/src-tauri/src/install_layout.rs").read_text()

    # ProgramData fallback constant and path helpers moved to install_layout (single source of
    # truth). The provisioner includes install_layout.rs via #[path] and delegates to it.
    assert r'OsString::from(r"C:\ProgramData")' in layout_rs
    assert 'join("install-logs")' in source
    assert 'join("venv")' in layout_rs
    assert 'join("bin")' in layout_rs
    assert 'join("runtime")' in layout_rs
    assert "icacls.exe" in source
    assert "*S-1-5-32-545" in source


def test_windows_provisioner_installs_wheel_and_machine_bd() -> None:
    source = _provisioner_source()

    assert 'os("venv")' in source
    assert 'os("pip")' in source
    assert "import agentshore.sidecar" in source
    # provision_bd lives in agentshore.beads.downloader; the installer is a
    # consented context so it passes assume_yes=True and the version pin.
    assert "from agentshore.beads.downloader import provision_bd" in source
    assert "provision_bd(REQUIRED_BD_VERSION, assume_yes=True, dest_dir=Path({}))" in source


def test_windows_provisioner_rolls_back_failed_venv_replace() -> None:
    source = _provisioner_source()

    assert "replace_venv_with_rollback" in source
    assert 'venv.with_file_name("venv.previous")' in source
    assert "rollback restored previous venv" in source


def test_windows_provisioner_does_not_shell_processes() -> None:
    source = _provisioner_source()

    assert "Command::new(program)" in source
    assert ".args(args)" in source
    assert "OpenProcess" in source
    assert "TerminateProcess" in source
    assert "powershell" not in source.lower()
    assert "cmd /c" not in source.lower()
