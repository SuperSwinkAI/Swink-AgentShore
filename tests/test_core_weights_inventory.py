"""Tests for ``weights_dir_inventory`` telemetry emitted at session start and shutdown."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import pytest
import structlog

from agentshore.config import RuntimeConfig
from agentshore.core import Orchestrator
from agentshore.core.helpers import _emit_weights_dir_inventory

if TYPE_CHECKING:
    from pathlib import Path


def _read_session_log(repo_root: Path, session_id: str) -> list[dict[str, object]]:
    """Read the NDJSON session log and return parsed events."""
    log_path = repo_root / ".agentshore" / "logs" / f"agentshore-{session_id}.log"
    if not log_path.exists():
        return []
    events: list[dict[str, object]] = []
    for raw_line in log_path.read_text().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _cfg() -> RuntimeConfig:
    return RuntimeConfig()


def _events_from_caplog(records: list[logging.LogRecord]) -> list[dict[str, object]]:
    """Reconstruct structlog event dicts from stdlib ``LogRecord`` objects.

    With ``structlog.stdlib.ProcessorFormatter.wrap_for_formatter`` configured
    (as ``setup_logging`` does), the ``record.msg`` is the full event_dict
    (passed through as a positional payload). When other tests bypass that
    formatter, the event metadata is attached as record attributes instead;
    handle both shapes.
    """
    out: list[dict[str, object]] = []
    std_fields = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()) | {
        "message",
        "asctime",
    }
    for r in records:
        msg = r.msg
        if isinstance(msg, dict):
            out.append(msg)
            continue
        # Fallback: attribute-based payload.
        if hasattr(r, "event"):
            ev: dict[str, object] = {
                k: v for k, v in r.__dict__.items() if k not in std_fields and not k.startswith("_")
            }
            out.append(ev)
    return out


def test_emit_weights_dir_inventory_missing_dir(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When the dir does not exist, the event records ``exists=False`` with zero counts."""
    weights = tmp_path / "no_such_dir"

    with (
        structlog.testing.capture_logs() as captured,
        caplog.at_level(logging.INFO, logger="agentshore.core"),
    ):
        _emit_weights_dir_inventory(weights, phase="session_start")
    events = list(captured) if captured else _events_from_caplog(list(caplog.records))

    matching = [e for e in events if e.get("event") == "weights_dir_inventory"]
    assert len(matching) == 1, events
    e = matching[0]
    assert e["phase"] == "session_start"
    assert e["path"] == str(weights)
    assert e["exists"] is False
    assert e["file_count"] == 0
    assert e["total_bytes"] == 0


def test_emit_weights_dir_inventory_counts_pt_files(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Counts ``*.pt`` files and sums their sizes; ignores non-pt entries."""
    weights = tmp_path / "weights"
    weights.mkdir()
    (weights / "policy_v1.pt").write_bytes(b"\x00" * 12)
    (weights / "policy_v2.pt").write_bytes(b"\x00" * 30)
    (weights / "README.txt").write_bytes(b"ignored")

    with (
        structlog.testing.capture_logs() as captured,
        caplog.at_level(logging.INFO, logger="agentshore.core"),
    ):
        _emit_weights_dir_inventory(weights, phase="shutdown_step")
    events = list(captured) if captured else _events_from_caplog(list(caplog.records))

    matching = [e for e in events if e.get("event") == "weights_dir_inventory"]
    assert len(matching) == 1, events
    e = matching[0]
    assert e["phase"] == "shutdown_step"
    assert e["path"] == str(weights)
    assert e["exists"] is True
    assert e["file_count"] == 2
    assert e["total_bytes"] == 42


@pytest.mark.asyncio
async def test_weights_dir_inventory_fires_at_start_and_shutdown(tmp_path: Path) -> None:
    """``weights_dir_inventory`` fires once with each phase across a bootstrap+stop cycle."""
    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path)
    sid = orch._session_id
    async with orch:
        pass  # immediate exit triggers stop()

    # Flush stdlib log handlers so the NDJSON file is fully written.
    for handler in logging.getLogger().handlers:
        handler.flush()

    events = _read_session_log(tmp_path, sid)
    inventory_events = [e for e in events if e.get("event") == "weights_dir_inventory"]
    phases = [e["phase"] for e in inventory_events]
    assert phases.count("session_start") == 1, inventory_events
    assert phases.count("shutdown_step") == 1, inventory_events

    expected_path = str(tmp_path / ".agentshore" / "weights")
    for e in inventory_events:
        assert e["path"] == expected_path
        assert "exists" in e
        assert "file_count" in e
        assert "total_bytes" in e


@pytest.mark.asyncio
async def test_weights_inventory_shutdown_step_logged_before_store_close(tmp_path: Path) -> None:
    """The new ``weights_inventory`` shutdown_step is emitted before ``store_close``."""
    orch = await Orchestrator.bootstrap(cfg=_cfg(), repo_root=tmp_path)
    sid = orch._session_id
    async with orch:
        pass

    for handler in logging.getLogger().handlers:
        handler.flush()

    events = _read_session_log(tmp_path, sid)
    shutdown_steps = [
        e["step"] for e in events if e.get("event") == "shutdown_step" and "step" in e
    ]
    assert "weights_inventory" in shutdown_steps, shutdown_steps
    assert "store_close" in shutdown_steps, shutdown_steps
    assert shutdown_steps.index("weights_inventory") < shutdown_steps.index("store_close")
