from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_windows_inno_template_matches_pkg_component_defaults() -> None:
    template = (REPO_ROOT / "packaging/desktop/windows/AgentShore.iss.in").read_text()

    assert "PrivilegesRequired=lowest" in template
    assert r"DefaultDirName={localappdata}\Programs\AgentShore" in template
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


def test_windows_sidecar_venv_locator_uses_localappdata() -> None:
    sidecar_rs = (REPO_ROOT / "desktop/src-tauri/src/sidecar.rs").read_text()

    assert 'std::env::var_os("LOCALAPPDATA")' in sidecar_rs
    assert "managed_venv_python_path_in_local_appdata" in sidecar_rs
    assert r"AgentShore\venv\Scripts\python.exe" in sidecar_rs


def test_windows_sidecar_launch_suppresses_console_window() -> None:
    sidecar_rs = (REPO_ROOT / "desktop/src-tauri/src/sidecar.rs").read_text()

    assert "std::os::windows::process::CommandExt" in sidecar_rs
    assert "CREATE_NO_WINDOW" in sidecar_rs
    assert "cmd.creation_flags(CREATE_NO_WINDOW)" in sidecar_rs


def test_windows_venv_installer_provisions_bd_at_install_time() -> None:
    script = (REPO_ROOT / "scripts/install-agentshore-venv.ps1").read_text()

    assert "from agentshore.beads.setup import provision_bd" in script
    assert "provision_bd(assume_yes=True)" in script


def test_windows_venv_installer_does_not_require_pip_in_managed_venv() -> None:
    script = (REPO_ROOT / "scripts/install-agentshore-venv.ps1").read_text()

    assert "-m pip" not in script
    assert "from importlib.metadata import version" in script


def test_windows_inno_template_uses_valid_wheel_filename_define() -> None:
    template = (REPO_ROOT / "packaging/desktop/windows/AgentShore.iss.in").read_text()

    assert "WheelFileName must be supplied" in template
    assert r"{app}\installer\{#WheelFileName}" in template
    assert "agentshore-wheel.whl" not in template
