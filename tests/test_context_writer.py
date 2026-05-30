"""Tests for agentshore.agents.context_writer — atomic context file writer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from agentshore.agents.context_writer import write_context_file


class TestWriteContextFile:
    """Exercise write_context_file() for correctness and atomicity."""

    def test_creates_valid_json(self, tmp_path: Path) -> None:
        """Written file must be valid JSON matching the input payload."""
        target = tmp_path / "context.json"
        payload = {"key": "value", "nested": {"a": 1}}
        write_context_file(target, payload)

        assert target.is_file()
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data == payload

    def test_context_file_contains_session_id(self, tmp_path: Path) -> None:
        """When payload includes session_id, it appears in the output."""
        target = tmp_path / "context.json"
        payload = {"session_id": "sess-xyz-789", "project": "agentshore"}
        write_context_file(target, payload)

        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["session_id"] == "sess-xyz-789"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        """Parent directories are created if they do not exist."""
        target = tmp_path / "deep" / "nested" / "context.json"
        write_context_file(target, {"ok": True})
        assert target.is_file()

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        """Calling twice overwrites the previous file."""
        target = tmp_path / "context.json"
        write_context_file(target, {"version": 1})
        write_context_file(target, {"version": 2})

        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["version"] == 2

    def test_atomic_write_uses_temp_file(self, tmp_path: Path) -> None:
        """Verify atomicity: the implementation uses tempfile + os.replace."""
        target = tmp_path / "context.json"
        payload = {"atomic": True}

        # Patch os.replace to verify it gets called (the core of atomicity).
        os_replace = __import__("os").replace
        with patch("agentshore.agents.context_writer.os.replace", wraps=os_replace) as mock_replace:
            write_context_file(target, payload)
            mock_replace.assert_called_once()
            # First arg is a temp path, second is the final target.
            call_args = mock_replace.call_args[0]
            assert str(call_args[1]) == str(target)

    def test_cleans_up_temp_on_error(self, tmp_path: Path) -> None:
        """If json.dump raises, the temp file is cleaned up."""
        target = tmp_path / "context.json"

        class BadValue:
            """Object that is not JSON serializable."""

        # json.dump will fail on this payload.
        with patch(
            "agentshore.agents.context_writer.json.dump",
            side_effect=TypeError("not serializable"),
        ):
            import pytest

            with pytest.raises(TypeError, match="not serializable"):
                write_context_file(target, {"bad": BadValue()})  # type: ignore[dict-item]

        # No temp files left behind.
        assert not target.exists()
        temps = list(tmp_path.glob(".context_*.tmp"))
        assert len(temps) == 0
