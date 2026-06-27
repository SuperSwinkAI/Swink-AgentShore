"""Tests for ``agentshore.data.corruption_evidence.capture_corruption_evidence``.

The collector is best-effort by design — every sub-step swallows its own
errors. Tests focus on: (a) the dict always has the canonical top-level
keys, (b) sub-step failures don't raise, (c) truncation works, (d) the
``corruption_event_id`` is a fresh UUID per call.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from unittest.mock import patch

from agentshore.data.corruption_evidence import capture_corruption_evidence


def _make_db(path: Path) -> Path:
    """Create a tiny SQLite file so file-stat assertions work."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.commit()
    conn.close()
    return path


def test_evidence_returns_dict_with_canonical_top_level_keys(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "agentshore.db")
    out = capture_corruption_evidence(db)
    for key in (
        "corruption_event_id",
        "captured_at_unix",
        "platform",
        "machine",
        "db_path",
        "db_files",
        "power_state",
        "caffeinate",
        "system_log",
        "fd_holders",
    ):
        assert key in out, f"missing key {key!r} in evidence dict"


def test_evidence_corruption_event_id_is_fresh_uuid(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "agentshore.db")
    out1 = capture_corruption_evidence(db)
    out2 = capture_corruption_evidence(db)
    assert out1["corruption_event_id"] != out2["corruption_event_id"]
    uuid.UUID(out1["corruption_event_id"])  # must parse


def test_evidence_includes_db_file_stats(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "agentshore.db")
    out = capture_corruption_evidence(db)
    main = out["db_files"][db.name]
    assert main is not None
    assert main["size_bytes"] > 0
    assert "mtime_unix" in main
    assert main["mode_octal"].startswith("0o")


def test_evidence_handles_missing_wal_shm_siblings(tmp_path: Path) -> None:
    """No WAL / SHM sibling files → those entries are ``None``, not raises."""
    db = _make_db(tmp_path / "agentshore.db")
    out = capture_corruption_evidence(db)
    assert out["db_files"][db.name + "-wal"] is None
    assert out["db_files"][db.name + "-shm"] is None


def test_evidence_records_seconds_since_db_mtime(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "agentshore.db")
    out = capture_corruption_evidence(db)
    assert "seconds_since_db_mtime" in out
    assert out["seconds_since_db_mtime"] >= 0.0


def test_evidence_returns_partial_dict_when_subprocess_missing(
    tmp_path: Path,
) -> None:
    """A missing CLI (pmset, lsof, etc.) doesn't abort capture."""
    db = _make_db(tmp_path / "agentshore.db")
    with patch("shutil.which", return_value=None):
        out = capture_corruption_evidence(db)
    assert "corruption_event_id" in out
    assert "power_state" in out
    # Sub-step errors are recorded inline.
    if out["platform"] == "darwin":
        assert "pmset_ps_error" in out["power_state"]


def test_evidence_truncates_long_output(tmp_path: Path) -> None:
    """Synthesised huge stdout from a sub-step is truncated to the documented limit."""
    from agentshore.data.corruption_evidence import _truncate

    huge = "x" * 50_000
    truncated = _truncate(huge, 8 * 1024)
    assert len(truncated.encode("utf-8")) <= 8 * 1024 + 32  # +marker
    assert "truncated" in truncated


def test_evidence_db_path_string_matches_input(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "agentshore.db")
    out = capture_corruption_evidence(db)
    assert out["db_path"] == str(db)
