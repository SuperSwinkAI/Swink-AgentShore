"""Per-session fleet-concurrency log (Phase 1 of fleet-concurrency tracking).

Each completed play appends one denormalized, self-describing NDJSON sample to
``fleet_concurrency.ndjson`` in the session directory. "Self-describing" is the
design goal: every line carries a full agent **roster** (type, tier, status,
current play, error class) *and* pre-reduced aggregates, so any dimension or
subset — by harness, by tier, by status, correlated with reward / rate-limit
churn — can be reconstructed offline without re-running the session.

This is a standalone observability artifact: it does NOT touch the SQLite schema
(no version bump) and is independent of the dashboard transport (the Stats-tab
graph derives its own series client-side from the snapshot stream). The
end-of-session report (Phase 2) aggregates this file; the writer never persists
derived rollups, so they cannot drift from the raw series.

Robustness invariant: writing a sample must never crash the orchestrator loop —
:meth:`ConcurrencyLog.record` swallows every error and degrades to a warning.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from agentshore.state import AgentStatus

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agentshore.state import AgentSnapshot, OrchestratorState, PlayOutcome

_logger = structlog.get_logger(__name__)

CONCURRENCY_FILENAME = "fleet_concurrency.ndjson"
# Bump when the line shape changes so downstream readers (ESR, graph, ad-hoc
# analysis) can branch on it instead of guessing.
RECORD_VERSION = 1

_LIVE_STATUSES = (AgentStatus.IDLE, AgentStatus.BUSY)


def _enum_value(value: object) -> str | None:
    """Return ``.value`` for an enum-or-None field, else ``None``."""
    return getattr(value, "value", None)


def build_concurrency_record(
    *,
    agents: Sequence[AgentSnapshot],
    total_plays: int,
    outcome: PlayOutcome,
    reward: float,
    seq: int,
    ts: str,
    session_id: str,
) -> dict[str, object]:
    """Build one fleet-concurrency sample as a plain JSON-able dict.

    Pure: no I/O and no clock, so it is fully deterministic and unit-testable —
    the caller supplies *seq* (monotonic per-session index) and *ts* (ISO
    timestamp). Decoupled from ``OrchestratorState`` (takes ``agents`` +
    ``total_plays`` directly) so tests need only build the roster, not the
    ~50-field state object.

    The returned dict denormalizes three views of the same instant:

    * ``roster`` — one entry per agent; the source of truth from which every
      aggregate below can be recomputed for an arbitrary subset.
    * convenience aggregates (``busy_by_type``, ``busy_by_type_tier``, …) so a
      ``jq`` one-liner or the graph can read counts without reducing the roster.
    * the completed-play context (``play_type``, ``reward``, the completed
      agent's ``error_class``) so concurrency correlates with outcome / churn
      on the same line, no join required.
    """
    roster: list[dict[str, object]] = []
    busy_by_type: dict[str, int] = {}
    live_by_type: dict[str, int] = {}
    busy_by_type_tier: dict[str, int] = {}
    status_totals: dict[str, int] = {status.value: 0 for status in AgentStatus}
    busy_total = 0
    live_total = 0

    for agent in agents:
        agent_type = agent.agent_type.value
        status_totals[agent.status.value] = status_totals.get(agent.status.value, 0) + 1
        roster.append(
            {
                "agent_id": agent.agent_id,
                "agent_type": agent_type,
                "model_tier": agent.model_tier,
                "status": agent.status.value,
                "play_type": _enum_value(agent.current_play_type),
                "error_class": _enum_value(agent.last_error_class),
            }
        )
        if agent.status in _LIVE_STATUSES:
            live_total += 1
            live_by_type[agent_type] = live_by_type.get(agent_type, 0) + 1
        if agent.status is AgentStatus.BUSY:
            busy_total += 1
            busy_by_type[agent_type] = busy_by_type.get(agent_type, 0) + 1
            tier_key = f"{agent_type}/{agent.model_tier or 'unknown'}"
            busy_by_type_tier[tier_key] = busy_by_type_tier.get(tier_key, 0) + 1

    completed = None
    if outcome.agent_id is not None:
        completed = next((a for a in agents if a.agent_id == outcome.agent_id), None)

    return {
        "v": RECORD_VERSION,
        "ts": ts,
        "session_id": session_id,
        "seq": seq,
        "total_plays": total_plays,
        "play_id": outcome.play_id,
        "play_type": outcome.play_type.value,
        "success": outcome.success,
        "skipped": outcome.skipped,
        "reward": reward,
        # Context of the dispatch that just finished (what changed the fleet).
        "completed_agent_id": outcome.agent_id,
        "completed_agent_type": completed.agent_type.value if completed is not None else None,
        "completed_model_tier": completed.model_tier if completed is not None else None,
        "completed_error_class": (
            _enum_value(completed.last_error_class) if completed is not None else None
        ),
        # Aggregates (busy = simultaneously dispatched; the concurrency that
        # presses on provider rate limits + RAM, per the design rationale).
        "busy_total": busy_total,
        "live_total": live_total,
        "busy_by_type": busy_by_type,
        "live_by_type": live_by_type,
        "busy_by_type_tier": busy_by_type_tier,
        "status_totals": status_totals,
        # Source of truth — any other slice is recomputable from here.
        "roster": roster,
    }


class ConcurrencyLog:
    """Append-only per-session writer for ``fleet_concurrency.ndjson``.

    One instance per session. File I/O is offloaded to a thread (no blocking the
    core loop) and :meth:`record` is fully guarded — observability must never
    take the orchestrator down.
    """

    def __init__(self, session_dir: Path, session_id: str) -> None:
        self._dir = session_dir
        self._path = session_dir / CONCURRENCY_FILENAME
        self._session_id = session_id
        self._lock = asyncio.Lock()
        self._seq = 0
        # Per-session artifact. The session directory is keyed by a stable
        # project hash, so a prior session for the same project would otherwise
        # leave its samples behind and the ESR/graph would read two runs. Drop
        # it on construction; nothing tails this file, so there is no reader
        # lock to fight (unlike the dashboard state/event files).
        with contextlib.suppress(OSError):
            self._path.unlink()

    @property
    def path(self) -> Path:
        return self._path

    async def record(
        self, *, next_state: OrchestratorState, outcome: PlayOutcome, reward: float
    ) -> None:
        """Append one sample. Never raises — degrades to a warning on failure.

        Takes the whole ``next_state`` (rather than its ``.agents`` /
        ``.total_plays``) so the field access happens *inside* this guarded body:
        an unguarded attribute read evaluated as a call argument is the exact
        failure shape that crashes the loop, which this writer must not do.
        """
        try:
            async with self._lock:
                self._seq += 1
                seq = self._seq
            record = build_concurrency_record(
                agents=next_state.agents,
                total_plays=next_state.total_plays,
                outcome=outcome,
                reward=reward,
                seq=seq,
                ts=datetime.now(UTC).isoformat(),
                session_id=self._session_id,
            )
            line = json.dumps(record, separators=(",", ":")) + "\n"
            await asyncio.to_thread(self._append, line)
        except Exception as exc:  # observability must not crash the loop
            _logger.warning("concurrency_log_record_failed", error=str(exc))

    def _append(self, line: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)


class NullConcurrencyLog:
    """No-op writer for headless / non-PPO / test paths."""

    async def record(
        self, *, next_state: OrchestratorState, outcome: PlayOutcome, reward: float
    ) -> None:
        return None
