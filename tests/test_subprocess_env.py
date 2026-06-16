"""Tests for the Windows-hardened subprocess policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentshore import subprocess_env


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    subprocess_env.reset_caches()
    yield
    subprocess_env.reset_caches()


def test_resolve_tool_honors_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_git = tmp_path / "git-override.exe"
    fake_git.write_text("")
    monkeypatch.setenv("AGENTSHORE_GIT_BIN", str(fake_git))
    # which must never be consulted when the override resolves.
    monkeypatch.setattr(subprocess_env.shutil, "which", lambda _name: "/should/not/win")
    assert subprocess_env.resolve_tool("git") == str(fake_git)


def test_resolve_tool_falls_back_to_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTSHORE_GH_BIN", raising=False)
    monkeypatch.setattr(subprocess_env.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert subprocess_env.resolve_tool("gh") == "/usr/bin/gh"


def test_resolve_tool_returns_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTSHORE_GIT_BIN", raising=False)
    monkeypatch.setattr(subprocess_env.shutil, "which", lambda _name: None)
    # Force the non-Windows branch so canonical-path probing is skipped.
    monkeypatch.setattr(subprocess_env.sys, "platform", "linux")
    assert subprocess_env.resolve_tool("git") is None


def test_resolve_tool_caches_positive_hits_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_which(name: str) -> str | None:
        calls["n"] += 1
        return None

    monkeypatch.setattr(subprocess_env.sys, "platform", "linux")
    monkeypatch.setattr(subprocess_env.shutil, "which", fake_which)
    monkeypatch.delenv("AGENTSHORE_GIT_BIN", raising=False)
    assert subprocess_env.resolve_tool("git") is None
    assert subprocess_env.resolve_tool("git") is None
    # A miss is re-probed (so a later install is seen); two calls, not one.
    assert calls["n"] == 2


def test_hardened_env_git_is_non_interactive() -> None:
    env = subprocess_env.hardened_env(for_git=True)
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == ""
    assert env["GCM_INTERACTIVE"] == "Never"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_hardened_env_git_pins_noninteractive_editor() -> None:
    # The git editor is a separate interactive surface from the credential
    # prompt; without these a rebase-internal ``git commit -e`` opens vim and
    # hangs forever on a detached subprocess, leaking the worktree (#168).
    # ``true`` (not "") is the no-op editor git honours.
    env = subprocess_env.hardened_env(for_git=True)
    assert env[subprocess_env.GIT_EDITOR_ENV] == "true"
    assert env[subprocess_env.GIT_SEQUENCE_EDITOR_ENV] == "true"


def test_hardened_env_non_git_does_not_pin_editor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Don't let a host-level GIT_EDITOR leak in and mask the assertion.
    monkeypatch.delenv(subprocess_env.GIT_EDITOR_ENV, raising=False)
    monkeypatch.delenv(subprocess_env.GIT_SEQUENCE_EDITOR_ENV, raising=False)
    env = subprocess_env.hardened_env(for_gh=True)
    assert subprocess_env.GIT_EDITOR_ENV not in env
    assert subprocess_env.GIT_SEQUENCE_EDITOR_ENV not in env


def test_hardened_env_gh_disables_prompts_and_pager() -> None:
    env = subprocess_env.hardened_env(for_gh=True)
    assert env["GH_PROMPT_DISABLED"] == "1"
    assert env["GH_PAGER"] == "cat"


def test_hardened_env_grok_overlays_headless_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TERM", raising=False)
    env = subprocess_env.hardened_env(for_grok=True)
    assert env["CI"] == "1"
    assert env["NO_COLOR"] == "1"
    assert env["CLICOLOR"] == "0"
    # TERM defaults to dumb only when unset.
    assert env["TERM"] == "dumb"


def test_hardened_env_grok_preserves_existing_term(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    env = subprocess_env.hardened_env(for_grok=True)
    assert env["TERM"] == "xterm-256color"


def test_hardened_env_grok_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """for_grok must be opt-in: a plain git env carries none of the headless keys."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    env = subprocess_env.hardened_env(for_git=True)
    assert "CI" not in env
    assert "NO_COLOR" not in env


def test_hardened_env_overlay_wins_and_drops_none() -> None:
    env = subprocess_env.hardened_env(
        {"GH_TOKEN": "tok", "GH_CONFIG_DIR": None},  # type: ignore[dict-item]
        for_gh=True,
    )
    assert env["GH_TOKEN"] == "tok"
    assert "GH_CONFIG_DIR" not in env or env.get("GH_CONFIG_DIR") != None  # noqa: E711


def test_git_global_args_neutralizes_credentials() -> None:
    args = subprocess_env.git_global_args()
    joined = " ".join(args)
    assert "credential.helper=" in joined
    assert "credential.interactive=never" in joined
    assert "core.askpass=" in joined


def test_git_global_args_injects_schannel_only_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess_env.sys, "platform", "win32")
    monkeypatch.delenv(subprocess_env.GIT_SSL_BACKEND_ENV, raising=False)
    assert "http.sslBackend=schannel" in " ".join(subprocess_env.git_global_args())

    monkeypatch.setattr(subprocess_env.sys, "platform", "linux")
    assert "sslBackend" not in " ".join(subprocess_env.git_global_args())


def test_git_global_args_schannel_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess_env.sys, "platform", "win32")
    monkeypatch.setenv(subprocess_env.GIT_SSL_BACKEND_ENV, "")
    assert "sslBackend" not in " ".join(subprocess_env.git_global_args())


def test_timeout_for_scales_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess_env.sys, "platform", "linux")
    base = subprocess_env.timeout_for("git.read")
    assert base == 15.0

    monkeypatch.setattr(subprocess_env.sys, "platform", "win32")
    monkeypatch.delenv(subprocess_env.TOOL_TIMEOUT_SCALE_ENV, raising=False)
    assert subprocess_env.timeout_for("git.read") == 15.0 * 1.5


def test_no_window_creationflags_zero_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess_env.sys, "platform", "linux")
    assert subprocess_env.no_window_creationflags() == 0


def test_kill_tree_sync_uses_taskkill_tree_force_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows the whole tree is force-killed by pid via ``taskkill /T /F``."""
    import subprocess as _subprocess

    monkeypatch.setattr(subprocess_env.sys, "platform", "win32")
    captured: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> object:
        captured.append(list(argv))
        return type("_Completed", (), {"returncode": 0})()

    monkeypatch.setattr(_subprocess, "run", fake_run)
    subprocess_env.kill_tree_sync(4321)

    assert captured == [["taskkill", "/PID", "4321", "/T", "/F"]]


def test_kill_tree_sync_kills_target_pid_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off Windows only the target pid is SIGKILLed — never the process group."""
    monkeypatch.setattr(subprocess_env.sys, "platform", "linux")
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(subprocess_env.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    subprocess_env.kill_tree_sync(4321)

    assert killed == [(4321, 9)]
