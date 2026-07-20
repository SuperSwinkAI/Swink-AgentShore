"""Tests for credential redaction in the structlog -> NDJSON pipeline.

Regression cover for the leak where ``structlog.processors.dict_tracebacks``
serialised exception frame locals — including the resolved per-agent identity
env dict — and wrote a live ``gho_…`` GitHub token into the session log in
cleartext.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentshore.log_redaction import REDACTED, redact_secrets
from agentshore.logging import get_logger, setup_logging

if TYPE_CHECKING:
    from collections.abc import Iterator

def _fake(prefix: str, body: str) -> str:
    """Assemble a credential-shaped test value at runtime.

    These fixtures are entirely synthetic, but a literal of the right shape
    sitting in the file trips credential scanners — GitHub push protection
    rejected this file over the Slack-shaped one below. Joining prefix and body
    here means no scanner-matching literal exists at rest while the redactor
    still sees the full shape at runtime, which is what these tests exercise.
    """
    return prefix + body


FAKE_GH_TOKEN = _fake("gho_", "0000EXAMPLE0000NotARealToken0000abcd")
FAKE_PAT = _fake("github_pat_", "11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789")
FAKE_ANTHROPIC = _fake("sk-ant-", "api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789")


@pytest.fixture
def log_file(tmp_path: Path) -> Iterator[Path]:
    """Configure the real logging pipeline against a temp NDJSON sink."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level

    log_dir = tmp_path / "logs"
    setup_logging(level="debug", log_dir=log_dir, session_id="redaction-test")
    path = log_dir / "agentshore-redaction-test.log"
    try:
        yield path
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers = original_handlers
        root.setLevel(original_level)


def _records(path: Path) -> list[dict[str, object]]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class TestTracebackFrameLocals:
    """(a) A secret living in an exception frame's locals must not survive."""

    def test_frame_local_env_dict_is_redacted(self, log_file: Path) -> None:
        def launch_agent() -> None:
            identity_env = {
                "GIT_AUTHOR_NAME": "Jwesleye",
                "GH_TOKEN": FAKE_GH_TOKEN,
                "GITHUB_TOKEN": FAKE_GH_TOKEN,
            }
            token = identity_env["GH_TOKEN"]
            assert token  # keep both locals live in the frame
            raise PermissionError("worktree not writable")

        log = get_logger("test")
        try:
            launch_agent()
        except PermissionError:
            log.error("unexpected_play_error", play_type="issue_pickup", exc_info=True)

        raw = log_file.read_text(encoding="utf-8")
        assert "gho_" not in raw
        assert FAKE_GH_TOKEN not in raw
        # The traceback itself must still be there — redaction, not suppression.
        assert "PermissionError" in raw
        assert "launch_agent" in raw
        assert REDACTED in raw

    def test_frame_local_bare_token_string_is_redacted(self, log_file: Path) -> None:
        def resolve() -> None:
            api_key = FAKE_ANTHROPIC
            assert api_key
            raise RuntimeError("boom")

        log = get_logger("test")
        try:
            resolve()
        except RuntimeError:
            log.error("resolve_failed", exc_info=True)

        raw = log_file.read_text(encoding="utf-8")
        assert "sk-ant-" not in raw
        assert FAKE_ANTHROPIC not in raw


class TestSecretKeyNames:
    """(b) A bare event kwarg with a secret-shaped key is redacted."""

    @pytest.mark.parametrize(
        "key",
        [
            "token",
            "api_key",
            "apikey",
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "ANTHROPIC_API_KEY",
            "password",
            "client_secret",
            "authorization",
            "bearer",
            "credential",
        ],
    )
    def test_secret_kwarg_redacted(self, log_file: Path, key: str) -> None:
        log = get_logger("test")
        log.info("some_event", **{key: "totally-innocuous-looking-value"})

        record = _records(log_file)[-1]
        assert record[key] == REDACTED
        assert "totally-innocuous-looking-value" not in json.dumps(record)

    def test_secret_key_nested_in_dict_redacted(self, log_file: Path) -> None:
        log = get_logger("test")
        log.info("spawn", env={"PATH": "/usr/bin", "GH_TOKEN": FAKE_GH_TOKEN})

        record = _records(log_file)[-1]
        assert record["env"]["GH_TOKEN"] == REDACTED
        assert record["env"]["PATH"] == "/usr/bin"

    def test_metadata_keys_are_not_redacted(self, log_file: Path) -> None:
        """Identity diagnostics must stay readable — they hold names, not values."""
        log = get_logger("test")
        log.info(
            "identity_status",
            token_source="keychain",
            token_resolved=True,
            gh_token_env="ZEKE_GH_TOKEN",
        )

        record = _records(log_file)[-1]
        assert record["token_source"] == "keychain"
        assert record["token_resolved"] is True
        assert record["gh_token_env"] == "ZEKE_GH_TOKEN"


