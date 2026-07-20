"""Tests for agentshore.logging — structured logging configuration."""

from __future__ import annotations

import logging

import pytest

from agentshore.logging import (
    attach_session_file_handler,
    get_logger,
    setup_logging,
    with_correlation,
)


class TestSetupLogging:
    """Exercise setup_logging() under various configurations."""

    def test_setup_logging_returns_without_error(self) -> None:
        """Calling with defaults must not raise."""
        setup_logging()
        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_setup_logging_includes_session_id(self, capsys: object) -> None:
        """When session_id is supplied, log output includes it."""
        setup_logging(session_id="sess-abc-123")
        log = get_logger("test")

        log.warning("hello", extra_key="val")

        # session_id is injected via a context-var processor; verify the var was set.
        from agentshore.logging import _session_id_var

        assert _session_id_var.get() == "sess-abc-123"

    def test_setup_logging_respects_level(self) -> None:
        """Setting level='warning' should configure root to WARNING."""
        setup_logging(level="warning")
        root = logging.getLogger()
        assert root.level == logging.WARNING

    def test_setup_logging_creates_log_file(self, tmp_path: object) -> None:
        """When log_dir + session_id are given, a log file is created."""
        from pathlib import Path

        log_dir = Path(str(tmp_path)) / "logs"
        setup_logging(level="info", log_dir=log_dir, session_id="test-session")
        assert log_dir.exists()
        log_file = log_dir / "agentshore-test-session.log"
        assert log_file.exists()


class TestAttachSessionFileHandler:
    """Exercise attach_session_file_handler() — the early, additive file sink."""

    @pytest.fixture(autouse=True)
    def _restore_root_handlers(self) -> None:
        """Snapshot/restore root handlers so this class doesn't leak state."""
        root = logging.getLogger()
        original = list(root.handlers)
        yield
        root.handlers = original

    def test_creates_log_file(self, tmp_path: object) -> None:
        from pathlib import Path

        log_dir = Path(str(tmp_path)) / "logs"
        attach_session_file_handler(log_dir, "sess-early-1")

        log_file = log_dir / "agentshore-sess-early-1.log"
        assert log_file.exists()

    def test_log_line_reaches_file(self, tmp_path: object) -> None:
        from pathlib import Path

        setup_logging()  # ensure structlog is configured, mirroring sidecar cold start
        log_dir = Path(str(tmp_path)) / "logs"
        attach_session_file_handler(log_dir, "sess-early-2")

        log = get_logger("test")
        log.warning("hello-from-early-handler")

        log_file = log_dir / "agentshore-sess-early-2.log"
        assert "hello-from-early-handler" in log_file.read_text(encoding="utf-8")

    def test_second_call_same_path_does_not_duplicate_handler(self, tmp_path: object) -> None:
        from pathlib import Path

        log_dir = Path(str(tmp_path)) / "logs"
        attach_session_file_handler(log_dir, "sess-early-3")
        attach_session_file_handler(log_dir, "sess-early-3")

        log_path = (log_dir / "agentshore-sess-early-3.log").resolve()
        matching = [
            h
            for h in logging.getLogger().handlers
            if isinstance(h, logging.FileHandler) and Path(h.baseFilename).resolve() == log_path
        ]
        assert len(matching) == 1

    def test_does_not_clear_existing_handlers(self, tmp_path: object) -> None:
        from pathlib import Path

        root = logging.getLogger()
        marker = logging.NullHandler()
        root.addHandler(marker)

        log_dir = Path(str(tmp_path)) / "logs"
        attach_session_file_handler(log_dir, "sess-early-4")

        assert marker in root.handlers


class TestWithCorrelation:
    """Exercise the correlation_id context manager."""

    def test_correlation_id_set_in_scope(self) -> None:
        from agentshore.logging import _correlation_id_var

        assert _correlation_id_var.get() is None
        with with_correlation("corr-42"):
            assert _correlation_id_var.get() == "corr-42"
        assert _correlation_id_var.get() is None
