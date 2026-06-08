from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def test_windows_inno_template_does_not_block_silent_installs_on_optional_failure() -> None:
    template = (REPO_ROOT / "packaging/desktop/windows/AgentShore.iss.in").read_text()

    assert "if Optional then" in template
    assert "if not WizardSilent then" in template
    assert (
        "RunInstallerStep('Installing AgentShore CLI', 'install-agentshore-cli.ps1', True, True)"
        in template
    )


def test_windows_inno_template_matches_pkg_install_order() -> None:
    template = (REPO_ROOT / "packaging/desktop/windows/AgentShore.iss.in").read_text()

    desktop = template.index("Provisioning AgentShore Desktop sidecar")
    timelapse = template.index("Provisioning Timelapse Capture")
    cli = template.index("Installing AgentShore CLI")

    assert desktop < timelapse < cli


def test_windows_inno_template_cleans_installer_payload_after_postinstall() -> None:
    template = (REPO_ROOT / "packaging/desktop/windows/AgentShore.iss.in").read_text()

    assert "procedure CleanupInstallerPayload()" in template
    assert r"DelTree(ExpandConstant('{app}\installer'), True, True, True)" in template
    assert r'Type: filesandordirs; Name: "{localappdata}\Programs\AgentShore"' in template
    assert r'Type: filesandordirs; Name: "{localappdata}\AgentShore\venv"' in template
    assert r'Type: filesandordirs; Name: "{commonappdata}\AgentShore\venv"' in template


def test_windows_build_script_stages_required_helpers() -> None:
    script = (REPO_ROOT / "scripts/build-windows.ps1").read_text()

    assert "build:tauri-sidecars" not in script
    assert "AGENTSHORE_SKIP_BD_SIDECAR" in script
    assert "CARGO_HTTP_CHECK_REVOKE" in script
    assert "tauri.windows-installer.conf.json" in script
    assert r"$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" in script

    for helper in [
        "install-agentshore-venv.ps1",
        "install-agentshore-cli.ps1",
        "install-timelapse.ps1",
        "run-windows-installer-step.ps1",
    ]:
        assert helper in script

    assert "agentshore-wheel.whl" not in script
    assert "Split-Path -Leaf $WheelPath" in script
    assert '"/DWheelFileName=$WheelFileName"' in script
    assert 'Invoke-Checked "uv" "--native-tls" "build"' in script
    assert "AgentShoreSetup-$Version-x64.exe" in script


def test_windows_installer_step_runner_preserves_paths_with_spaces(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is required for the installer runner")

    script_dir = tmp_path / "Program Files Like" / "AgentShore" / "installer"
    script_dir.mkdir(parents=True)
    helper = script_dir / "helper script.ps1"
    wheel_dir = tmp_path / "wheel dir"
    wheel_dir.mkdir()
    wheel = wheel_dir / "agentshore wheel.whl"
    wheel.write_text("not a real wheel")
    helper.write_text(
        """
param([Parameter(Mandatory = $true)][string]$Wheel)
Write-Host "wheel=$Wheel"
if (-not $Wheel.EndsWith("agentshore wheel.whl")) { exit 17 }
exit 0
""".lstrip()
    )

    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(tmp_path / "local app data")
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPO_ROOT / "scripts/run-windows-installer-step.ps1"),
            "-StepName",
            "Path Quote Test",
            "-ScriptPath",
            str(helper),
            "-Wheel",
            str(wheel),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    log_path = Path(env["LOCALAPPDATA"]) / "AgentShore" / "install-logs" / "Path-Quote-Test.log"
    log_bytes = log_path.read_bytes()
    log_text = (
        log_bytes.decode("utf-16") if log_bytes.startswith(b"\xff\xfe") else log_bytes.decode()
    )
    assert result.returncode == 0, result.stderr
    assert log_text.strip() == f"wheel={wheel}"


def test_windows_build_script_regenerates_eula_and_supports_authenticode_signing() -> None:
    script = (REPO_ROOT / "scripts/build-windows.ps1").read_text()

    assert "Invoke-EulaGenerator" in script
    assert "build-eula-rtf.py" in script
    assert "Resolve-SignTool" in script
    assert "Resolve-CodeSigningThumbprint" in script
    assert "Invoke-AuthenticodeSign -FilePath $AppExe" in script
    assert "Invoke-AuthenticodeSign -FilePath $SetupOut" in script
    assert "[switch]$NoSign" in script
    assert "[string]$CertificateThumbprint" in script


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


