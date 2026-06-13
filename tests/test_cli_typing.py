"""Tests for the value-coercion + SSH preflight helpers used by the CLI.

``str_or_none`` (``agentshore.config.coerce``) narrows ``dict.get(key)`` reads
of optional string fields to ``str | None`` at a single, testable boundary;
non-string values collapse to ``None`` rather than being coerced.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agentshore.cli.helpers import _check_ssh_signing_key_loaded
from agentshore.config.coerce import str_or_none


class TestStrOrNone:
    def test_none_returns_none(self) -> None:
        assert str_or_none(None) is None

    def test_string_value_returned_unchanged(self) -> None:
        assert str_or_none("claude-1") == "claude-1"

    def test_non_string_value_collapses_to_none(self) -> None:
        # A malformed scalar (e.g. an int from bad YAML) is dropped rather than
        # silently stringified into a bogus token.
        assert str_or_none(42) is None

    def test_empty_string_value_returned_as_empty_string(self) -> None:
        assert str_or_none("") == ""


class TestCheckSshSigningKeyLoaded:
    """desktop-l7i pre-flight: ssh-add probe at session start."""

    def test_ssh_add_missing_from_path_is_not_loaded(self) -> None:
        with patch("shutil.which", return_value=None):
            loaded, detail = _check_ssh_signing_key_loaded()
        assert loaded is False
        assert "not found" in detail

    def test_ssh_add_exit_1_no_identities_is_not_loaded(self) -> None:
        import subprocess

        result = subprocess.CompletedProcess(
            args=["ssh-add", "-l"],
            returncode=1,
            stdout="",
            stderr="The agent has no identities.",
        )
        with (
            patch("shutil.which", return_value="/usr/bin/ssh-add"),
            patch("subprocess.run", return_value=result),
        ):
            loaded, detail = _check_ssh_signing_key_loaded()
        assert loaded is False
        assert "no identities" in detail.lower()

    def test_ssh_add_exit_0_with_key_is_loaded(self) -> None:
        import subprocess

        fingerprint = "256 SHA256:abc123/wxyz Ember Raven (ED25519)"
        result = subprocess.CompletedProcess(
            args=["ssh-add", "-l"],
            returncode=0,
            stdout=fingerprint + "\n",
            stderr="",
        )
        with (
            patch("shutil.which", return_value="/usr/bin/ssh-add"),
            patch("subprocess.run", return_value=result),
        ):
            loaded, detail = _check_ssh_signing_key_loaded()
        assert loaded is True
        assert detail == fingerprint

    def test_ssh_add_timeout_is_not_loaded(self) -> None:
        import subprocess

        with (
            patch("shutil.which", return_value="/usr/bin/ssh-add"),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ssh-add", 5)),
        ):
            loaded, detail = _check_ssh_signing_key_loaded()
        assert loaded is False
        assert "probe failed" in detail


class TestSshSigningSetupHint:
    """The fix text must be platform-appropriate (#68): --apple-use-keychain is
    macOS-only and invalid on Windows/Linux."""

    def test_macos_hint_uses_apple_keychain(self) -> None:
        from agentshore.core.git_safety import ssh_signing_setup_hint

        with (
            patch("sys.platform", "darwin"),
            patch(
                "agentshore.core.git_safety._resolve_signing_key",
                return_value="/home/user/.ssh/id_ed25519",
            ),
        ):
            hint = ssh_signing_setup_hint()
        assert "--apple-use-keychain" in hint
        assert "/home/user/.ssh/id_ed25519" in hint

    def test_windows_hint_starts_agent_service_not_apple(self) -> None:
        from agentshore.core.git_safety import ssh_signing_setup_hint

        with (
            patch("sys.platform", "win32"),
            patch(
                "agentshore.core.git_safety._resolve_signing_key",
                return_value="/home/user/.ssh/id_ed25519",
            ),
        ):
            hint = ssh_signing_setup_hint()
        assert "ssh-agent" in hint
        assert "--apple-use-keychain" not in hint

    def test_linux_hint_is_plain_ssh_add(self) -> None:
        from agentshore.core.git_safety import ssh_signing_setup_hint

        with (
            patch("sys.platform", "linux"),
            patch(
                "agentshore.core.git_safety._resolve_signing_key",
                return_value="/home/user/.ssh/id_ed25519",
            ),
        ):
            hint = ssh_signing_setup_hint()
        assert hint == "ssh-add /home/user/.ssh/id_ed25519"


class TestSshSigningEnabled:
    """The pre-flight only fires when the repo actually SSH-signs commits."""

    @staticmethod
    def _fake_git(values: dict[str, str]):
        import subprocess

        def _run(args: list[str], cwd: object, **_kw: object) -> subprocess.CompletedProcess[str]:
            key = args[-1]
            if key not in values:
                return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=values[key] + "\n", stderr=""
            )

        return _run

    def test_enabled_when_gpgsign_true_and_format_ssh(self) -> None:
        from pathlib import Path

        from agentshore.core import git_safety

        with patch.object(
            git_safety,
            "_run_git",
            side_effect=self._fake_git({"commit.gpgsign": "true", "gpg.format": "ssh"}),
        ):
            assert git_safety.ssh_signing_enabled(Path("repo")) is True

    def test_disabled_when_gpgsign_unset(self) -> None:
        from pathlib import Path

        from agentshore.core import git_safety

        with patch.object(git_safety, "_run_git", side_effect=self._fake_git({})):
            assert git_safety.ssh_signing_enabled(Path("repo")) is False

    def test_disabled_when_format_is_not_ssh(self) -> None:
        from pathlib import Path

        from agentshore.core import git_safety

        with patch.object(
            git_safety,
            "_run_git",
            side_effect=self._fake_git({"commit.gpgsign": "true", "gpg.format": "openpgp"}),
        ):
            assert git_safety.ssh_signing_enabled(Path("repo")) is False


class TestReportSshSigningStatus:
    """The init/bootstrap pre-flight printer: only probes the agent when the
    repo SSH-signs commits, with a platform-correct hint."""

    def test_unconfigured_signing_is_quiet_and_skips_agent_probe(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from pathlib import Path

        from agentshore.cli import helpers

        with (
            patch("agentshore.core.git_safety.ssh_signing_enabled", return_value=False),
            patch.object(helpers, "_check_ssh_signing_key_loaded") as probe,
        ):
            result = helpers.report_ssh_signing_status(Path("repo"))
        out = capsys.readouterr().out
        assert result is True
        assert "not configured" in out
        assert "NOT LOADED" not in out
        probe.assert_not_called()

    def test_signed_repo_with_key_prints_ok_and_returns_true(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from pathlib import Path

        from agentshore.cli import helpers

        with (
            patch("agentshore.core.git_safety.ssh_signing_enabled", return_value=True),
            patch.object(
                helpers,
                "_check_ssh_signing_key_loaded",
                return_value=(True, "256 SHA256:abc (ED25519)"),
            ),
        ):
            result = helpers.report_ssh_signing_status(Path("repo"))
        out = capsys.readouterr().out
        assert result is True
        assert "SSH signing key: ok" in out

    def test_signed_repo_no_key_prints_windows_hint_and_returns_false(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from pathlib import Path

        from agentshore.cli import helpers

        with (
            patch("agentshore.core.git_safety.ssh_signing_enabled", return_value=True),
            patch.object(
                helpers, "_check_ssh_signing_key_loaded", return_value=(False, "agent unreachable")
            ),
            # Stub the key probe so the faked-win32 hint doesn't run the real
            # ``shutil.which`` (its win32 branch calls ``_winapi``, absent off
            # Windows); the hint's platform-branching logic still runs for real.
            patch(
                "agentshore.core.git_safety._resolve_signing_key",
                return_value="~/.ssh/id_ed25519",
            ),
            patch("sys.platform", "win32"),
        ):
            result = helpers.report_ssh_signing_status(Path("repo"))
        out = capsys.readouterr().out
        assert result is False
        assert "NOT LOADED" in out
        assert "ssh-agent" in out
        assert "--apple-use-keychain" not in out
