"""Tests for the timelapse-capture auto-install routine helpers."""

from __future__ import annotations

import pytest

from agentshore.timelapse import setup


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("v24.1.0", 24),
        ("v25.6.1\n", 25),
        ("18.20.4", 18),
        ("not a version", None),
        ("", None),
    ],
)
def test_node_major(text: str, expected: int | None) -> None:
    assert setup._node_major(text) == expected


async def test_install_cli_uses_pinned_npm_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    # The installer must pull a pinned npm-registry version, not the GitHub
    # ``releases/latest`` tarball (which lagged at the broken 0.3.0 that
    # erroneously required ``--duration``). 0.3.1+ restores indefinite mode;
    # the pin tracks the deliberately-adopted CLI version (currently 0.4.0).
    seen: dict[str, object] = {}

    monkeypatch.setattr(setup.shutil, "which", lambda _name: "/usr/bin/npm")

    async def fake_run(cmd: list[str], **kwargs: object) -> setup.CommandResult:
        seen["cmd"] = cmd
        return setup.CommandResult(args=tuple(cmd), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(setup, "_run", fake_run)

    await setup._install_cli(tmp_path)  # type: ignore[arg-type]

    assert seen["cmd"] == ["npm", "install", "-g", "timelapse-capture@0.4.0"]


async def test_install_timelapse_linux_returns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(setup.sys, "platform", "linux")
    result = await setup.install_timelapse()
    assert result.success is False
    assert "macOS and Windows" in result.message


async def test_install_timelapse_windows_uses_winget_deps_without_homebrew(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setattr(setup.sys, "platform", "win32")
    calls: list[str] = []

    async def fail_ffmpeg(cwd: object) -> None:
        raise AssertionError("Windows installer must not require Homebrew ffmpeg preinstall")

    async def record_node(cwd: object) -> None:
        calls.append("node")

    async def record_windows_ffmpeg(cwd: object) -> None:
        calls.append("ffmpeg")

    async def record_cli(cwd: object) -> None:
        calls.append("cli")

    async def record_doctor(cwd: object) -> None:
        calls.append("doctor")

    monkeypatch.setattr(setup, "_ensure_ffmpeg", fail_ffmpeg)
    monkeypatch.setattr(setup, "_ensure_windows_ffmpeg", record_windows_ffmpeg)
    monkeypatch.setattr(setup, "_ensure_node", record_node)
    monkeypatch.setattr(setup, "_install_cli", record_cli)
    monkeypatch.setattr(setup, "_verify_doctor", record_doctor)

    result = await setup.install_timelapse(tmp_path)  # type: ignore[arg-type]

    assert result.success is True
    assert calls == ["ffmpeg", "node", "cli", "doctor"]


async def test_ensure_windows_node_reports_missing_winget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setattr(setup.sys, "platform", "win32")
    monkeypatch.setattr(setup.shutil, "which", lambda _name: None)

    with pytest.raises(setup.TimelapseError) as exc:
        await setup._ensure_node(tmp_path)  # type: ignore[arg-type]

    assert "Node.js 24+ is required but winget was not found" in str(exc.value)


async def test_ensure_windows_node_accepts_existing_supported_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setattr(setup.sys, "platform", "win32")
    monkeypatch.setattr(setup.shutil, "which", lambda name: f"C:/Program Files/nodejs/{name}.exe")

    async def fake_run(cmd: list[str], **kwargs: object) -> setup.CommandResult:
        assert cmd == ["node", "--version"]
        return setup.CommandResult(args=tuple(cmd), returncode=0, stdout="v24.1.0\n", stderr="")

    monkeypatch.setattr(setup, "_run", fake_run)

    await setup._ensure_node(tmp_path)  # type: ignore[arg-type]


async def test_ensure_windows_node_installs_when_existing_node_is_too_old(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setattr(setup.sys, "platform", "win32")
    node_versions = iter(["v22.14.0\n", "v26.3.0\n"])
    winget_calls: list[str] = []

    def fake_which(name: str) -> str | None:
        if name in {"node", "npm", "winget"}:
            return f"C:/tools/{name}.exe"
        return None

    async def fake_run(cmd: list[str], **kwargs: object) -> setup.CommandResult:
        if cmd == ["node", "--version"]:
            return setup.CommandResult(
                args=tuple(cmd), returncode=0, stdout=next(node_versions), stderr=""
            )
        winget_calls.append(" ".join(cmd))
        return setup.CommandResult(args=tuple(cmd), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(setup.shutil, "which", fake_which)
    monkeypatch.setattr(setup, "_run", fake_run)

    await setup._ensure_node(tmp_path)  # type: ignore[arg-type]

    assert len(winget_calls) == 1
    assert "--id OpenJS.NodeJS" in winget_calls[0]
    assert "--scope user" in winget_calls[0]


async def test_ensure_windows_ffmpeg_installs_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    seen = {"ffmpeg": False, "ffprobe": False}
    winget_calls: list[str] = []

    def fake_which(name: str) -> str | None:
        if name == "winget":
            return "C:/tools/winget.exe"
        if name in seen and seen[name]:
            return f"C:/Users/example/AppData/Local/Microsoft/WinGet/Links/{name}.exe"
        return None

    async def fake_run(cmd: list[str], **kwargs: object) -> setup.CommandResult:
        winget_calls.append(" ".join(cmd))
        seen["ffmpeg"] = True
        seen["ffprobe"] = True
        return setup.CommandResult(args=tuple(cmd), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(setup.shutil, "which", fake_which)
    monkeypatch.setattr(setup, "_run", fake_run)

    await setup._ensure_windows_ffmpeg(tmp_path)  # type: ignore[arg-type]

    assert len(winget_calls) == 1
    assert "--id Gyan.FFmpeg" in winget_calls[0]


async def test_install_timelapse_reports_step_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(setup.sys, "platform", "darwin")

    async def boom(cwd: object) -> None:
        raise setup.TimelapseError("ffmpeg/ffprobe are required but Homebrew was not found.")

    monkeypatch.setattr(setup, "_ensure_ffmpeg", boom)
    result = await setup.install_timelapse()
    assert result.success is False
    assert "Homebrew" in result.message
