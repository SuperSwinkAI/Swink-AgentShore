"""StateWriter — atomic state snapshot + appended events log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentshore.ipc.state_writer import (
    EVENTS_FILENAME,
    STATE_FILENAME,
    NullStateWriter,
    StateWriter,
)


@pytest.fixture
def writer(tmp_path: Path) -> StateWriter:
    return StateWriter(tmp_path)


def _msg(d: dict[str, object]) -> str:
    """Format *d* as the StateWriter expects: single-line JSON, no newline."""
    return json.dumps(d, separators=(",", ":"))


async def test_write_state_creates_atomic_snapshot(writer: StateWriter, tmp_path: Path) -> None:
    """First write produces the snapshot file with the serialized payload."""
    await writer.write_state(_msg({"total_plays": 1, "agents": []}))

    state_file = tmp_path / STATE_FILENAME
    assert state_file.exists()
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data == {"total_plays": 1, "agents": []}


async def test_write_state_overwrites_previous(writer: StateWriter, tmp_path: Path) -> None:
    """Successive writes replace the previous snapshot."""
    await writer.write_state(_msg({"total_plays": 1}))
    await writer.write_state(_msg({"total_plays": 2}))

    state_file = tmp_path / STATE_FILENAME
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["total_plays"] == 2

    # No stray tmp files left over (would indicate failed atomic swap).
    tmp_leftovers = list(tmp_path.glob(".dashboard_state-*.tmp"))
    assert tmp_leftovers == []


async def test_write_state_strips_trailing_newline(writer: StateWriter, tmp_path: Path) -> None:
    """The provider's NDJSON helper appends a trailing \\n; the snapshot strips it."""
    await writer.write_state(_msg({"k": 1}) + "\n")

    state_file = tmp_path / STATE_FILENAME
    contents = state_file.read_text(encoding="utf-8")
    assert not contents.endswith("\n")
    assert json.loads(contents) == {"k": 1}


async def test_append_event_creates_ndjson(writer: StateWriter, tmp_path: Path) -> None:
    """Each event is a single NDJSON line in the events file."""
    await writer.append_event(_msg({"type": "play_started", "play_id": 1}))
    await writer.append_event(_msg({"type": "play_completed", "play_id": 1}))

    events_file = tmp_path / EVENTS_FILENAME
    lines = events_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"type": "play_started", "play_id": 1}
    assert json.loads(lines[1]) == {"type": "play_completed", "play_id": 1}


async def test_rotation_keeps_tail_when_file_exceeds_cap(
    writer: StateWriter, tmp_path: Path
) -> None:
    """Events file rotates to the trailing window once it exceeds 5MB.

    Pre-populate a fake oversized file, then append one more event to
    trigger the rotation check.
    """
    events_file = tmp_path / EVENTS_FILENAME
    big_line = json.dumps({"type": "synthetic", "x": "y" * 500}) + "\n"
    target_size = 6 * 1024 * 1024
    with events_file.open("w", encoding="utf-8") as fh:
        written = 0
        while written < target_size:
            fh.write(big_line)
            written += len(big_line)

    assert events_file.stat().st_size > 5 * 1024 * 1024

    await writer.append_event(_msg({"type": "newest", "marker": True}))

    after = events_file.stat().st_size
    assert after <= (1 * 1024 * 1024) + len(big_line) + 256

    # Last line of the rotated file is the freshly-appended event — readers
    # never lose the most recent activity.
    lines = events_file.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == _msg({"type": "newest", "marker": True})


async def test_concurrent_writes_serialize(writer: StateWriter) -> None:
    """The internal asyncio.Lock prevents interleaved tmp+rename races."""
    import asyncio

    payloads = [_msg({"total_plays": i}) for i in range(20)]
    await asyncio.gather(*(writer.write_state(p) for p in payloads))

    # The final file is *some* valid snapshot (not necessarily the last
    # gather-arg, but a well-formed JSON document).
    data = json.loads(writer.state_path.read_text(encoding="utf-8"))
    assert "total_plays" in data
    assert isinstance(data["total_plays"], int)


