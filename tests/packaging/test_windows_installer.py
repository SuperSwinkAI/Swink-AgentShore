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


def test_windows_build_script_stages_required_helpers() -> None:
    script = (REPO_ROOT / "scripts/build-windows.ps1").read_text()

    assert "build:tauri-sidecars" not in script
    assert "AGENTSHORE_SKIP_BD_SIDECAR" in script
    assert "tauri.windows-installer.conf.json" in script

    for helper in [
        "install-agentshore-venv.ps1",
        "install-agentshore-cli.ps1",
        "install-timelapse.ps1",
        "run-windows-installer-step.ps1",
    ]:
        assert helper in script

    assert "agentshore-wheel.whl" in script
    assert "AgentShoreSetup-$Version-x64.exe" in script


def test_windows_tauri_config_disables_build_time_bd_sidecar() -> None:
    config = (
        REPO_ROOT / "packaging/desktop/windows/tauri.windows-installer.conf.json"
    ).read_text()

    assert '"externalBin": []' in config


def test_windows_venv_installer_provisions_bd_at_install_time() -> None:
    script = (REPO_ROOT / "scripts/install-agentshore-venv.ps1").read_text()

    assert "from agentshore.beads.setup import provision_bd" in script
    assert "provision_bd(assume_yes=True)" in script
