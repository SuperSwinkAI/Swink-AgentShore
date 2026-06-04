"""Tests for the value-coercion + SSH preflight helpers used by the CLI.

``str_or_none`` (``agentshore.config.coerce``) narrows ``dict.get(key)`` reads
of optional string fields to ``str | None`` at a single, testable boundary;
non-string values collapse to ``None`` rather than being coerced.
"""

from __future__ import annotations

from unittest.mock import patch

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