def test_constructor_clears_stale_session_files(tmp_path: Path) -> None:
    """StateWriter wipes prior-session files so the dashboard bridge
    cannot replay a stale `session_ended` event during prime-from-disk.

    Regression for the v0.13.x file-backed IPC: the session directory is
    keyed by project_key (stable path hash), so successive sessions for
    the same project shared dashboard_events.ndjson. A `session_ended`
    line from the prior session caused the new bridge to call
    `uvicorn.Server.should_exit = True` within milliseconds of binding.
    """
    state_file = tmp_path / STATE_FILENAME
    events_file = tmp_path / EVENTS_FILENAME
    state_file.write_text(_msg({"stale": "snapshot"}), encoding="utf-8")
    events_file.write_text(
        _msg({"type": "session_ended", "payload": {"reason": "cli_request"}}) + "\n",
        encoding="utf-8",
    )

    StateWriter(tmp_path)

    assert not state_file.exists()
    assert not events_file.exists()


def test_constructor_survives_locked_prior_session_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prior-session file the OS won't let us delete (Windows: an orphaned
    dashboard bridge holds it, no FILE_SHARE_DELETE) must not crash boot.

    The old code suppressed only FileNotFoundError, so the PermissionError a
    locked file raises propagated and took the orchestrator down on startup.
    """
    import agentshore.ipc.state_writer as sw

    monkeypatch.setattr(sw.sys, "platform", "win32")
    monkeypatch.setattr(sw, "_WIN_LOCK_BACKOFF_S", 0.0)
    (tmp_path / STATE_FILENAME).write_text(_msg({"stale": 1}), encoding="utf-8")
    (tmp_path / EVENTS_FILENAME).write_text(_msg({"stale": 1}) + "\n", encoding="utf-8")

    def _locked_unlink(self: Path, *a: object, **k: object) -> None:
        raise PermissionError(32, "in use by another process")

    monkeypatch.setattr(Path, "unlink", _locked_unlink)

    # Must not raise despite both files being unremovable.
    StateWriter(tmp_path)


async def test_write_state_retries_transient_lock_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A momentary reader lock on the snapshot (bridge mid-poll) is retried,
    not fatal — the replace succeeds once the reader's handle closes."""
    import agentshore.ipc.state_writer as sw

    monkeypatch.setattr(sw.sys, "platform", "win32")
    monkeypatch.setattr(sw, "_WIN_LOCK_BACKOFF_S", 0.0)
    writer = StateWriter(tmp_path)

    real_replace = sw.os.replace
    calls = {"n": 0}

    def _flaky_replace(src: str, dst: object) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(sw.os, "replace", _flaky_replace)
    await writer.write_state(_msg({"k": "v"}))

    assert calls["n"] == 3
    assert json.loads((tmp_path / STATE_FILENAME).read_text(encoding="utf-8")) == {"k": "v"}


async def test_write_state_drops_snapshot_after_persistent_lock_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the lock never clears, a coalesced snapshot is dropped (recoverable)
    rather than crashing the orchestrator loop."""
    import agentshore.ipc.state_writer as sw

    monkeypatch.setattr(sw.sys, "platform", "win32")
    monkeypatch.setattr(sw, "_WIN_LOCK_BACKOFF_S", 0.0)
    monkeypatch.setattr(sw, "_WIN_LOCK_RETRIES", 3)
    writer = StateWriter(tmp_path)

    def _always_locked(src: str, dst: object) -> None:
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(sw.os, "replace", _always_locked)
    # Must return cleanly, leaving no temp files behind.
    await writer.write_state(_msg({"k": "v"}))
    assert not list(tmp_path.glob(".dashboard_state-*.tmp"))


async def test_null_writer_is_noop(tmp_path: Path) -> None:
    """NullStateWriter accepts both methods without touching the disk."""
    writer = NullStateWriter()
    await writer.write_state(_msg({"x": 1}))
    await writer.append_event(_msg({"x": 1}))
    # nothing should be written into tmp_path
    assert list(tmp_path.iterdir()) == []
