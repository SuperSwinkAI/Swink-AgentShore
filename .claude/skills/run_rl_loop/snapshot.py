#!/usr/bin/env python3
"""AgentShore session snapshot from the NDJSON log.

Reads the structured log emitted by ``agentshore start`` and prints a compact
status block: session id, agents (with cost and call counts), play tallies,
the last 5 plays with errors for failures, and the top event types by
frequency.

Why a log-based snapshot rather than a SQL one: SQLite WAL mode does not
allow safe concurrent reads from a separate process while agentshore holds
the SHM open. Trying ``mode=ro`` fails with ``database disk image is
malformed`` even when the underlying file is fine. The NDJSON log is the
authoritative live readout — it's what agentshore writes synchronously and
it has every field this report needs.

Usage: python3 snapshot.py /path/to/agentshore_rl_loop.log
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


def _truncate(s: str, n: int = 80) -> str:
    return s if len(s) <= n else s[:n] + "…"


def main(log_path: Path) -> int:
    if not log_path.exists():
        print("[STATUS] no log yet")
        return 0

    events: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not events:
        print("[STATUS] log is empty")
        return 0

    # Session id: take the most common one (handles a session restart in the
    # same log file by surfacing the active one).
    session_ids: dict[str, int] = defaultdict(int)
    for e in events:
        sid = e.get("session_id")
        if isinstance(sid, str):
            session_ids[sid] += 1
    sid = max(session_ids, key=session_ids.get) if session_ids else "?"

    by_event: dict[str, int] = defaultdict(int)
    for e in events:
        by_event[str(e.get("event", "?"))] += 1

    # Agents: aggregate state, cost, and call count from log events.
    agents: dict[str, dict[str, Any]] = {}

    def _agent(aid: str) -> dict[str, Any]:
        return agents.setdefault(
            aid,
            {"id": aid, "agent_type": "?", "status": "?", "calls": 0, "cost": 0.0},
        )

    for e in events:
        ev = e.get("event")
        if ev == "agent_instantiated":
            a = _agent(str(e.get("agent_id", "?")))
            a["agent_type"] = str(e.get("agent_type", "?")).replace("AgentType.", "")
        elif ev == "agent_status_changed":
            a = _agent(str(e.get("agent_id", "?")))
            a["status"] = str(e.get("to_status", "?"))
        elif ev == "cli_dispatch_done":
            a = _agent(str(e.get("agent_id", "?")))
            a["calls"] += 1
            cost = e.get("dollar_cost")
            if isinstance(cost, (int, float)):
                a["cost"] += float(cost)
            atype = e.get("agent_type")
            if isinstance(atype, str) and a["agent_type"] == "?":
                a["agent_type"] = atype.replace("AgentType.", "")
        elif ev == "agent_cleared":
            a = _agent(str(e.get("agent_id", "?")))
            a["status"] = "terminated"

    plays = [e for e in events if e.get("event") == "play_completed"]
    ok = sum(1 for p in plays if p.get("success"))
    fail = len(plays) - ok
    total_cost = sum(a["cost"] for a in agents.values())

    live = [a for a in agents.values() if a["status"] not in {"terminated", "error"}]
    terminated = [a for a in agents.values() if a["status"] in {"terminated", "error"}]

    print(f"[STATUS {time.strftime('%H:%M:%S')}]")
    print(
        f"  Session {sid[:8]}  plays={len(plays)} ok={ok} fail={fail}  "
        f"cost=${total_cost:.4f}  agents={len(live)}/{len(agents)} (live/total)"
    )

    if live:
        for a in live:
            print(
                f"  Agent {a['agent_type']:<14} {a['id'][:8]}  "
                f"status={a['status']}  calls={a['calls']}  cost=${a['cost']:.4f}"
            )
    else:
        print("  (no live agents)")
    if terminated:
        term_cost = sum(a["cost"] for a in terminated)
        term_calls = sum(a["calls"] for a in terminated)
        print(f"  ({len(terminated)} terminated agents — calls={term_calls} cost=${term_cost:.4f})")

    if plays:
        print("  Last 5 plays:")
        for p in plays[-5:]:
            ptype = str(p.get("play_type", "?"))
            success = bool(p.get("success"))
            line = f"    {ptype:<24} ok={int(success)}"
            if not success:
                err = p.get("error")
                if err:
                    line += f"  err={_truncate(str(err))}"
            print(line)
    else:
        print("  (no plays yet)")

    # Top events by frequency. Useful diagnostic surface — repeated
    # ``loop_detected`` or ``learnings_load_failed`` are visible here.
    print("  Top events:")
    for ev, n in sorted(by_event.items(), key=lambda kv: -kv[1])[:8]:
        print(f"    {ev:<28} {n}")

    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: snapshot.py <path-to-agentshore-log>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(Path(sys.argv[1])))
