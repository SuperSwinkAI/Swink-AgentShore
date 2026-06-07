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

    for helper in [
        "install-agentshore-venv.ps1",
        "install-agentshore-cli.ps1",
        "install-timelapse.ps1",
        "run-windows-installer-step.ps1",
    ]:
        assert helper in script

    assert "agentshore-wheel.whl" in script
    assert "AgentShoreSetup-$Version-x64.exe" in script