def test_windows_sidecar_path_overlay_includes_npm_global_shims() -> None:
    sidecar_rs = (REPO_ROOT / "desktop/src-tauri/src/sidecar.rs").read_text()

    assert 'std::env::var_os("APPDATA")' in sidecar_rs
    assert '.join("npm")' in sidecar_rs


def test_windows_sidecar_venv_locator_uses_programdata_with_localappdata_fallback() -> None:
    sidecar_rs = (REPO_ROOT / "desktop/src-tauri/src/sidecar.rs").read_text()

    assert 'std::env::var_os("PROGRAMDATA")' in sidecar_rs
    assert "managed_venv_python_path_in_programdata" in sidecar_rs
    assert 'std::env::var_os("LOCALAPPDATA")' in sidecar_rs
    assert "managed_venv_python_path_in_local_appdata" in sidecar_rs
    assert r"AgentShore\venv\Scripts\python.exe" in sidecar_rs


def test_windows_sidecar_suppresses_bytecode_writes_for_machine_wide_venv() -> None:
    sidecar_rs = (REPO_ROOT / "desktop/src-tauri/src/sidecar.rs").read_text()

    assert 'cmd.env("PYTHONDONTWRITEBYTECODE", "1")' in sidecar_rs


def test_windows_venv_installer_uses_programdata() -> None:
    script = (REPO_ROOT / "scripts/install-agentshore-venv.ps1").read_text()

    assert r'Join-Path $env:ProgramData "AgentShore\venv"' in script
    assert r'Join-Path $env:LOCALAPPDATA "AgentShore\venv"' not in script


def test_windows_timelapse_installer_uses_programdata_venv() -> None:
    script = (REPO_ROOT / "scripts/install-timelapse.ps1").read_text()

    assert r'Join-Path $env:ProgramData "AgentShore\venv\Scripts\python.exe"' in script
    assert r'Join-Path $env:LOCALAPPDATA "AgentShore\venv\Scripts\python.exe"' not in script


def test_windows_build_script_documents_machine_wide_layout() -> None:
    script = (REPO_ROOT / "scripts/build-windows.ps1").read_text()

    assert "%ProgramFiles%\\AgentShore" in script
    assert "%ProgramData%\\AgentShore\\venv" in script
    assert "%LocalAppData%\\Programs\\AgentShore" not in script


def test_windows_sidecar_path_overlay_still_includes_localappdata_bd() -> None:
    sidecar_rs = (REPO_ROOT / "desktop/src-tauri/src/sidecar.rs").read_text()

    assert 'std::env::var_os("LOCALAPPDATA")' in sidecar_rs
    assert '.join("Programs")' in sidecar_rs
    assert '.join("bd")' in sidecar_rs


def test_windows_sidecar_launch_suppresses_console_window() -> None:
    sidecar_rs = (REPO_ROOT / "desktop/src-tauri/src/sidecar.rs").read_text()

    assert "std::os::windows::process::CommandExt" in sidecar_rs
    assert "CREATE_NO_WINDOW" in sidecar_rs
    assert "cmd.creation_flags(CREATE_NO_WINDOW)" in sidecar_rs


def test_windows_venv_installer_provisions_bd_at_install_time() -> None:
    script = (REPO_ROOT / "scripts/install-agentshore-venv.ps1").read_text()

    assert "from agentshore.beads.setup import provision_bd" in script
    assert "provision_bd(assume_yes=True)" in script


def test_windows_venv_installer_stops_stale_runtime_processes_before_replace() -> None:
    script = (REPO_ROOT / "scripts/install-agentshore-venv.ps1").read_text()

    assert "function Stop-AgentShoreRuntimeProcesses" in script
    assert "agentshore\\.sidecar" in script
    assert "agentshore(\\.exe)?\\s+dashboard" in script
    assert "Remove-DirectoryWithRetry -Path $VenvPath" in script


def test_windows_venv_installer_does_not_require_pip_in_managed_venv() -> None:
    script = (REPO_ROOT / "scripts/install-agentshore-venv.ps1").read_text()

    assert "-m pip" not in script
    assert "from importlib.metadata import version" in script


def test_windows_inno_template_uses_valid_wheel_filename_define() -> None:
    template = (REPO_ROOT / "packaging/desktop/windows/AgentShore.iss.in").read_text()

    assert "WheelFileName must be supplied" in template
    assert r"{app}\installer\{#WheelFileName}" in template
    assert "agentshore-wheel.whl" not in template