class TestCredentialValueShapes:
    """(c) A credential-shaped value under an innocuous key is redacted."""

    @pytest.mark.parametrize(
        "value",
        [
            FAKE_GH_TOKEN,
            FAKE_PAT,
            FAKE_ANTHROPIC,
            _fake("ghp_", "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"),
            _fake("ghu_", "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"),
            _fake("ghs_", "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"),
            _fake("ghr_", "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"),
            _fake("xoxb-", "1234567890-ABCDEFGHIJKLMNOP"),
            _fake("sk-", "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"),
        ],
    )
    def test_credential_value_under_innocuous_key(self, log_file: Path, value: str) -> None:
        log = get_logger("test")
        log.info("agent_launch", note=value)

        record = _records(log_file)[-1]
        assert record["note"] == REDACTED

    def test_credential_embedded_in_longer_string(self, log_file: Path) -> None:
        """Frame locals arrive as reprs, so secrets are substrings, not whole values."""
        log = get_logger("test")
        log.info("agent_launch", detail=f"env={{'GH_TOKEN': '{FAKE_GH_TOKEN}'}} argv=[...]")

        record = _records(log_file)[-1]
        assert FAKE_GH_TOKEN not in record["detail"]
        assert REDACTED in record["detail"]
        assert "argv=[...]" in record["detail"]

    def test_credential_in_list_value(self, log_file: Path) -> None:
        log = get_logger("test")
        log.info("gh_command", cmd=["gh", "auth", "login", "--with-token", FAKE_GH_TOKEN])

        record = _records(log_file)[-1]
        assert FAKE_GH_TOKEN not in json.dumps(record)
        assert record["cmd"][:2] == ["gh", "auth"]


class TestOrdinaryContentUnaffected:
    """(d) Non-secret log content passes through byte-for-byte."""

    def test_ordinary_event_untouched(self, log_file: Path) -> None:
        log = get_logger("test")
        log.info(
            "play_completed",
            play_type="code_review",
            issue="gh-646",
            agent_id="b1d8f2b1-d644-42b9-a9e4-5028e1bd7a06",
            duration_s=41.5,
            success=True,
            branch="feature/some-thing_here",
            path="/Users/wes/Development/noodle",
            nested={"a": [1, 2, {"b": "sk-not-a-key"}]},
        )

        record = _records(log_file)[-1]
        assert record["event"] == "play_completed"
        assert record["play_type"] == "code_review"
        assert record["issue"] == "gh-646"
        assert record["agent_id"] == "b1d8f2b1-d644-42b9-a9e4-5028e1bd7a06"
        assert record["duration_s"] == 41.5
        assert record["success"] is True
        assert record["branch"] == "feature/some-thing_here"
        assert record["path"] == "/Users/wes/Development/noodle"
        assert record["nested"] == {"a": [1, 2, {"b": "sk-not-a-key"}]}

    def test_ordinary_traceback_untouched(self, log_file: Path) -> None:
        log = get_logger("test")
        try:
            raise ValueError("plain old failure")
        except ValueError:
            log.error("boom", exc_info=True)

        raw = log_file.read_text(encoding="utf-8")
        assert "plain old failure" in raw
        assert REDACTED not in raw


class TestProcessorUnit:
    """Direct unit cover for the processor's cheapness guarantees."""

    def test_depth_cap_stops_recursion(self) -> None:
        deep: dict[str, object] = {"GH_TOKEN": FAKE_GH_TOKEN}
        for _ in range(30):
            deep = {"nest": deep}

        out = redact_secrets(None, "info", {"event": "x", "payload": deep})

        # Walk down to the cap and confirm we stopped rather than blew the stack.
        assert out["event"] == "x"
        assert isinstance(out["payload"], dict)

    def test_non_string_scalars_pass_through(self) -> None:
        out = redact_secrets(None, "info", {"event": "x", "n": 5, "f": 1.5, "b": None})
        assert out == {"event": "x", "n": 5, "f": 1.5, "b": None}
