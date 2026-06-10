from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).parents[2]
_POSTINSTALL = _ROOT / "packaging/desktop/installer-scripts/postinstall"
_CLI_INSTALL = _ROOT / "scripts/install-agentshore-cli.sh"


def _script() -> str:
    return _POSTINSTALL.read_text(encoding="utf-8")


def test_desktop_postinstall_runs_venv_helper_as_console_user() -> None:
    script = _script()

    assert 'launchctl asuser "${CONSOLE_UID}" /usr/bin/sudo -H -u "${CONSOLE_USER}" "$@"' in script
    assert 'run_as_console_user /bin/bash "$INSTALL_HELPER" --wheel "$BUNDLED_WHEEL"' in script
    assert 'launchctl asuser "${CONSOLE_UID}" \\\n       /bin/bash "$INSTALL_HELPER"' not in script


def test_desktop_postinstall_launch_agent_opens_app_by_path_after_installer() -> None:
    script = _script()
    launch_line = next(
        line for line in script.splitlines() if "/usr/bin/pgrep -qx Installer" in line
    )

    assert "/usr/bin/pgrep -qx Installer" in script
    assert "/usr/bin/open '${APP_PATH}'" in script
    assert "/usr/bin/open -a AgentShore" not in script
    assert "&" not in launch_line
    assert 'run_as_console_user /bin/launchctl remove "${FIRST_LAUNCH_LABEL}"' in script
    assert 'run_as_console_user /bin/launchctl load -w "${FIRST_LAUNCH_PLIST}"' in script


def test_macos_cli_helper_installs_same_bare_wheel_requirement_as_windows() -> None:
    script = _CLI_INSTALL.read_text(encoding="utf-8")

    assert '"$UV_BIN" tool install --native-tls --force --reinstall --python 3.12' in script
    # wheel path is passed as a plain path (not file:// URI) — uv resolves local paths directly.
    assert "$WHEEL_PATH" in script
    assert "agentshore[all]" not in script
