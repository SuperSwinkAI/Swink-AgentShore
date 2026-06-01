#!/usr/bin/env python3
"""Machine-readable progress counters for a running AgentShore session.

Companion to ``snapshot.py``. ``snapshot.py`` is the human-readable check-in
readout; this script emits a single JSON line that ``/monitor_run`` diffs
across check-ins to drive its idle/exit/error logic without grepping.

Why log-based rather than SQL: SQLite WAL mode does not allow safe concurrent
reads while agentshore holds the SHM open (``mode=ro`` fails with "database
disk image is malformed" even on a healthy file). The NDJSON log is the
authoritative live readout.

The skill compares ``play_completed`` between consecutive check-ins:
``play_completed`` is the authoritative "real work happened" marker. Zero new
completed plays since the previous check-in is the idle signal; a rising
``loop_detected`` with no new completed plays is the stronger wedge signal.

Output (one JSON object on stdout):
  {
    "ok": true,                # false only when the log is missing/empty
    "session_id": "662a7c5d",
    "play_completed": 104,     # cumulative count — diff this across check-ins
    "play_started": 109,
    "ok_plays": 75,
    "fail_plays": 29,
    "loop_detected": 3,        # wedge signal — diff this too
    "selector_idle": 56,       # PPO had nothing to pick
    "all_masked": 43,          # every action masked — stuck-shaped
    "error_lines": 7,          # level=error / level=critical lines
    "traceback_lines": 2,      # lines beginning a Python traceback
    "asyncio_unretrieved": 0,  # "task exception was never retrieved"
    "ended": false,            # a terminal shutdown event is present
    "end_event": null,         # which one, if ended
    "last_event_ts": "2026-06-01T00:59:12.001Z",
    "last_event_age_s": 1234   # seconds since the newest log line (-1 if unknown)
  }

Usage: python3 progress.py /path/to/agentshore-<session_id>.log
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

# Terminal shutdown events, in the order we prefer to report them.
_END_EVENTS = ("session_ended", "shutdown_complete", "drain_complete", "session_shutdown")

# Error signals. Substring matches on bare "Error:"/"Exception:" over-match
# agent output_tail JSON, so we key off the structured ``level`` field of each
# parsed event instead (same rationale as run_rl_loop, but parsed rather than
# grepped — the NDJSON renders ``"level": "info"`` *with* a space, so a raw
# ``"level":"error"`` substring scan silently never matches).
_ERROR_LEVELS = frozenset({"error", "critical"})


def _parse_ts(raw: str) -> float | None:
    """Parse an ISO8601 timestamp (trailing ``Z``) to epoch seconds.

    AgentShore stamps every event UTC with a trailing ``Z``; swapping it for
    ``+00:00`` makes ``fromisoformat`` return a tz-aware datetime whose
    ``.timestamp()`` is correct. Kept import-light (no ``datetime.UTC``) so the
    script runs under the system ``python3`` (3.10) the skill invokes, not just
    the project's 3.12 venv.
    """
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def main(log_path: Path) -> int:
    if not log_path.exists():
        print(json.dumps({"ok": False, "reason": "no log yet"}))
        return 0

    text = log_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    by_event: dict[str, int] = defaultdict(int)
    ok_plays = 0
    fail_plays = 0
    session_ids: dict[str, int] = defaultdict(int)
    last_ts_raw: str | None = None
    ended = False
    end_event: str | None = None
    error_lines = 0

    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev = str(e.get("event", "?"))
        by_event[ev] += 1
        sid = e.get("session_id")
        if isinstance(sid, str):
            session_ids[sid] += 1
        ts = e.get("timestamp")
        if isinstance(ts, str):
            last_ts_raw = ts
        if str(e.get("level", "")).lower() in _ERROR_LEVELS:
            error_lines += 1
        if ev == "play_completed":
            if e.get("success"):
                ok_plays += 1
            else:
                fail_plays += 1
        if ev in _END_EVENTS and not ended:
            ended = True
            end_event = ev

    # Traceback / asyncio signals: scan raw lines as a backstop. The formatter
    # folds exceptions into a structured ``exception`` field, but a hard crash
    # printed by Python's default handler still lands here line-anchored.
    traceback_lines = sum(1 for ln in lines if ln.lstrip().startswith("Traceback"))
    asyncio_unretrieved = sum(1 for ln in lines if "task exception was never retrieved" in ln)

    last_age = -1.0
    if last_ts_raw is not None:
        epoch = _parse_ts(last_ts_raw)
        if epoch is not None:
            last_age = max(0.0, time.time() - epoch)

    sid = max(session_ids, key=lambda k: session_ids[k]) if session_ids else "?"

    out: dict[str, Any] = {
        "ok": True,
        "session_id": sid[:8],
        "play_completed": by_event.get("play_completed", 0),
        "play_started": by_event.get("play_started", 0),
        "ok_plays": ok_plays,
        "fail_plays": fail_plays,
        "loop_detected": by_event.get("loop_detected", 0),
        "selector_idle": by_event.get("selector_idle", 0),
        "all_masked": by_event.get("ppo_selector.all_masked", 0),
        "error_lines": error_lines,
        "traceback_lines": traceback_lines,
        "asyncio_unretrieved": asyncio_unretrieved,
        "ended": ended,
        "end_event": end_event,
        "last_event_ts": last_ts_raw,
        "last_event_age_s": round(last_age, 1),
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: progress.py <path-to-agentshore-log>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(Path(sys.argv[1])))
