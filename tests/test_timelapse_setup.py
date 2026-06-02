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


async def test_install_timelapse_non_macos_returns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(setup.sys, "platform", "linux")
    result = await setup.install_timelapse()
    assert result.success is False
    assert "macOS" in result.message


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
