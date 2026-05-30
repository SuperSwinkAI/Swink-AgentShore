"""Tests for the typed dict-extraction helpers in agentshore.cli.

These helpers exist to centralise narrowing of ``dict[str, object].get(key)``
into ``str | None`` / ``int | None`` at a single, testable boundary so callers
can drop ``# type: ignore`` suppressions when feeding values into ``PlayParams``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agentshore.cli import _check_ssh_signing_key_loaded, _int_or_none, _str_or_none


class TestStrOrNone:
    def test_missing_key_returns_none(self) -> None:
        assert _str_or_none({}, "agent_id") is None

    def test_explicit_none_returns_none(self) -> None:
        assert _str_or_none({"agent_id": None}, "agent_id") is None

    def test_string_value_returned_unchanged(self) -> None:
        assert _str_or_none({"agent_id": "claude-1"}, "agent_id") == "claude-1"

    def test_non_string_value_coerced_to_str(self) -> None:
        # Defensive coercion — IPC payloads should already be strings, but if
        # an int slips through we coerce rather than crash.
        assert _str_or_none({"agent_id": 42}, "agent_id") == "42"

    def test_empty_string_value_returned_as_empty_string(self) -> None:
        assert _str_or_none({"agent_id": ""}, "agent_id") == ""


class TestIntOrNone:
    def test_missing_key_returns_none(self) -> None:
        assert _int_or_none({}, "issue_number") is None

    def test_explicit_none_returns_none(self) -> None:
        assert _int_or_none({"issue_number": None}, "issue_number") is None

    def test_int_value_returned_unchanged(self) -> None:
        assert _int_or_none({"issue_number": 12}, "issue_number") == 12

    def test_string_digit_value_coerced_to_int(self) -> None:
        assert _int_or_none({"issue_number": "5"}, "issue_number") == 5

    def test_zero_is_preserved(self) -> None:
        assert _int_or_none({"issue_number": 0}, "issue_number") == 0

    def test_bool_is_rejected(self) -> None:
        # ``bool`` is a subclass of ``int`` in Python; we reject it explicitly
        # because a payload supplying ``True`` for ``issue_number`` is bogus.
        with pytest.raises(TypeError):
            _int_or_none({"issue_number": True}, "issue_number")

    def test_non_numeric_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            _int_or_none({"issue_number": "not-a-number"}, "issue_number")

    def test_unsupported_type_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            _int_or_none({"issue_number": [1, 2, 3]}, "issue_number")


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
